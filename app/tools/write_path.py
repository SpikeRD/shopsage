"""写路径公共设施：**「唯一副作用出口」的落地零件**（`6.tools.md` 第 4 节第 1 条）。

`6.tools.md` 把工具定义成「一切副作用（建工单、退款）的唯一出口」，本模块提供两个领域工具
（`ticket.py` / `refund.py`）共用的三样东西，避免它们各写一份、各错一处：

1. **写会话**：写操作只走 `SessionLocal`（绑定**写**引擎）。读路径的 `get_read_engine()`
   在这里**不允许出现** —— 「读写分离」在代码层面就是「两个引擎入口不许混用」，
   混用之后 D1 的整套防线就退化成命名习惯（`3.db-runbook.md` §5.3 的决策记录）。
2. **幂等插入**：`INSERT ... ON CONFLICT DO NOTHING` + 回查既有行。
   为什么不是「先 SELECT 再 INSERT」：并发重投（第 12 步队列重放、用户连点两次）时，
   两个请求会**同时**查到空、**同时**插入 —— 竞态窗口在大压力下必然被命中。
   把幂等性交给数据库的唯一约束，应用层只负责「撞了就取回那一行」。
3. **幂等的命名空间**：唯一键是 `(user_id, idempotency_key)` 复合列。
   单列全局唯一的版本有个隐蔽的越权通道：用户 B 拿一个与用户 A 相同的键，
   就会命中 A 的行、拿到 A 的 ticket_id。复合键等于给键加了「谁发起的」这个前缀。

⚠️ 本模块**不暴露给 agent**：`app/tools/__init__.py` 只导出领域工具函数（`create_ticket` 等），
不导出这里的 `write_session` / `insert_once` —— 「能写库」这件事不该成为工具层的公共 API。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.tools.errors import ToolContractError

# 幂等键的长度上限（与 `tickets.idempotency_key` / `refunds.idempotency_key` 的 VARCHAR(64) 一致）。
# 在应用层先拦一道的理由：让报错发生在**第一次调用**、且消息里带「怎么办」，
# 而不是让数据库抛 22001（value too long），调用方还得去猜是哪一列太长了。
MAX_IDEMPOTENCY_KEY_CHARS: Final[int] = 64

# 幂等键的字符集：允许 UUID、`req-123`、`chat:42:refund` 这类写法，但不允许空白与控制字符
# （它们会让「同一个键」在日志/CSV/HTTP 头里出现多种等价写法，幂等随之失效）。
_IDEMPOTENCY_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._:\-]+$")


def validate_idempotency_key(key: Any) -> str:
    """校验幂等键形状，返回规范化后的值（去首尾空白）。

    三条硬要求（都对应真实事故）：
      · 非空：留空等于「没有幂等」，重复请求会重复建单（A2 就白做了）；
      · ≤64 字符：给数据库列宽留出明确边界；
      · 只含 `[A-Za-z0-9._:-]`：避免「看起来不同的键其实是同一个」或反之。
    """
    if not isinstance(key, str) or not key.strip():
        raise ToolContractError(
            "idempotency_key 不能为空：写操作必须带幂等键，"
            "建议由调用方生成天然唯一的键（请求 id / 图的一次执行 id / `chat:<会话>:<序号>`）"
        )
    cleaned = key.strip()
    if len(cleaned) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise ToolContractError(
            f"idempotency_key 过长（{len(cleaned)} > {MAX_IDEMPOTENCY_KEY_CHARS}）："
            "请改用短键（如请求 id），不要塞入整段文本"
        )
    if not _IDEMPOTENCY_KEY_RE.match(cleaned):
        raise ToolContractError(
            f"idempotency_key 含非法字符：{cleaned!r}（只允许字母/数字/._:-，不允许空白与换行）"
        )
    return cleaned


@contextmanager
def write_session(session: Session | None = None) -> Iterator[Session]:
    """写路径会话：调用方给了就用它的（测试/事务复用），否则自己开一个并用**写引擎**。

    与 `app/db/queries.py::_read_session` 刻意**不共用**：那个走只读引擎，这个走写引擎 ——
    「哪条路径用哪个引擎」是安全属性，共用一个 helper 就等于把这个属性藏进了参数默认值。
    """
    if session is not None:
        yield session
        return
    with SessionLocal() as own:
        yield own


def insert_once(
    db: Session,
    model: type[Any],
    values: Mapping[str, Any],
    *,
    conflict_on: tuple[str, ...],
    lookup: Mapping[str, Any],
) -> tuple[Any, bool]:
    """幂等插入：返回 `(主键值, 是否新建)`。

    参数：
        values: 要插入的列（必须包含 `conflict_on` 里的列）。
        conflict_on: 唯一约束的列（本仓一律是 `("user_id", "idempotency_key")`）。
        lookup: 冲突后按它回查既有行的等值条件。

    实现口径（两条都必须）：
      1. `on_conflict_do_nothing(index_elements=...)` —— 让数据库**原子地**决定谁是第一次；
      2. 拿不到 RETURNING 就按 `lookup` 回查 —— 那说明键已存在，这正是「幂等命中」。

    ⚠️ 不要改成「先 SELECT 再 INSERT」：查询与插入之间的窗口就是竞态，
    并发重投时两条都会插入成功（A2 失效），而测试在单线程下**永远是绿的**。
    """
    statement = (
        pg_insert(model)
        .values(**dict(values))
        .on_conflict_do_nothing(index_elements=list(conflict_on))
        .returning(model.__mapper__.primary_key[0])
    )
    inserted = db.execute(statement).scalar_one_or_none()
    if inserted is not None:
        return inserted, True

    conditions = [getattr(model, column) == value for column, value in lookup.items()]
    existing = db.execute(
        select(model.__mapper__.primary_key[0]).where(*conditions)
    ).scalar_one_or_none()
    if existing is None:  # pragma: no cover - 只可能出现在「约束被删了」的异常环境
        raise ToolContractError(
            f"幂等插入冲突但回查不到既有行（{model.__tablename__}）："
            "唯一约束与回查条件不一致，请检查迁移与 lookup 参数"
        )
    return existing, False


__all__ = [
    "MAX_IDEMPOTENCY_KEY_CHARS",
    "insert_once",
    "validate_idempotency_key",
    "write_session",
]
