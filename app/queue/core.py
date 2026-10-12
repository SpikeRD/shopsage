"""队列语义的**内核**：重试分级、死信兜底、人工重放（`12.queue.md` §4 第 3、4 条）。

本模块**刻意不 import arq、不连 Redis、不碰数据库** —— 它只回答三个问题：

1. 这个函数失败了，**该不该重试**（`classify`）；
2. 重试次数用完了，**怎么办**（`execute_job` 的第 4 条分支：进死信 + 留在队列里等人工）；
3. 死信里的任务，**怎么重放**（`replay_entry`）。

为什么这三件事必须与 arq 解耦（本步最重要的一个结构决定）：

* **它们能被离线验证**。`12.queue.md` §7 的五条用例里有四条（重试→死信、重放、
  延迟、幂等）都不需要真 Redis —— 只要内核与传输层分开，「连续失败 3 次进死信」
  就能在内存里跑完（与第 7、8 步「注入假依赖」是同一条纪律）；
* **换队列实现时它们不变**。arq / Celery / SQS 的差别在「怎么取任务」，
  而「失败几次算放弃」「放弃之后谁来兜」是**业务策略**，不该跟着传输层漂；
* **判定逻辑只有一处**。`app/tools/errors.py` 与 `app/llm/errors.py` 已经分别定义了
  「可重试」的白名单，队列这一层只做**归并**（`classify`），不重新发明一套分类。

## 两条铁律

**铁律一：死信先落盘，任务才允许离开队列。**

「重试上限用尽」与「不可恢复错误」这两种收场，都必须先写出一条**死信记录**
（Postgres，见 `app/queue/store.py`），再决定这个 job 的最终去向。顺序反了就会出现
「任务从 Redis 消失、死信表里也没有」的窗口 —— 那就是**静默丢任务**，
而 `12.queue.md` §8 的验收写的是「零任务丢失」。

**铁律二：不可恢复错误**不**重投队列，而是留一条死信 + 让 arq 记录失败结果。**

把「参数写错」也重投，等于让同一个错误在队列里被犯 N 次（还会把重试额度浪费掉）；
但**直接抛出去又不行** —— arq 会把它记成 job failed 并把任务从队列里删掉，
从此没有任何地方知道「有一个任务永久失败了」。所以这一类的收场是
「先写死信（可见、可重放），再把异常抛给 arq（让重试计数与失败指标也对上）」。
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Protocol

from app.llm.errors import (
    LLMError,
    RetryableLLMError,
    is_permanent,
    is_retryable,
)
from app.observability.metrics import QUEUE_DEPTH
from app.queue.jobs import RetryPolicy
from app.tools.errors import PermanentToolError, RetryableToolError, ToolError

logger = logging.getLogger(__name__)

#: 任务自报的重试提示（任务实现里 `raise RetryTask(reason="下游 429", defer=30)`）。
#:
#: 为什么要有这个类型（而不是「让任务直接 raise RetryableToolError」）：
#: 任务自己最清楚「等多久合适」（限流头里就写着 `Retry-After`）。
#: 本模块把「该不该重试」（由错误类型决定）与「等多久」（由任务提示 + 退避策略决定）分开，
#: 于是「任务知道得更准」时不必去改全局策略。
class RetryTask(RuntimeError):
    """任务要求**明确的重投**（可带自定义延迟）。"""

    def __init__(self, message: str, *, defer: float | None = None) -> None:
        super().__init__(message)
        #: 秒；`None` = 用策略算出来的退避值
        self.defer = defer


class PermanentTaskError(RuntimeError):
    """任务自报的**不可恢复**失败（参数错、业务规则不允许）。"""


class DeadLetterStore(Protocol):
    """死信落盘端口（实现见 `app/queue/store.py`：Postgres）。

    刻意做成一个 `Protocol` 而不是「直接用 SQLAlchemy」：
    离线用例需要**内存实现**，而内存实现必须与真实现是同一个接口 —— 否则
    「离线绿、真库红」会变成常态（第 8 步的 `memory` checkpointer 是同一个思路）。
    """

    def record(self, entry: "DeadLetterRecord") -> bool:
        """写一条死信记录；**已存在同 job_id 时返回 False**（幂等，不覆盖失败现场）。"""
        ...

    def fetch(self, job_id: str) -> "DeadLetterRecord | None":
        """按 job_id 取一条（重放脚本用）。"""
        ...

    def pending(self, *, limit: int = 50) -> list["DeadLetterRecord"]:
        """按失败时间取出待处理的一批（`failed_at` 升序 —— 先失败的先处理）。"""
        ...

    def resolve(self, job_id: str, *, resolution: str, replayed_job_id: str = "") -> bool:
        """标记一条死信已被处理（重放成功 / 人工关闭）。"""
        ...


class QueueMetrics(Protocol):
    """指标端口（`11.observability.md` 的 `Metrics` 与 `MetricsRegistry` 都满足它）。"""

    def counter(self, name: str, labels: Mapping[str, str] | None = None, *, value: float = 1.0) -> None: ...

    def observe(self, name: str, seconds: float, labels: Mapping[str, str] | None = None) -> None: ...

    def gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None: ...


@dataclass
class DeadLetterRecord:
    """一条死信：**完整的失败现场**（`12.queue.md` §4 第 4 条）。

    每个字段都有它救回来的那一次排障：

    | 字段 | 回答的问题 |
    |---|---|
    | `job_id` | 这是哪个任务（也是重放的幂等键） |
    | `function` / `kind` / `payload` | **重放时怎么把它再跑一遍** |
    | `attempts` | 试了几次（区分「一次就永久失败」与「三次都恢复不了」） |
    | `error_type` / `error_message` | 出了什么错（可聚类：「全是 429」还是「全是 KeyError」） |
    | `error_traceback` | 出错在哪一行（**只有这里能看到**，因为 arq 的失败结果会过期） |
    | `retryable` | 这是「运气不好」还是「代码坏了」（决定该不该重放） |
    | `policy` | 当时用的是哪套策略（策略改了之后回看旧记录仍然说得通） |
    """

    job_id: str
    function: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    error_type: str = ""
    error_message: str = ""
    error_traceback: str = ""
    retryable: bool = False
    policy: RetryPolicy | None = None
    enqueued_at: datetime | None = None
    failed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    user_id: int | None = None
    replay_count: int = 0
    replayed_job_id: str = ""
    resolution: str = ""
    resolved_at: datetime | None = None

    @property
    def replay_key(self) -> str:
        """重放时使用的幂等键（= 原 job_id 去掉前缀）。

        ⚠️ 重放**必须复用原键**（而不是造一个新的）：否则「重放两次」会变成
        「跑两个任务」，而写路径的副作用就重复了（A2）。
        复用原键还让 `enqueue` 天然具备「重放中的任务不会被第二次重放挤掉」的性质。
        """
        return self.job_id[2:] if self.job_id.startswith("q_") else self.job_id


# ── 错误分类（归并两层白名单，只此一处）──────────────────────────────────────


def classify(exc: BaseException) -> bool:
    """这个异常**值不值得重投队列**。

    判定顺序（**顺序即语义**）：

    1. `RetryTask` → 可重试（任务自己要求的，最权威）；
    2. `PermanentTaskError` / `PermanentToolError` / `PermanentLLMError` /
       `ToolContractError` 等永久类型 → 不可重试；
    3. `RetryableToolError` / `RetryableLLMError` → 可重试；
    4. 其余 `ToolError` / `LLMError` → 不可重试（两层的默认方向都是「保守」）；
    5. **没见过的异常** → 不可重试。

    第 5 条是本模块最容易被写反的一处：`KeyError` / `TypeError` 这类
    「代码有 bug」的异常重投 100 次结果一样，只会让同一个 bug 被犯 100 次
    （还会把重试额度耗光，让真正「运气不好」的任务排在它后面）。
    「重试上限 + 死信」的分工正是为了这一刻：**代码坏了要有人看见**，
    而死信队列就是那个「看见」的地方。

    ⚠️ `asyncio.CancelledError` 在 Python 3.8+ 继承 `BaseException`，
    不会走到这里（arq 自己按「可重试」处理 worker 关停时的取消）—— 这是要的：
    部署重启不该把一个任务判成永久失败。
    """
    if isinstance(exc, RetryTask):
        return True
    if isinstance(exc, PermanentTaskError):
        return False
    if isinstance(exc, PermanentToolError):
        return False
    if isinstance(exc, ToolError):
        return isinstance(exc, RetryableToolError)
    if isinstance(exc, LLMError):
        # `is_permanent` / `is_retryable` 是第 5 步的白名单判定（那里的真相），
        # 这里只做归并：先排显式永久，再看白名单 —— 顺序与 `app/llm/errors.py` 一致。
        if is_permanent(exc):
            return False
        return isinstance(exc, RetryableLLMError) or is_retryable(exc)
    return False


def summarize_error(exc: BaseException, *, limit: int = 60) -> str:
    """一行错误摘要（日志与指标标签用；**不含 traceback**）。

    为什么要单独一个函数：指标标签里塞 traceback 会让**基数爆炸**
    （每一条 traceback 都是一条新时间序列），而日志里塞全文又会让一行变几十行。
    两处的需求不同，所以「摘要」与「全文」是两个出口（全文进死信表的 `error_traceback`）。
    """
    text = " ".join(str(exc).split())
    head = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return head[:limit]


def traceback_text(exc: BaseException, *, limit: int = 4000) -> str:
    """异常的完整栈（进死信表；截断到 `limit` 字符防一条记录撑爆一页）。"""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return text[:limit]


_TERMINAL: Final[frozenset[str]] = frozenset({"succeeded", "dead_letter", "permanent_failure"})


def make_dead_letter_record(
    *,
    job_id: str,
    function: str,
    kind: str,
    payload: Mapping[str, Any],
    exc: BaseException,
    attempts: int,
    policy: RetryPolicy,
    retryable: bool,
    enqueued_at: datetime | None = None,
    user_id: int | None = None,
) -> DeadLetterRecord:
    """把一次失败的现场收成一条死信记录（唯一的组装点）。"""
    return DeadLetterRecord(
        job_id=job_id,
        function=function,
        kind=kind,
        payload=dict(payload or {}),
        attempts=int(attempts),
        error_type=type(exc).__name__,
        error_message=" ".join(str(exc).split())[:2000],
        error_traceback=traceback_text(exc),
        retryable=bool(retryable),
        policy=policy,
        enqueued_at=enqueued_at,
        failed_at=datetime.now(timezone.utc),
        user_id=user_id,
    )


# ── 任务执行内核 ─────────────────────────────────────────────────────────────


@dataclass
class JobOutcome:
    """一次任务执行的最终结论（**调用方只看这个对象**）。"""

    job_id: str
    status: str  # succeeded / retry_planned / dead_letter / permanent_failure
    attempts: int
    value: Any = None
    error: BaseException | None = None
    dead_letter: DeadLetterRecord | None = None
    #: 计划重投的延迟（秒）；`status="retry_planned"` 时有值
    retry_in: float | None = None

    @property
    def accepted(self) -> bool:
        """这次执行是否算「收场了」（成功或已进死信）。"""
        return self.status in _TERMINAL

    @property
    def retrying(self) -> bool:
        return self.status == "retry_planned"

    def summary(self) -> str:
        tail = f" in {self.retry_in:.2f}s" if self.retry_in is not None else ""
        err = f" | {summarize_error(self.error)}" if self.error is not None else ""
        return f"{self.job_id} {self.status} (attempt {self.attempts}){tail}{err}"


class RetryPlanned(Exception):
    """内部信号：这次不执行完就返回，让传输层把任务**带延迟**放回队列。

    ⚠️ 是**信号**不是**错误**：`execute_job` 捕获它并翻成 `JobOutcome(status="retry_planned")`，
    所以它会经 `callable(return_value)` 回到 arq 的包装层（`worker.py`），
    由那里 raise `arq.worker.Retry(defer=...)` —— 见 `worker.py` 为什么把
    「内核结论」与「arq 动作」分成两层。
    """

    def __init__(self, defer: float, message: str) -> None:
        super().__init__(message)
        self.defer = float(defer)


async def execute_job(
    *,
    job_id: str,
    function: str,
    kind: str,
    payload: Mapping[str, Any],
    attempt: int,
    run: Callable[[], Awaitable[Any]],
    policy: RetryPolicy,
    dead_letters: DeadLetterStore | None = None,
    metrics: QueueMetrics | None = None,
    enqueued_at: datetime | None = None,
    user_id: int | None = None,
) -> JobOutcome:
    """执行一个任务，并按「重试分级 + 死信兜底」收场（内核的**唯一**执行入口）。

    四条收场路径（每一条都对应 `12.queue.md` 的一句验收）：

    | 情况 | 收场 | 为什么 |
    |---|---|---|
    | 成功 | `succeeded` | 正常路径；顺带记指标与耗时 |
    | 可重试错误，还有额度 | `retry_planned(defer)` | 退避之后再跑，**不**进死信（那会让死信被噪音灌满） |
    | 可重试错误，额度用尽 | `dead_letter`（先落盘） | A1：失败变成**可见、可人工兜底的事件** |
    | 不可恢复错误 | `dead_letter`（先落盘） | 参数错/代码 bug 重试无意义，但**必须留痕** |

    `run` 是**零参数可等待对象**（不是「函数 + 参数」）：这样本模块不必知道
    任务函数的签名（payload 怎么展开是 `tasks.py` 的事），也就不会被任务签名绑住。

    ⚠️ `dead_letters=None` 时**不抛错**，只是退化成「只记日志」：离线跑图/演示
    不该因为没接库就跑不起来。但生产里 `worker.py` **一定**会注入它 ——
    那是「零任务丢失」这条验收的实现位置，缺了它这条验收就没有落点（`worker.py`
    启动时会检查并告警）。
    """
    started = time.perf_counter()
    try:
        value = await run()
    except BaseException as exc:  # noqa: BLE001 - 这里就是分类与收场的位置
        retryable = classify(exc)
        labels = {"function": function, "kind": kind}
        if metrics is not None:
            metrics.counter("shopsage_queue_job_failures_total", {**labels, **_retry_label(retryable)})

        if retryable and policy.should_retry(attempt):
            defer = _defer_for(exc, policy, attempt)
            logger.warning(
                "任务第 %d/%d 次失败（可恢复），%.2fs 后重投：%s | %s",
                attempt,
                policy.attempts,
                defer,
                job_id,
                summarize_error(exc),
            )
            if metrics is not None:
                metrics.counter("shopsage_queue_retries_total", labels)
            raise RetryPlanned(defer, summarize_error(exc)) from exc

        reason = "exhausted" if retryable else "permanent"
        logger.error(
            "任务%s（第 %d/%d 次）：%s | %s",
            "重试额度用尽" if retryable else "遇到不可恢复错误",
            attempt,
            policy.attempts,
            job_id,
            summarize_error(exc),
        )
        entry = make_dead_letter_record(
            job_id=job_id,
            function=function,
            kind=kind,
            payload=payload,
            exc=exc,
            attempts=attempt,
            policy=policy,
            retryable=retryable,
            enqueued_at=enqueued_at,
            user_id=user_id,
        )
        written = _write_dead_letter(dead_letters, entry, metrics=metrics)
        if not written:
            # 死信没落盘：**不能静默**。此时任务仍在 Redis 里（调用方会抛异常让 arq
            # 记失败），但「可见性」这条保证已经破了 —— 必须吼出来。
            logger.error(
                "死信落盘失败，任务 %s 只存在于 Redis 的失败结果里（重放脚本找不到它）：原因=%s",
                job_id,
                reason,
            )
        return JobOutcome(
            job_id=job_id,
            status="dead_letter",
            attempts=attempt,
            error=exc,
            dead_letter=entry if written else None,
        )

    elapsed = time.perf_counter() - started
    if metrics is not None:
        metrics.counter("shopsage_queue_jobs_total", {"function": function, "kind": kind, "status": "succeeded"})
        metrics.observe("shopsage_queue_job_seconds", elapsed, {"function": function})
        _record_depth(metrics, None)
    logger.info("任务完成：%s（第 %d 次，%.2fs）", job_id, attempt, elapsed)
    return JobOutcome(job_id=job_id, status="succeeded", attempts=attempt, value=value)


def _defer_for(exc: BaseException, policy: RetryPolicy, attempt: int) -> float:
    """这次重投该等多久：**任务自己的提示优先**，否则用策略算出的退避。"""
    if isinstance(exc, RetryTask) and exc.defer is not None:
        return max(0.0, float(exc.defer))
    return policy.delay_for(attempt)


def _retry_label(retryable: bool) -> dict[str, str]:
    return {"retryable": "true" if retryable else "false"}


def _write_dead_letter(
    store: DeadLetterStore | None,
    entry: DeadLetterRecord,
    *,
    metrics: QueueMetrics | None,
) -> bool:
    """把死信写下去；返回**是否成功**。

    ⚠️ 写入失败**不抛**：此刻已经处在一个失败路径上，再抛一个「写死信失败」的异常
    会盖掉原始错误（排障时最想看到的那个），而且 arq 会把两次失败混成一次。
    所以这里只返回 False，由调用方决定怎么吼（见 `execute_job`）。
    """
    if store is None:
        logger.error(
            "没有配置死信表（DeadLetterStore 为 None）：任务 %s 的失败现场只留在日志里，"
            "无法人工重放。生产环境必须注入 Postgres 实现（见 app/queue/worker.py）",
            entry.job_id,
        )
        return False
    try:
        created = store.record(entry)
    except Exception as exc:  # noqa: BLE001 - 死信写入失败不该盖掉原始错误
        logger.error("写死信记录失败（job_id=%s）：%s", entry.job_id, exc)
        return False
    if metrics is not None:
        metrics.counter(
            "shopsage_queue_dead_letter_total",
            {"function": entry.function, "kind": entry.kind, "reason": "exhausted" if entry.retryable else "permanent"},
        )
        _record_depth(metrics, None)
    logger.error(
        "已进死信队列：job_id=%s attempts=%d 类型=%s（%s）",
        entry.job_id,
        entry.attempts,
        entry.error_type,
        "连续失败" if entry.retryable else "不可恢复",
    )
    return bool(created)


def _record_depth(metrics: QueueMetrics, value: float | None) -> None:
    """把队列深度写成 gauge（`None` = 不写）。

    ⚠️ 每次任务结束都**不知道**真实积压（那要问 Redis），所以这里刻意不做估算 ——
    一个「猜出来的深度」比没有深度更糟：它会让扩容决策建立在假数字上。
    真实深度由 `RedisJobQueue.depth()` 在探活/演示时写入（`scale_signal()`）。
    """
    if value is None:
        return
    metrics.gauge(QUEUE_DEPTH, value)


# ── 人工重放 ─────────────────────────────────────────────────────────────────


@dataclass
class ReplayPlan:
    """一次重放的**纯计算**结果（不产生副作用，便于单测与干跑）。"""

    entry: DeadLetterRecord
    job_id: str
    idempotency_key: str
    function: str
    kind: str
    payload: dict[str, Any]
    defer_by: float = 0.0

    def describe(self) -> str:
        return (
            f"{self.job_id} → {self.function}（原第 {self.entry.attempts} 次失败，"
            f"重放第 {self.entry.replay_count + 1} 次）"
        )


def replay_plan(entry: DeadLetterRecord, *, defer_by: float = 0.0, key_suffix: str = "") -> ReplayPlan:
    """算出一条死信记录的**重放入参**。

    两个刻意的决定：

    1. **默认复用原幂等键** → 重放两次只会跑一次（写路径的副作用不会重复，A2）；
    2. 但**允许一次显式重跑**（`key_suffix`）：有些失败是「原始输入就是错的」，
       人工修好参数之后确实需要再跑一次。此时 key 是 `<原键>:retry2`（递增后缀），
       于是「同一条死信被点了几次」在 Redis 的 job_id 上是**看得见**的。
       ⚠️ 后缀同样要过 `normalize_key`（有长度上限），所以不能无限叠加：
       达到上限时抛错并要求人工处理（一个被重放 100 次的任务该被修，而不是再点一次）。
    """
    from app.queue.jobs import normalize_key

    key = entry.replay_key
    if key_suffix:
        key = normalize_key(f"{key}:{key_suffix}")
    payload = dict(entry.payload or {})
    # 死信记录里带着上一次的 job_id 与重放次数（见 worker 的 `dlq_key` 约定）：
    # 重放时**必须**让新任务知道「我在补哪一条」，否则成功之后没人去关掉那条死信。
    payload["dlq_key"] = entry.job_id
    payload["replay_of"] = entry.job_id
    return ReplayPlan(
        entry=entry,
        job_id=f"q_{key}",
        idempotency_key=key,
        function=entry.function,
        kind=entry.kind,
        payload=payload,
        defer_by=float(defer_by),
    )


def render_entry(entry: DeadLetterRecord) -> str:
    """一行可读的死信摘要（CLI 列表与文档示例共用，避免两处各拼一遍）。"""
    when = entry.failed_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if entry.failed_at else "?"
    return (
        f"{entry.job_id:<48} {entry.kind:<10} attempts={entry.attempts} "
        f"{entry.error_type:<24} retryable={str(entry.retryable):<5} {when} {summarize_error_str(entry)}"
    )


def summarize_error_str(entry: DeadLetterRecord, *, limit: int = 48) -> str:
    text = entry.error_message or entry.error_type
    return text[:limit]


def dump_payload(payload: Mapping[str, Any]) -> str:
    """payload 的可读渲染（CLI 用；**不打印可能含 PII 的字段全文**）。"""
    return json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True)[:400]


def pending_sequence(entries: Sequence[DeadLetterRecord]) -> list[str]:
    """待处理死信的 job_id 序列（`--all` 重放用；顺序 = 失败时间升序）。"""
    return [entry.job_id for entry in entries]


__all__ = [
    "DeadLetterRecord",
    "DeadLetterStore",
    "JobOutcome",
    "PermanentTaskError",
    "QueueMetrics",
    "ReplayPlan",
    "RetryPlanned",
    "RetryTask",
    "classify",
    "dump_payload",
    "execute_job",
    "make_dead_letter_record",
    "pending_sequence",
    "render_entry",
    "replay_plan",
    "summarize_error",
    "summarize_error_str",
    "traceback_text",
]
