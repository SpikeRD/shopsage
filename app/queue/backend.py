"""队列传输层：**入队、查状态、看积压**（arq + Redis）。

本模块是 `app/queue/` 里**唯一**知道 arq 与 Redis 存在的地方。这样分层的收益在
`app/queue/core.py` 的 docstring 里写过（内核可离线验证、换传输层不改策略），
代价是这里必须把 arq 的 API 用法讲清楚 —— 下面每一条都是**实测**出来的语义。

## 实测记下来的 arq 语义（写错任何一条都会静默出错）

| 行为 | 实测结论 | 影响 |
|---|---|---|
| `enqueue_job(..., _job_id=X)` 重复调用 | 返回 `None`（**不抛错**、也不覆盖已有任务） | 幂等入队的实现基础：`None` = 「已入队，没重复投」 |
| 延迟任务 | `_defer_by=秒` 写进 Redis 的**有序集合**（score = 到点时间），worker 到点才取 | 定时/延迟任务不用另建机制 |
| job 数据存哪 | `arq:job:<id>`（String）、队列 `arq:queue`（ZSET）、结果 `arq:result:<id>` | 「积压多少」= `zcard`；「有没有在等」= `zscore` |
| 结果保留 | 默认 24 小时（`RESULT_TTL_SECONDS`） | 「第二天再捞结果」可行，但**不能**当长期存储（死信表才是） |
| 重试 | 函数里 `raise Retry(defer=秒)`；第 `_job_try` 次由 arq 自己计数 | 见 `worker.py`：我们把它包了一层，好让「重试额度用尽」也能落死信 |

## 为什么入队前要显式检查「任务是否已存在」

arq 的重复入队返回 `None` 这件事**只覆盖「还在队列里」的情形**。任务跑完并从队列里
移除之后，同一个 `_job_id` 是**可以被再次入队**的（结果还留着，但队列项没了）。
于是「同一个幂等键投两次」在任务已经跑完时会变成**跑第二次** —— 而对客路径上
那意味着重复的模型调用（C1 成本）与潜在的重复副作用。

所以 `enqueue()` 的顺序是：**先查状态**（`Job.status()`）→ 已存在过（在队列里 /
在跑 / 已完成 / 有结果）就直接返回「已存在」，不投。多出来的一次 Redis 往返
买的是「幂等键真的幂等」—— 而这正是 A1/A2 那条验收要的东西。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Literal, Protocol

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.jobs import Job, JobStatus
from redis.exceptions import RedisError

from app.queue.jobs import RESULT_TTL_SECONDS, JobSpec

logger = logging.getLogger(__name__)

#: 队列名。**它本身就是 Redis 的 key**（`arq` 直接用这个名字当 ZSET 的键，
#: 默认值是 `arq:queue` —— 实测 `arq.constants.default_queue_name`），
#: 所以这里写成 `arq:shopsage:default` 而不是「再叠一层前缀」。
#:
#: ⚠️ 它是一个**部署参数**而不是纯常量：`redis_url` 的 db 号决定「用哪一套 Redis」，
#: 队列名决定「同一个 Redis 里的哪条队列」。多环境共用一个实例时靠它隔离 ——
#: 而**不**靠给 key 加前缀（那要改 arq 的内部约定，升级时会静默失效）。
DEFAULT_QUEUE_NAME: Final[str] = "arq:shopsage:default"

#: 状态取值域（比 arq 的 `JobStatus` 多两个：`unknown` 与 `dead_letter`）。
#:
#: 为什么要在 arq 之外多两个：
#:
#: * `unknown` —— Redis 里查不到任何痕迹（没投过、或结果已过期）。**不能**报成 `not_found`
#:   就完事：调用方要能区分「这个 id 从来不存在」（可能是拼错了）与「它跑完了但结果过期了」；
#: * `dead_letter` —— 任务的终局不在 arq 里（在死信表里）。状态接口把它并进来，
#:   前端才有一个「这个长任务到底怎么样了」的完整答案。
JobState = Literal["queued", "deferred", "in_progress", "complete", "not_found", "unknown", "dead_letter"]


class QueueUnavailable(RuntimeError):
    """队列后端（Redis）不可用。

    单独一个类型：入队失败是**可用性**问题（HTTP 层要回 503 + `Retry-After`），
    而「任务失败」是业务问题（进死信、人工兜底）。两者混成一个 500 会让
    「Redis 挂了」看起来像「报表算不出来」。
    """


@dataclass
class EnqueueOutcome:
    """一次入队的结果（`enqueue()` 的返回）。"""

    job_id: str
    function: str
    kind: str
    #: 本次调用**是否真的投了一个新任务**（False = 幂等命中，之前已经投过）
    created: bool
    defer_by: float = 0.0
    queue_name: str = DEFAULT_QUEUE_NAME
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def status(self) -> JobState:
        return "deferred" if self.defer_by > 0 else "queued"

    def summary(self) -> str:
        verb = "已入队" if self.created else "幂等命中（未重复入队）"
        tail = f" 延迟 {self.defer_by:.1f}s" if self.defer_by > 0 else ""
        return f"{verb}：{self.kind} job_id={self.job_id}{tail}"


class JobQueue(Protocol):
    """队列端口（**入队与查询**两件事；任务怎么跑是 `worker.py` 的事）。

    ⚠️ 五个方法都要在**内存实现**里也有一份（`tests/queue/fake_queue.py`）：
    「离线可测」这条纪律要求端口足够小 —— 每加一个方法，内存实现就多一处可能与
    真实现漂移的地方。`result()` 与 `gauges()` 之所以值得进端口，是因为
    `GET /api/tasks/{id}` 与 `GET /api/tasks/queue` 真的需要它们
    （而不是「将来可能用得上」）。
    """

    async def enqueue(self, spec: JobSpec) -> EnqueueOutcome: ...

    async def status(self, job_id: str) -> JobState: ...

    async def result(self, job_id: str) -> Any: ...

    async def owner(self, job_id: str) -> int | None:
        """这个任务**是谁的**（从入队时的 job 数据里读；读不到返回 `None`）。

        为什么需要它（而不是「等任务跑完从结果里读 user_id」）：`GET /api/tasks/{id}`
        要在**排队中**就做越权判定 —— 否则别人的任务状态（以及它「正在跑什么」）
        会一直可读到最后一步才被挡住。
        与 `result()` 一样，读不到（未入队/已过期）返回 `None` 而不是抛错：
        「不知道是谁的」不该让状态查询 500，由调用方决定怎么处置。
        """
        ...

    async def depth(self) -> int: ...

    async def gauges(self) -> QueueGauges: ...

    async def aclose(self) -> None: ...


@dataclass
class QueueGauges:
    """队列的可观测读数（`scale_signal()` 的返回）。"""

    depth: int
    deferred: int
    dead_letters: int
    oldest_dead_letter_seconds: float | None = None

    @property
    def backlog(self) -> int:
        """积压 = 立刻可取的 + 还没到点的（两者都还没被取走）。"""
        return self.depth + self.deferred

    def scale_hint(self) -> str:
        """给扩容决策的一句话（`12.queue.md` §5「背压」）。

        ⚠️ 判据刻意**粗**（只看积压与最老死信），因为它是给人看的一句话，
        不是自动扩缩容的输入 —— 自动扩缩容要的是延迟分位数（第 11 步的指标），
        不是队列长度（一个「每任务 5 分钟」的队列，积压 10 也可能完全正常）。
        """
        if self.oldest_dead_letter_seconds is not None and self.oldest_dead_letter_seconds > 86_400:
            return "⚠️ 有死信超过 24 小时无人处理：先走人工兜底流程，再谈扩容"
        if self.backlog >= 100:
            return "积压 ≥100：加 worker（先确认单个任务耗时没有变长）"
        if self.backlog >= 20:
            return "积压 ≥20：留意趋势，可能是下游变慢"
        return "积压正常"


def redis_settings_from_url(url: str, **overrides: Any) -> RedisSettings:
    """`REDIS_URL` → arq 的 `RedisSettings`。

    ⚠️ `conn_timeout` 从 arq 默认的 **1 秒**改大：入队是**请求路径上的一次写**
    （`POST /api/tasks`），而 1 秒在跨主机（本仓的开发拓扑就是 Windows → Linux VM）
    的首次连接（DNS + TCP + AUTH）上非常容易踩线。症状是**偶发**的
    `TimeoutError` —— 最容易被归因成「网络抖动」而放过去。
    """
    dsn = str(url or "").strip()
    if not dsn:
        raise QueueUnavailable(
            "REDIS_URL 没有配置：队列（第 12 步）与限流（第 9 步）都依赖它。"
            "在仓库根 .env 里填 `REDIS_URL=redis://<host>:6379/0`"
        )
    settings = RedisSettings.from_dsn(dsn)
    settings.conn_timeout = int(overrides.pop("conn_timeout", 5))
    for key, value in overrides.items():
        if not hasattr(settings, key):
            raise TypeError(f"RedisSettings 没有 {key!r} 这个字段（检查拼写）")
        setattr(settings, key, value)
    return settings


class RedisJobQueue:
    """arq 之上的队列实现（**唯一**直接使用 `create_pool` 的地方）。"""

    def __init__(
        self,
        *,
        redis: ArqRedis,
        queue_name: str = DEFAULT_QUEUE_NAME,
        dead_letters: Any = None,
    ) -> None:
        self._redis = redis
        self.queue_name = queue_name
        #: 可选：死信表（有它才能回答「这个 job 是不是进死信了」）
        self._dead_letters = dead_letters

    @classmethod
    async def open(
        cls,
        redis_url: str,
        *,
        queue_name: str = DEFAULT_QUEUE_NAME,
        dead_letters: Any = None,
        **settings_overrides: Any,
    ) -> "RedisJobQueue":
        """建连接池（`await RedisJobQueue.open(...)`）。

        用 `create_pool` 而不是 `redis.from_url`：前者返回的 `ArqRedis` 才带
        `enqueue_job`（它同时绑定了 `default_queue_name` 与 job 序列化器）。
        """
        settings = redis_settings_from_url(redis_url, **settings_overrides)
        pool = await create_pool(settings)
        return cls(redis=pool, queue_name=queue_name, dead_letters=dead_letters)

    # ── 入队 ─────────────────────────────────────────────────────────────────
    async def enqueue(self, spec: JobSpec) -> EnqueueOutcome:
        """投一个任务；**幂等命中时不重复投**（见模块 docstring 的说明）。"""
        job_id = spec.job_id
        try:
            existing = await self._existing_state(job_id)
        except RedisError as exc:  # pragma: no cover - 连接层故障
            raise QueueUnavailable(f"查任务状态失败（Redis）：{exc}") from exc
        if existing is not None and existing not in ("not_found", "complete"):
            # 还在队列里 / 正在跑 / 排在中途：不重复投
            logger.info("幂等命中：%s 已在队列中（状态=%s），本次不重复入队", job_id, existing)
            return EnqueueOutcome(
                job_id=job_id,
                function=spec.function,
                kind=spec.kind,
                created=False,
                defer_by=spec.defer_by,
                queue_name=self.queue_name,
            )

        kwargs: dict[str, Any] = {}
        if spec.defer_by > 0:
            kwargs["_defer_by"] = float(spec.defer_by)
        try:
            job = await self._redis.enqueue_job(
                spec.function,
                {"payload": spec.payload, "kind": spec.kind, "user_id": spec.user_id},
                _job_id=job_id,
                _queue_name=self.queue_name,
                **kwargs,
            )
        except RedisError as exc:
            raise QueueUnavailable(f"入队失败（Redis）：{exc}") from exc

        if job is None:
            # arq 自己判定了「同 id 已存在」（与上面的预检查构成双保险）
            logger.info("幂等命中（arq 判定）：%s 已存在", job_id)
            return EnqueueOutcome(
                job_id=job_id, function=spec.function, kind=spec.kind, created=False, defer_by=spec.defer_by
            )

        outcome = EnqueueOutcome(
            job_id=job_id,
            function=spec.function,
            kind=spec.kind,
            created=True,
            defer_by=spec.defer_by,
            queue_name=self.queue_name,
        )
        logger.info(
            "入队：kind=%s job_id=%s function=%s%s",
            spec.kind,
            job_id,
            spec.function,
            f" 延迟 {spec.defer_by:.1f}s" if spec.defer_by > 0 else "",
        )
        return outcome

    async def _existing_state(self, job_id: str) -> JobState | None:
        """这个 job_id 在 Redis 里处于什么状态（`None` = 连接层没答上来）。"""
        return await self.status(job_id)

    # ── 查询 ─────────────────────────────────────────────────────────────────
    async def status(self, job_id: str) -> JobState:
        """任务状态（arq 的五态 + `dead_letter` + `unknown`）。

        ⚠️ `Job.status()` 对「结果已过期」的 job 返回 `not_found` —— 与「从来没投过」
        无法区分（Redis 里查不到就是查不到）。本方法**如实**把它们都报出来，
        由调用方结合「有没有结果」去判断：`complete` 只有在结果还在时才是 complete。
        """
        try:
            raw = await Job(job_id, self._redis, _queue_name=self.queue_name).status()
        except RedisError as exc:  # pragma: no cover
            raise QueueUnavailable(f"查任务状态失败（Redis）：{exc}") from exc

        state: JobState = {
            JobStatus.queued: "queued",
            JobStatus.deferred: "deferred",
            JobStatus.in_progress: "in_progress",
            JobStatus.complete: "complete",
            JobStatus.not_found: "not_found",
        }.get(raw, "unknown")

        if state == "not_found" and self._dead_letters is not None:
            # 队列里没有：可能已经进死信。这一步让状态接口给出「终局」而不是「查不到」。
            try:
                entry = self._dead_letters.fetch(job_id)
            except Exception as exc:  # noqa: BLE001 - 死信表不可用不该让状态查询 500
                logger.warning("查死信表失败（状态查询降级为 not_found）：%s", exc)
            else:
                if entry is not None:
                    return "dead_letter"
        return state

    async def result(self, job_id: str) -> Any:
        """取任务返回值（没有结果时抛 `arq.jobs.ResultNotFound`，由调用方翻成 404）。"""
        return await Job(job_id, self._redis, _queue_name=self.queue_name).result(timeout=0)

    async def owner(self, job_id: str) -> int | None:
        """从 Redis 里的 job 数据读出「发起人」。

        ⚠️ 这里读的是 `arq:job:<id>` 里的**入队参数**（`Job.info()`），
        而不是结果 —— 因为排队中的任务还没有结果，而越权判定必须在**任何状态**下都有效
        （否则「别人正在跑什么」这件事会一直可读）。
        任务跑完之后 `arq:job:*` 会被删掉，那时回落到结果里的 `user_id`
        （`enqueue` 时我们把 `user_id` 写进了 job 参数，`_result_payload` 又把它带进了结果）。
        """
        try:
            info = await Job(job_id, self._redis, _queue_name=self.queue_name).info()
        except RedisError as exc:  # pragma: no cover - 连接层故障
            logger.warning("读任务归属失败（Redis）：%s", exc)
            return None
        args = tuple(getattr(info, "args", ()) or ())
        if args and isinstance(args[0], Mapping):
            raw = args[0].get("user_id")
            try:
                return int(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None
        return None

    async def depth(self) -> int:
        """立刻可取的积压条数（`arq:<queue>` 这个 ZSET 的基数）。"""
        try:
            return int(await self._redis.zcard(self._queue_key()))
        except RedisError as exc:  # pragma: no cover
            raise QueueUnavailable(f"查队列深度失败（Redis）：{exc}") from exc

    async def deferred(self) -> int:
        """还没到执行时间的任务条数（`zcount(now, +inf)`）。

        与 `depth()` 分开的理由：延迟任务多不等于积压 —— 那是**定时报表**的正常形态
        （一天 200 个），把它算进扩容信号会让 worker 每天定时被加一批。
        """
        try:
            return int(await self._redis.zcount(self._queue_key(), time.time() * 1000, "+inf"))
        except RedisError as exc:  # pragma: no cover
            raise QueueUnavailable(f"查延迟任务数失败（Redis）：{exc}") from exc

    async def gauges(self) -> QueueGauges:
        """读一次队列现状（演示脚本 / 验收 / 探活用）。"""
        depth = await self.depth()
        deferred = await self.deferred()
        dead = 0
        oldest: float | None = None
        if self._dead_letters is not None:
            try:
                dead = int(self._dead_letters.count_pending())
                oldest = self._dead_letters.oldest_pending_age()
            except Exception as exc:  # noqa: BLE001 - 队列现状不该因为死信表挂了而读不出来
                logger.warning("读死信表计数失败（队列读数降级）：%s", exc)
        return QueueGauges(depth=depth, deferred=deferred, dead_letters=dead, oldest_dead_letter_seconds=oldest)

    async def pending_job_ids(self, *, limit: int = 100) -> list[str]:
        """队列里还没被取走的 job_id（**按可执行时间升序**）。

        用途是排障与演示（「这几个任务为什么还没跑」），不是业务逻辑 ——
        真正的消费由 arq 的 worker 负责。
        """
        try:
            raw = await self._redis.zrange(self._queue_key(), 0, max(0, int(limit) - 1))
        except RedisError as exc:  # pragma: no cover
            raise QueueUnavailable(f"读队列内容失败（Redis）：{exc}") from exc
        return [item.decode() if isinstance(item, bytes) else str(item) for item in raw]

    async def ping(self) -> bool:
        """Redis 可达性（`/health` 用）。"""
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001 - 探活失败返回 False，不抛
            return False

    def _queue_key(self) -> str:
        # arq 把队列名**直接**当作 ZSET 的 key（`arq.constants.default_queue_name == 'arq:queue'`，
        # 而 `Worker(queue_name=...)` 就把它当 key 用）—— 所以这里不再补任何前缀。
        return self.queue_name

    async def aclose(self) -> None:
        """释放连接池（**必须显式关**：worker/脚本结束时漏掉它会留下挂起的连接）。"""
        try:
            await self._redis.aclose()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不该让调用方崩
            logger.debug("关闭 Redis 连接池时出错（忽略）：%s", exc)


#: 结果 TTL 的再导出（`jobs.py` 是真相，这里只为「队列相关的参数一处可见」）
RESULT_TTL: Final[int] = RESULT_TTL_SECONDS

#: arq 在 Redis 里的 key 前缀（排障时 `keys arq:*` 用得上）
KEY_PREFIX: Final[str] = "arq:"


# ── 进程级默认队列（「一行入队」的实现）────────────────────────────────────────
#
# 为什么要有它：`12.queue.md` §2 的用法是 `await enqueue_report(user_id=1, params={...})`
# —— 没有 queue 参数。那个「默认队列」必须来自某处，而三个选项各有代价：
#
#   ① 每次入队新建一个连接池 → 每个请求一次 TCP+AUTH，而且池不会被关（连接泄漏）；
#   ② 模块级全局单例 → **跨事件循环复用**。`redis.asyncio` 的连接是绑定在循环上的，
#      于是「测试里第二个 asyncio.run() 用第一个循环的池」会得到
#      `RuntimeError: Event loop is closed`（本仓实测过，最难查的一类：单跑绿、全量红）；
#   ③ **按事件循环缓存**（本实现）→ 同一个循环内复用，换循环就新建。
#
# 选 ③ 的代价：缓存字典里会留下已关闭循环的条目。所以有 `close_default_queues()`
# ——它由 FastAPI 的 lifespan（退出时）与 worker 的 `on_shutdown` 调用，
# 是「谁创建谁释放」在这条路径上的落点。
_QUEUES: dict[int, RedisJobQueue] = {}


def _loop_key() -> int:
    """当前事件循环的标识（没有循环时用 0，表示「只在同步上下文里用过」）。"""
    import asyncio

    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


async def default_queue(
    *,
    redis_url: str | None = None,
    queue_name: str = DEFAULT_QUEUE_NAME,
    dead_letters: Any = None,
) -> RedisJobQueue:
    """取本事件循环的默认队列（没有就建）。

    ⚠️ 参数只在**首次创建**时生效：第二条请求传一个不同的 `redis_url` 不会换掉已有的池
    （那会让「一半请求投到 A、一半投到 B」这种最难查的问题出现）。
    要换队列就显式传 `queue=` 参数给 `enqueue_*`（那是注入点，行为是明确的）。
    """
    key = _loop_key()
    existing = _QUEUES.get(key)
    if existing is not None:
        return existing

    if redis_url is None:
        from app.config import get_settings

        redis_url = str(getattr(get_settings(), "redis_url", "") or "")
    if dead_letters is None:
        try:
            from app.queue.store import PostgresDeadLetterStore

            dead_letters = PostgresDeadLetterStore()
        except Exception as exc:  # noqa: BLE001 - 死信表可选：没有它只是状态查询降级
            logger.warning("死信表不可用（状态查询将报 not_found）：%s", exc)
            dead_letters = None

    queue = await RedisJobQueue.open(redis_url, queue_name=queue_name, dead_letters=dead_letters)
    _QUEUES[key] = queue
    logger.info("已建立默认队列连接：queue=%s", queue_name)
    return queue


async def close_default_queues() -> int:
    """关闭并丢弃所有缓存的默认队列；返回关闭了几个（lifespan 退出时调用）。"""
    closed = 0
    for key, queue in list(_QUEUES.items()):
        try:
            await queue.aclose()
            closed += 1
        except Exception as exc:  # noqa: BLE001 - 关闭失败不该让应用退出失败
            logger.debug("关闭默认队列时出错（忽略）：%s", exc)
        _QUEUES.pop(key, None)
    return closed


def describe_state(state: JobState) -> str:
    """把状态翻成一句中文（API 响应与 CLI 共用）。"""
    return {
        "queued": "已入队，等待 worker 取走",
        "deferred": "延迟任务，还没到执行时间",
        "in_progress": "正在执行",
        "complete": "执行完成（结果在读缓存里）",
        "not_found": "队列里没有这个任务，且死信表里也没有",
        "unknown": "状态未知（arq 返回了未登记的取值）",
        "dead_letter": "已进死信队列，等待人工重放或关闭",
    }.get(state, str(state))


__all__ = [
    "DEFAULT_QUEUE_NAME",
    "EnqueueOutcome",
    "KEY_PREFIX",
    "JobQueue",
    "JobState",
    "QueueGauges",
    "QueueUnavailable",
    "RESULT_TTL",
    "RedisJobQueue",
    "close_default_queues",
    "default_queue",
    "describe_state",
    "redis_settings_from_url",
]
