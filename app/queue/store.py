"""死信表（Postgres）：**任务失败之后的唯一留痕处**（`12.queue.md` §6 第 3 步）。

为什么死信要落**业务库**而不是留在 Redis：

| 方案 | 重启后还在？ | 能按字段查？ | 与审计/报表同源？ |
|---|---|---|---|
| 留在 Redis 的失败结果 | 结果 TTL 一到就没了（默认 24h） | ❌（只能按 job_id 取一条） | ❌ |
| 落一个专用表 | ✅ | ✅（「最近一周按错误类型分组」这种问题要能回答） | ✅ |

`12.queue.md` §8 的验收写的是「零任务丢失，可人工重放」——
「可人工重放」要求失败现场在**人能查、能筛、能标记已处理**的地方，
Redis 的字符串结果做不到这些。

## 三个刻意的设计决定

1. **`job_id` 上有唯一约束，写入用 `ON CONFLICT DO UPDATE` 而不是 DO NOTHING**：
   `DO NOTHING` 会让「同一条死信被第二次写」静默消失，而 `DO UPDATE` 必须显式决定
   「哪些列允许被覆盖」。这里只允许覆盖**失败现场**那几个字段，`resolution` /
   `replay_count` / `replayed_job_id` **绝不覆盖** —— 否则一次重复写入会把
   「已经人工重放成功」的状态抹掉，任务会重新出现在待处理列表里。
2. **`resolution` 是文本 + `resolved_at` 时间戳，不是布尔位**：
   死信的收场方式不止一种（`replayed_ok` / `replayed_failed` / `wont_fix` / `manual_fix`），
   而布尔位会逼着后来的人往别处记「怎么处理的」。
3. **`payload` 用 JSONB**：重放时原样喂回任务，所以它必须能被完整还原
   （JSONB 比 Text 多出「能按字段查」与「写入时校验」两个好处，代价只是不能存非 JSON 值 ——
   而那本来就是 `JobSpec.payload` 的契约）。"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import QueueDeadLetter
from app.db.session import SessionLocal, engine
from app.queue.core import DeadLetterRecord
from app.queue.jobs import RetryPolicy

logger = logging.getLogger(__name__)

#: 一次查询最多取多少条（防止「死信积压一万条」时把内存打满）
DEFAULT_PAGE: Final[int] = 50

#: 允许被重复写入覆盖的列（**失败现场**；见模块 docstring 第 1 条）
_REWRITABLE: Final[tuple[str, ...]] = (
    "function",
    "kind",
    "payload",
    "attempts",
    "error_type",
    "error_message",
    "error_traceback",
    "retryable",
    "policy",
    "enqueued_at",
    "failed_at",
    "user_id",
)


def _policy_dict(policy: RetryPolicy | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    return {
        "attempts": policy.attempts,
        "base_delay": policy.base_delay,
        "max_delay": policy.max_delay,
        "jitter": policy.jitter,
    }


def to_row(entry: DeadLetterRecord) -> dict[str, Any]:
    """`DeadLetterRecord` → 一行（**唯一的组装点**，重写与首次写入共用）。"""
    return {
        "job_id": entry.job_id,
        "function": entry.function,
        "kind": entry.kind,
        "payload": dict(entry.payload or {}),
        "attempts": int(entry.attempts),
        "error_type": entry.error_type or "",
        "error_message": entry.error_message or "",
        "error_traceback": entry.error_traceback or "",
        "retryable": bool(entry.retryable),
        "policy": _policy_dict(entry.policy),
        "enqueued_at": entry.enqueued_at,
        "failed_at": entry.failed_at,
        "user_id": entry.user_id,
    }


def to_record(row: Mapping[str, Any]) -> DeadLetterRecord:
    """一行 → `DeadLetterRecord`（重放脚本与用例共用）。"""
    raw = dict(row)
    policy_data = raw.get("policy") or None
    policy = RetryPolicy(**policy_data) if isinstance(policy_data, dict) else None
    return DeadLetterRecord(
        job_id=str(raw["job_id"]),
        function=str(raw["function"]),
        kind=str(raw["kind"]),
        payload=dict(raw.get("payload") or {}),
        attempts=int(raw.get("attempts") or 1),
        error_type=str(raw.get("error_type") or ""),
        error_message=str(raw.get("error_message") or ""),
        error_traceback=str(raw.get("error_traceback") or ""),
        retryable=bool(raw.get("retryable")),
        policy=policy,
        enqueued_at=raw.get("enqueued_at"),
        failed_at=raw.get("failed_at"),
        user_id=raw.get("user_id"),
        replay_count=int(raw.get("replay_count") or 0),
        replayed_job_id=str(raw.get("replayed_job_id") or ""),
        resolution=str(raw.get("resolution") or ""),
        resolved_at=raw.get("resolved_at"),
    )


class PostgresDeadLetterStore:
    """死信表的生产实现（**每个方法一次短事务**，不持有长连接）。

    ⚠️ 为什么不是一个长驻 Session：worker 是长跑进程，而长事务会让
    PostgreSQL 的 autovacuum 无法回收（死信表只在失败时写，本该是最干净的表）。
    每次方法自己开一个 `Session`，用完即还（与 `app/db/session.get_db` 的口径一致）。
    """

    def __init__(self, *, session_factory: Any = None) -> None:
        self._session_factory = session_factory or SessionLocal

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ── 写 ───────────────────────────────────────────────────────────────────
    def record(self, entry: DeadLetterRecord) -> bool:
        """写一条死信；返回**是不是新建的**（已存在则只更新失败现场）。

        用 `ON CONFLICT (job_id) DO UPDATE` 而不是 `DO NOTHING` 的理由见模块 docstring：
        `DO NOTHING` 会让「任务被重投后第二次失败」的现场（attempts 变大、错误变了）
        静默消失。真正不能被覆盖的是「人工已处理」的那几个字段，见 `_REWRITABLE`。

        ⚠️ 「是不是新建的」是**先查后写**（`existed`）得到的，不是 `RETURNING` 的花样：
        这一步只在失败路径上跑，多一次 SELECT 的代价可以忽略；
        而把两个查询塞进同一个事务里能保证「先查后写」不会与并发写入打架
        （同一 job_id 的两次写入最多发生一次，因为 arq 的 job_id 保证同一时刻只有一个实例在跑）。
        """
        values = to_row(entry)
        with self._session() as session:
            existed = (
                session.execute(
                    select(QueueDeadLetter.job_id).where(QueueDeadLetter.job_id == values["job_id"])
                ).first()
                is not None
            )
            statement = pg_insert(QueueDeadLetter).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[QueueDeadLetter.job_id],
                set_={column: statement.excluded[column] for column in _REWRITABLE},
            )
            session.execute(statement)
        return not existed

    # ── 读 ───────────────────────────────────────────────────────────────────
    #
    # ⚠️ 读语句一律用 `select(QueueDeadLetter.__table__)`（**Table**，不是 ORM 实体）：
    # `session.execute(select(Entity))` 返回的是 `Row` 对象，`.mappings()` 拿到的是
    # `{实体属性名: ...}` 形状里**没有**我们列的键（实测症状：`KeyError: 'job_id'`）。
    # 用 Table 选出来的才是标准列映射，`to_record()` 才能统一吃两边的行。
    def fetch(self, job_id: str) -> DeadLetterRecord | None:
        table = QueueDeadLetter.__table__
        with self._session() as session:
            row = (
                session.execute(select(table).where(table.c.job_id == str(job_id)))
                .mappings()
                .first()
            )
        return to_record(row) if row else None

    def pending(self, *, limit: int = DEFAULT_PAGE, include_resolved: bool = False) -> list[DeadLetterRecord]:
        """按 `failed_at` 升序取一批（**先失败的先处理**）。

        `include_resolved=False`（默认）只取还没处理过的：重放脚本的默认行为是
        「把待处理的都跑一遍」，而已处理过的再跑一次会浪费一次模型/DB 调用
        （还要靠幂等键兜住，不如一开始就不取）。
        """
        table = QueueDeadLetter.__table__
        statement = select(table).order_by(table.c.failed_at.asc()).limit(int(limit))
        if not include_resolved:
            statement = statement.where(table.c.resolution == "")
        with self._session() as session:
            rows = session.execute(statement).mappings().all()
        return [to_record(row) for row in rows]

    def count_pending(self) -> int:
        table = QueueDeadLetter.__table__
        with self._session() as session:
            return int(
                session.execute(
                    select(func.count()).select_from(table).where(table.c.resolution == "")
                ).scalar_one()
            )

    def oldest_pending_age(self) -> float | None:
        """最久未处理的死信已经躺了多久（秒）。`None` = 没有待处理的。

        这是**唯一值得告警的死信指标**：条数多可以是「刚发生一批抖动」，
        而「最老的躺了三天」意味着人工兜底这条路径**根本没人在走**（A1 失效）。
        """
        table = QueueDeadLetter.__table__
        with self._session() as session:
            oldest = session.execute(
                select(func.min(table.c.failed_at)).where(table.c.resolution == "")
            ).scalar_one_or_none()
        if oldest is None:
            return None
        now = datetime.now(timezone.utc)
        moment = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
        return max(0.0, (now - moment).total_seconds())

    # ── 标记 ─────────────────────────────────────────────────────────────────
    def resolve(self, job_id: str, *, resolution: str, replayed_job_id: str = "") -> bool:
        """标记一条死信已被处理（重放成功 / 人工关闭）。

        `replay_count` 在这里 +1（而不是在「发起重放」时）：语义是
        「这条死信被真正推进过一次」。发起时 +1 会让「入队失败」也算进去，
        而那时任务其实没跑。
        """
        text = str(resolution or "").strip()
        if not text:
            raise ValueError("resolution 不能为空：它记录「这条死信是怎么收场的」")
        table = QueueDeadLetter.__table__
        statement = (
            update(table)
            .where(table.c.job_id == str(job_id))
            .values(
                resolution=text,
                resolved_at=func.now(),
                replay_count=table.c.replay_count + 1,
                replayed_job_id=str(replayed_job_id or ""),
            )
        )
        with self._session() as session:
            result = session.execute(statement)
        return bool(result.rowcount)

    # ── 探活 ─────────────────────────────────────────────────────────────────
    def available(self) -> bool:
        """表是否存在、库是否可达（不抛异常；`/health` 与验收脚本用）。"""
        try:
            with self._session() as session:
                session.execute(select(func.count()).select_from(QueueDeadLetter.__table__)).scalar_one()
            return True
        except SQLAlchemyError as exc:
            logger.warning("死信表不可用：%s", exc)
            return False


def table_engine() -> Engine:
    """暴露写路径引擎（验收脚本里直接查行数用；业务代码不要用）。"""
    return engine


__all__ = [
    "DEFAULT_PAGE",
    "PostgresDeadLetterStore",
    "table_engine",
    "to_record",
    "to_row",
]
