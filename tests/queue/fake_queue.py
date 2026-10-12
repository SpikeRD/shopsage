"""内存队列：**与 arq 语义对齐的离线实现**（测试与演示共用，不是「测试后门」）。

`12.queue.md` §7 的五条用例里，四条（延迟、重试→死信、重放、幂等）讲的是**队列语义**，
与「传输层是 Redis 还是内存」无关。本模块把 arq 的那部分语义在内存里实现一遍，
于是：

* 队列语义在**没有 Redis 的机器上**也能验（CI、新同学的第一次 `uv run pytest`）；
* 演示脚本能一条命令跑完全部场景（`scripts/queue_demo.py`）；
* 真 Redis 那一组用例（`test_queue_redis.py`，门控开关）只用来证明
  「我们对 arq 语义的理解是对的」—— 它验的是**理解**，不是**业务逻辑**。

## 与 arq 对齐的四条语义（**每一条都实测过**，不是猜的）

| 语义 | arq（Redis） | 本实现 |
|---|---|---|
| 重复 `_job_id` 入队 | `enqueue_job` 返回 `None`，**不覆盖**已有任务 | `enqueue()` 返回 `created=False` |
| `defer_by` 延迟 | 写进 ZSET 的 score，worker 到点才取 | `run_at` 时间戳，`drain()` 只取到点的 |
| `job_try` | `INCR arq:retry:<id>`，从 1 开始 | `JobRecord.try_no`，从 1 开始 |
| `Retry(defer=秒)` | 任务留在队列里、score 推后 | 重排 `run_at`，`try_no += 1` |
| `max_tries` 判定 | `job_try > max_tries` → **函数不被调用** | 同一条判据（复用它才能复现那个坑） |

⚠️ **`drain()` 是「跑一遍 worker」的等价物，而不是「等它跑完」**：
它只在**当前时刻**已经到点的任务上跑一轮（每个任务最多跑一次），
所以「延迟 3 秒的任务现在不会执行」这条断言是可靠且不睡 3 秒的。
要推进时间就调 `advance(seconds)`（显式的时间旅行，比 `sleep` 快且不 flaky）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.queue.backend import DEFAULT_QUEUE_NAME, EnqueueOutcome, QueueGauges
from app.queue.core import DeadLetterRecord, JobOutcome
from app.queue.jobs import JobSpec

#: 内存队列里的任务状态（与 arq 的 `JobStatus` 同名同义 + `dead_letter`）
JOB_QUEUED = "queued"
JOB_DEFERRED = "deferred"
JOB_COMPLETE = "complete"
JOB_DEAD = "dead_letter"

#: 任务**已经不在队列里**的状态（收场）。到了这里，同一个幂等键可以再投 ——
#: 这正是人工重放的工作方式（与 arq「失败即从队列移除」一致）。
_TERMINAL_STATUSES = frozenset({JOB_COMPLETE, JOB_DEAD})


@dataclass
class JobRecord:
    """队列里的一条任务（**内存里的 arq job 数据**）。"""

    job_id: str
    function: str
    kind: str
    payload: dict[str, Any]
    user_id: int | None = None
    try_no: int = 1
    #: 到点时间（单调时钟秒；`defer_by=0` 时就是入队那一刻）
    run_at: float = 0.0
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: 上一次「被安排到什么时候」（算退避实际延迟用）
    last_run_at: float | None = None
    status: str = JOB_QUEUED
    result: dict[str, Any] | None = None
    error: BaseException | None = None
    outcome: JobOutcome | None = None

    @property
    def deferred(self) -> bool:
        return self.status == JOB_DEFERRED


@dataclass
class RunRecord:
    """一次执行的记录（`drain()` 的返回，用例据此断言「跑了几次、等了几秒」）。"""

    job_id: str
    try_no: int
    status: str
    error: str = ""
    delay: float = 0.0
    value: Any = None


@dataclass
class DrainReport:
    """`drain()` 的结果：跑过哪些任务、各自的收场。"""

    runs: list[RunRecord] = field(default_factory=list)
    now: float = 0.0

    @property
    def executed(self) -> list[str]:
        return [item.job_id for item in self.runs]

    def statuses(self) -> list[str]:
        return [item.status for item in self.runs]

    def delays(self) -> list[float]:
        return [rounded for item in self.runs for rounded in [round(item.delay, 3)]]

    def summary(self) -> str:
        if not self.runs:
            return "本轮没有可执行的任务"
        return " | ".join(
            f"{item.job_id} try={item.try_no} {item.status}" + (f" (+{item.delay:.2f}s)" if item.delay else "")
            for item in self.runs
        )


class MemoryDeadLetterStore:
    """死信表的**内存实现**（接口与 `PostgresDeadLetterStore` 一致）。"""

    def __init__(self) -> None:
        self.records: dict[str, DeadLetterRecord] = {}
        #: 人为制造「写死信失败」——用来验「死信没落盘时必须吼出来」这条分支
        self.fail_writes = False
        self.write_calls = 0

    def record(self, entry: DeadLetterRecord) -> bool:
        self.write_calls += 1
        if self.fail_writes:
            raise RuntimeError("模拟：死信表写不进去（磁盘满/权限/连接断）")
        created = entry.job_id not in self.records
        self.records[entry.job_id] = entry
        return created

    def fetch(self, job_id: str) -> DeadLetterRecord | None:
        return self.records.get(job_id)

    def pending(self, *, limit: int = 50, include_resolved: bool = False) -> list[DeadLetterRecord]:
        items = [
            entry
            for entry in self.records.values()
            if include_resolved or not entry.resolution
        ]
        items.sort(key=lambda item: item.failed_at or datetime.now(timezone.utc))
        return items[: int(limit)]

    def count_pending(self) -> int:
        return len(self.pending(limit=10_000))

    def oldest_pending_age(self) -> float | None:
        pending = self.pending(limit=10_000)
        if not pending:
            return None
        oldest = min((item.failed_at for item in pending if item.failed_at), default=None)
        if oldest is None:
            return None
        moment = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())

    def resolve(self, job_id: str, *, resolution: str, replayed_job_id: str = "") -> bool:
        entry = self.records.get(job_id)
        if entry is None:
            return False
        entry.resolution = str(resolution)
        entry.resolved_at = datetime.now(timezone.utc)
        entry.replay_count += 1
        entry.replayed_job_id = str(replayed_job_id)
        return True

    def available(self) -> bool:
        return True


class MemoryJobQueue:
    """内存队列（`JobQueue` 端口的实现之一）。

    ⚠️ 它**不是**「假实现」：`app/queue/worker.bridge_job` 会直接调用它来处理任务，
    也就是说「重试 / 进死信 / 重放」这三条路径跑的是**生产代码**，
    只是传输层换成了内存（与第 8 步用 `InMemorySaver` 验断点重放是同一个思路）。
    """

    def __init__(
        self,
        *,
        queue_name: str = DEFAULT_QUEUE_NAME,
        dead_letters: MemoryDeadLetterStore | None = None,
        max_tries: int | None = None,
    ) -> None:
        self.queue_name = queue_name
        self.jobs: dict[str, JobRecord] = {}
        self.dead_letters = dead_letters or MemoryDeadLetterStore()
        #: 任务名 → 实现（`worker.bridge_job` 从这里取；默认走生产注册表）
        self.runners: dict[str, Any] = {}
        #: 人为制造失败：`{任务名: [(异常, 次数)]}`（次数用完后恢复正常）
        self.failures: dict[str, list[BaseException]] = {}
        #: 每个任务被真正"干活"了几次（幂等用例的关键断言）
        self.calls: dict[str, int] = {}
        #: `max_tries` 的默认值（与 `worker.MAX_ATTEMPTS` 同源：比最大额度多一档）
        self.max_tries = max_tries
        self._offset = 0.0

    # ── 队列端口 ─────────────────────────────────────────────────────────────
    async def enqueue(self, spec: JobSpec) -> EnqueueOutcome:
        """入队；**同 job_id 已存在（且还在队列里）时返回 `created=False`**。

        与 arq 的语义对齐（实测：重复 `_job_id` 时 `enqueue_job` 返回 `None`）：
        所谓「已存在」指的是**队列项还在**。任务一旦收场（跑完或失败被移除），
        同一个键就可以再投 —— 这正是**人工重放**的工作方式
        （`scripts/queue_replay.py` 用原幂等键重新入队同一条死信）。
        """
        job_id = spec.job_id
        existing = self.jobs.get(job_id)
        if existing is not None and existing.status not in _TERMINAL_STATUSES:
            return EnqueueOutcome(
                job_id=job_id,
                function=spec.function,
                kind=spec.kind,
                created=False,
                defer_by=spec.defer_by,
                queue_name=self.queue_name,
            )
        now = time.monotonic() + self._offset
        self.jobs[job_id] = JobRecord(
            job_id=job_id,
            function=spec.function,
            kind=spec.kind,
            payload=dict(spec.payload),
            user_id=spec.user_id,
            run_at=now + float(spec.defer_by or 0.0),
            status=JOB_DEFERRED if spec.defer_by else JOB_QUEUED,
        )
        return EnqueueOutcome(
            job_id=job_id,
            function=spec.function,
            kind=spec.kind,
            created=True,
            defer_by=spec.defer_by,
            queue_name=self.queue_name,
        )

    async def status(self, job_id: str) -> str:
        record = self.jobs.get(job_id)
        if record is None:
            return "dead_letter" if self.dead_letters.fetch(job_id) is not None else "not_found"
        if record.status == JOB_COMPLETE:
            return "complete"
        if record.status == JOB_DEAD:
            return "dead_letter"
        now = time.monotonic() + self._offset
        return "deferred" if record.run_at > now else "queued"

    async def result(self, job_id: str) -> Any:
        record = self.jobs.get(job_id)
        if record is None or record.result is None:
            from arq.jobs import ResultNotFound

            raise ResultNotFound(f"没有 {job_id} 的结果（未完成 / 已过期 / 从未入队）")
        return record.result

    async def owner(self, job_id: str) -> int | None:
        """任务归属（与 `RedisJobQueue.owner` 同一语义：从入队参数里读）。"""
        record = self.jobs.get(job_id)
        if record is None:
            return None
        return record.user_id if record.user_id is not None else None

    async def depth(self) -> int:
        now = time.monotonic() + self._offset
        return sum(
            1
            for record in self.jobs.values()
            if record.status == JOB_QUEUED and record.run_at <= now
        )

    async def gauges(self) -> QueueGauges:
        now = time.monotonic() + self._offset
        deferred = sum(
            1
            for record in self.jobs.values()
            if record.status == JOB_DEFERRED and record.run_at > now
        )
        return QueueGauges(
            depth=await self.depth(),
            deferred=deferred,
            dead_letters=self.dead_letters.count_pending(),
            oldest_dead_letter_seconds=self.dead_letters.oldest_pending_age(),
        )

    async def aclose(self) -> None:
        return None

    # ── 「跑一轮 worker」 ─────────────────────────────────────────────────────
    def reset(self) -> None:
        """清空队列状态（任务、计数、注入的失败），**保留死信表与 runner**。

        用途：演示脚本要在**同一个进程**里连续跑七个场景，而上一个场景留下的
        同键任务（`demo:dlq:fatal` 之类）会让下一个场景的 `enqueue` 变成幂等命中 ——
        于是「这次应当新建任务」的断言会莫名其妙地失败。
        死信表刻意**不清**（它是真库时清不动，而且「跨场景积累」正是要观察的东西）。
        """
        self.jobs.clear()
        self.calls.clear()
        self.failures.clear()
        self._offset = 0.0

    def advance(self, seconds: float) -> float:
        """把队列的时钟向前拨 `seconds`（延迟任务用例用它，而不是 `sleep`）。"""
        self._offset += float(seconds)
        return self._offset

    @property
    def now(self) -> float:
        return time.monotonic() + self._offset

    def due(self) -> list[JobRecord]:
        """**当前时刻已经到点**的任务（按到点时间升序 —— 与 arq 的 ZSET 同序）。"""
        moment = self.now
        items = [
            record
            for record in self.jobs.values()
            if record.status in (JOB_QUEUED, JOB_DEFERRED) and record.run_at <= moment
        ]
        items.sort(key=lambda item: item.run_at)
        return items

    async def drain(self, *, limit: int = 100) -> DrainReport:
        """跑一轮（等价于 worker 的一次轮询）：每个到点任务最多执行一次。"""
        from app.queue.worker import MAX_ATTEMPTS, bridge_job

        report = DrainReport(now=self.now)
        for record in self.due()[: int(limit)]:
            attempt = record.try_no
            record.last_run_at = record.run_at
            ctx: dict[str, Any] = {
                "job_id": record.job_id,
                "job_try": attempt,
                "function": record.function,
                "enqueue_time": record.enqueued_at,
                "dead_letters": self.dead_letters,
            }
            enabled = MAX_ATTEMPTS if self.max_tries is None else int(self.max_tries)
            if attempt > enabled:
                # 与 arq 的 `run_job` 同一条判据：**超限时函数根本不会被调用**。
                # 本行刻意保留这个分支：它是「为什么 worker 的 max_tries 必须比
                # 重试额度多一档」那条结论的可复现版本（见 worker.py 的 docstring）。
                record.status = JOB_DEAD
                report.runs.append(RunRecord(record.job_id, attempt, "max_tries_exceeded"))
                continue

            runner = self._runner_for(record.function)
            try:
                from arq.worker import Retry

                value = await bridge_job(ctx, self._wire_payload(record), runner=runner, dead_letters=self.dead_letters)
            except Retry as retry:
                record.try_no = attempt + 1
                record.run_at = self.now + float(retry.defer_score or 0) / 1000.0
                record.status = JOB_DEFERRED
                report.runs.append(
                    RunRecord(record.job_id, attempt, "retry", delay=float(retry.defer_score or 0) / 1000.0)
                )
                continue
            except BaseException as exc:  # noqa: BLE001 - 与 arq 一样：失败被记下来，不炸掉 worker
                outcome = ctx.get("job_outcome")
                record.error = exc
                record.outcome = outcome if isinstance(outcome, JobOutcome) else None
                record.status = JOB_DEAD
                record.result = self._failure_payload(record)
                report.runs.append(RunRecord(record.job_id, attempt, "failed", error=type(exc).__name__))
                await self.resolve_replayed(record, record.outcome)
                continue

            outcome = ctx.get("job_outcome")
            record.outcome = outcome if isinstance(outcome, JobOutcome) else None
            record.result = dict(value)
            record.status = JOB_COMPLETE
            report.runs.append(RunRecord(record.job_id, attempt, "complete", value=record.result))
            await self.resolve_replayed(record, record.outcome)
        return report

    async def run_until_idle(self, *, max_rounds: int = 20) -> list[DrainReport]:
        """连续跑，直到没有到点的任务（**不**推进时间：延迟任务会留在队列里）。

        为什么要有这个：重试会把任务推到「未来」，于是「重试 3 次才进死信」这件事
        需要「跑一轮 → 时间前进到重投点 → 再跑一轮」。本方法只负责「跑到没得跑」，
        推进时间由用例显式 `advance()`（显式的时间旅行比 `sleep` 快且不 flaky）。
        """
        rounds: list[DrainReport] = []
        for _ in range(int(max_rounds)):
            report = await self.drain()
            if not report.runs:
                break
            rounds.append(report)
        return rounds

    # ── 内部 ─────────────────────────────────────────────────────────────────
    def _runner_for(self, function: str) -> Any:
        """取任务实现：用例注册的优先，否则走生产注册表。"""
        if function in self.runners:
            return self.runners[function]
        from app.queue.worker import runner_for

        return runner_for(function)

    def _wire_payload(self, record: JobRecord) -> dict[str, Any]:
        """造 arq 形状的 payload（与 `tasks.build_spec` 入队时塞的一致）。"""
        return {
            "function": record.function,
            "kind": record.kind,
            "user_id": record.user_id,
            "payload": dict(record.payload),
        }

    def inject_failure(self, function: str, *errors: BaseException) -> None:
        """让某个任务的下 N 次执行抛错（第 N+1 次恢复）。

        用法（重试→死信用例）：

        ```python
        queue.inject_failure(FUNC_REPORT, *[Boom("下游 503")] * 3)
        ```
        """
        self.failures.setdefault(function, []).extend(errors)

    def take_failure(self, function: str) -> BaseException | None:
        """取出下一次要抛的异常（没有就返回 `None`）。"""
        items = self.failures.get(function) or []
        if not items:
            return None
        return items.pop(0)

    def count_calls(self, function: str) -> int:
        return int(self.calls.get(function, 0))

    def bump_calls(self, function: str) -> int:
        self.calls[function] = self.count_calls(function) + 1
        return self.calls[function]

    async def resolve_replayed(self, record: JobRecord, outcome: JobOutcome | None) -> None:
        """重放成功时关闭原死信（**与 `worker.after_job_end_hook` 同一件事**）。

        ⚠️ 这里直接**调用生产的那一个钩子**而不是复刻一份逻辑：
        内存队列没有 arq 的生命周期钩子，而「重放成功 → 关掉死信」这条行为
        必须与生产完全一致。所以实现只有一处（`worker.after_job_end_hook`），
        本方法只是「在内存里替 arq 调它一次」（连同 `job_payload` 里那个 `dlq_key` 约定）。
        """
        from app.queue.worker import after_job_end_hook

        await after_job_end_hook(
            {
                "job_outcome": outcome,
                "job_payload": dict(record.payload),
                "dead_letters": self.dead_letters,
            }
        )

    def _failure_payload(self, record: JobRecord) -> dict[str, Any]:
        """失败的结果（形状与 `worker._result_payload` 一致，**由真实现生成**）。"""
        from app.queue.worker import _result_payload

        outcome = record.outcome
        if outcome is None:  # pragma: no cover - 极端情况：异常没有走到 execute_job
            from app.queue.core import JobOutcome

            outcome = JobOutcome(job_id=record.job_id, status="dead_letter", attempts=record.try_no)
        return _result_payload(outcome, user_id=record.user_id)

    def make_runner(self, function: str, body: Any) -> Any:
        """造一个「会记账、会按注入的失败列表抛错」的 runner。

        记账（`calls`）是幂等用例的关键断言：`test_idempotent_enqueue` 要证明的是
        「同一个幂等键不重复执行」，而「执行了几次」只有 runner 自己知道。
        """

        async def runner(payload: Mapping[str, Any]) -> Any:
            self.bump_calls(function)
            failure = self.take_failure(function)
            if failure is not None:
                raise failure
            result = body(dict(payload))
            if hasattr(result, "__await__"):
                result = await result
            return result

        return runner

    def enqueued_job_ids(self) -> list[str]:
        """当前还在队列里的任务（排障/断言用；已完成的不在其中）。"""
        return sorted(job_id for job_id, record in self.jobs.items() if record.status != JOB_COMPLETE)


def deadline(seconds: float) -> datetime:
    """造一个「`seconds` 秒之后」的时间戳（延迟任务用例用；与 arq 的 `_defer_by` 等价）。"""
    return datetime.now(timezone.utc) + timedelta(seconds=float(seconds))


__all__ = [
    "DrainReport",
    "JOB_COMPLETE",
    "JOB_DEAD",
    "JOB_DEFERRED",
    "JOB_QUEUED",
    "JobRecord",
    "MemoryDeadLetterStore",
    "MemoryJobQueue",
    "RunRecord",
    "deadline",
]
