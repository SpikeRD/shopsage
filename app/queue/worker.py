"""worker 进程：`WorkerSettings` + **内核结论 ↔ arq 动作的桥**（`12.queue.md` §6 第 1、4 步）。

启动：

```powershell
uv run arq app.queue.worker.WorkerSettings            # 前台
uv run python -m scripts.worker                       # 等价入口（多一层配置自检，见该脚本）
```

## 本模块存在的唯一理由：把 arq 的「重试」与我们自己的「收场」接起来

arq 自己有一套重试（函数里 `raise Retry(defer=...)`），而 `12.queue.md` §4 第 3、4 条
要的是**分级重试 + 超限进死信**。两件事有一处**硬冲突**，实测（读 arq 0.28 的
`Worker.run_job` 源码）是这样的：

```
job_try = INCR arq:retry:<job_id>          # 每次取到任务先 +1
if job_try > max_tries:
    → 直接 finish_failed_job(...)，**函数根本不会被调用**
```

也就是说：**如果让 arq 的 `max_tries` 来判定「重试额度用尽」，那么「用尽」那一刻
我们的函数一次都不会被调用** —— 于是「进死信」这件事永远没有机会发生，
任务从 Redis 消失、死信表里也没有。这正是 `12.queue.md` §8 那句
「零任务丢失」要防的事，也是本步最贵的一个坑。

处置（`bridge_job` 与 `WorkerSettings.max_tries` 逐行实现）：

1. `max_tries = MAX_ATTEMPTS = 最大重试额度 + 1`。于是「额度用尽」的那一次**会**进函数，
   由 `core.execute_job` 写完死信再收场；arq 那一档只做**最后一道保险**
   （防我们的计数本身出问题）；
2. `Retry` 只在「内核说还要重试」时抛，且**延迟由内核算**（任务可用
   `RetryTask(defer=)` 覆盖）—— 退避策略只有一处真相；
3. `selfcheck()` 把「max_tries 必须大于最大额度」变成一条**启动期检查**：
   这个数字改错了不会有任何报错，只会让死信队列静默地永远是空的。

## 死信的清理：重放成功之后

重放的任务带 `dlq_key`（原死信的 job_id，由 `core.replay_plan` 塞进 payload）。
任务**成功**时把它标成 `replayed_ok`（并记下重放用的 job_id）—— 少了这一步，
死信列表永远不收敛，「有死信躺着」这条告警会一直响（而那正是 A1 失效的形态）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

from arq import cron
from arq.connections import RedisSettings
from arq.worker import Retry, func

from app.observability.metrics import QUEUE_DEPTH
from app.queue import agent_runs
from app.queue.backend import (
    DEFAULT_QUEUE_NAME,
    RedisJobQueue,
    close_default_queues,
    redis_settings_from_url,
)
from app.queue.core import DeadLetterStore, JobOutcome, RetryPlanned, execute_job
from app.queue.jobs import (
    DEFAULT_POLICIES,
    FUNC_AGENT,
    FUNC_KB_REBUILD,
    FUNC_REPORT,
    FUNCTIONS,
    JOB_TIMEOUT_SECONDS,
    RESULT_TTL_SECONDS,
    RetryPolicy,
    kind_of_function,
)

logger = logging.getLogger(__name__)

#: arq 的 `max_tries` = 我们的最大额度 + 1（见模块 docstring 第 1 条）。
#:
#: 取 `DEFAULT_POLICIES` 里最大的 `attempts` 再加一档：这样**任何**登记过的策略
#: 都来不及被 arq 抢先判定（arq 的那一档只在「我们的计数坏了」时才该生效）。
MAX_ATTEMPTS: Final[int] = max(policy.attempts for policy in DEFAULT_POLICIES.values()) + 1

#: 死信积压的巡检间隔（秒）。cron 每分钟看一眼 —— 它是**保留任务**，
#: 不参与业务队列，也不会被业务任务的 `max_tries` 影响。
DEAD_LETTER_CHECK_SECONDS: Final[int] = 60

#: 死信「躺太久」的判据（秒）：24 小时。超过它说明**人工兜底这条路径没人在走**
#: （A1 失效），而不是「刚发生了一批抖动」。
DEAD_LETTER_ALERT_SECONDS: Final[float] = 86_400.0

#: 任务名 → 实现（`app/queue/agent_runs.py` 里的三个函数）。
#:
#: ⚠️ 值是**函数对象**而不是字符串：字符串要靠 `getattr` 找回来，
#: 而打错一个字母只会在运行时炸（且是在 worker 里，不在启动时）。
_RUNNERS: Final[dict[str, Any]] = {
    FUNC_REPORT: agent_runs.run_report,
    FUNC_AGENT: agent_runs.run_agent_turn,
    FUNC_KB_REBUILD: agent_runs.rebuild_knowledge_base,
}


def _metrics() -> Any:
    """进程默认指标实例（`/metrics` 渲染的就是它）。"""
    from app.observability.metrics import default_metrics

    return default_metrics()


def _store_of(ctx: Mapping[str, Any], override: DeadLetterStore | None) -> DeadLetterStore | None:
    """取死信表：显式传入的优先，否则用 `on_startup` 建好的那个（`ctx["dead_letters"]`）。"""
    if override is not None:
        return override
    store = ctx.get("dead_letters")
    return store if store is not None else None


def runner_for(function: str) -> Any:
    """任务名 → 内核要调的那个「干活」函数（**注册表的唯一真相**）。"""
    runner = _RUNNERS.get(function)
    if runner is None:
        raise KeyError(f"任务 {function!r} 没有实现（检查 app/queue/worker.py 的 _RUNNERS）")
    return runner


def registered_functions() -> list[Any]:
    """要注册给 arq 的函数列表（每个都带 `timeout` / `keep_result`）。

    `func(...)` 是 arq 的包装：`timeout` 用 `JOB_TIMEOUT_SECONDS`（长任务的上限，
    理由见 `jobs.py`），`keep_result` 用 `RESULT_TTL_SECONDS`（客户端第二天回来捞结果）。
    `max_tries` **刻意不在这里覆盖**：它由 `WorkerSettings.max_tries` 统一给
    （理由见模块 docstring 第 1 条 —— 这个数字必须比所有策略的额度大一档）。
    """
    return [
        func(
            _bridge_for(name),
            name=name,
            timeout=JOB_TIMEOUT_SECONDS,
            keep_result=RESULT_TTL_SECONDS,
        )
        for name in sorted(_RUNNERS)
    ]


def _bridge_for(name: str) -> Any:
    """造一个「名字叫 `name`」的桥函数（arq 用 `__qualname__` 注册函数名）。

    ⚠️ 为什么要它：三个任务名（`shopsage.run_report` 等）都是**持久化契约**
    （写进 Redis 的 job 数据），而桥只有一个。arq 按 `Function.name` 注册，
    所以每种任务需要自己的 `Function` 对象与自己的名字 ——
    用闭包而不是「三个手抄的函数」，漂移就没有了发生的地方。

    ⚠️ 它还负责把**任务名塞进 ctx**（`ctx["function"] = name`）：arq 自己不往 ctx 里
    放函数名（实测 0.28 的 `run_job` 只放 `job_id` / `job_try` / `enqueue_time` / `score`），
    而 `bridge_job` 需要它来**选出正确的重试策略**。
    少了这一行，`policy_for("")` 会落到「未登记 → 只跑一次」那一档 ——
    症状是「可恢复的错误第一次就进死信」，看起来像「重试没生效」，
    而报错里没有任何线索指向策略选择。
    """

    async def bridge(ctx: dict[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        ctx["function"] = name
        return await bridge_job(ctx, payload)

    bridge.__name__ = name.rsplit(".", 1)[-1]
    bridge.__qualname__ = name
    return bridge


async def bridge_job(
    ctx: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    runner: Any = None,
    dead_letters: DeadLetterStore | None = None,
) -> dict[str, Any]:
    """任务入口：**执行 + 按内核结论决定「重投」还是「收场」**。

    `ctx` 由 arq 注入（实测 0.28 的 `run_job` 里
    `ctx = {**self.ctx, **{'job_id':…, 'job_try':…, 'enqueue_time':…, 'score':…}}`），
    本函数用到三个键：`job_id` / `job_try` / `enqueue_time`。
    ⚠️ 函数名不在 ctx 里（arq 没放），所以调用方（`_bridge_for` 造的那一层）
    不能传 —— 这里用 `ctx["function"]`（`on_job_start` 也拿不到它，所以走兜底）。

    `payload` 的形状由 `tasks.build_spec` 决定（入队时作为 arq 的第一个位置参数）：

    ```
    {"payload": {...任务自己的入参...}, "kind": "report", "user_id": 2}
    ```

    为什么裹一层 `{"payload": ...}` 而不是把任务入参直接铺开：
    arq 的 job 参数是**位置参数 + 关键字参数**，把它们铺开会与任务签名绑死，
    而「任务签名改了、队列里还有旧任务」这件事在发布时必然发生
    （症状是 `TypeError: got an unexpected keyword argument`，任务全部进死信）。
    多一层字典让「任务入参的演进」不需要动传输层。

    `runner` / `dead_letters` 是**注入点**（测试直接调它来验「重投 / 进死信 / 重放」
    三条路径，不必起真 worker）；生产路径两者都是 `None`，走注册表与 ctx。
    """
    from app.queue.jobs import policy_for

    data = dict(payload or {})
    inner = dict(data.get("payload") or {})
    # 任务名：**payload 优先**（`tasks.build_spec` 入队时写进去的，是生产路径的真相），
    # 取不到时用 ctx 里的（`_bridge_for` 注册时塞的）——两者都没有就只能用空串，
    # 那时 `policy_for("")` 会给最保守的一档（不重试），并由下面这条日志暴露出来。
    function = str(data.get("function") or ctx.get("function") or "")
    if not function:
        logger.error("任务 payload 里没有 function，且 ctx 里也没有：将按「不重试」处理（job_id=%s）", ctx.get("job_id"))
    kind = str(data.get("kind") or kind_of_function(function))
    attempt = int(ctx.get("job_try") or 1)
    job_id = str(ctx.get("job_id") or "")
    policy: RetryPolicy = ctx.get("policy") or policy_for(function)

    active_runner = runner if runner is not None else runner_for(function)
    ctx["job_payload"] = inner  # type: ignore[index] - 给 after_job_end 用（见那函数）
    try:
        outcome = await execute_job(
            job_id=job_id,
            function=function,
            kind=kind,
            payload=inner,
            attempt=attempt,
            run=lambda: active_runner(inner),
            policy=policy,
            dead_letters=_store_of(ctx, dead_letters),
            metrics=_metrics(),
            enqueued_at=ctx.get("enqueue_time"),
            user_id=data.get("user_id"),
        )
    except RetryPlanned as planned:
        # 内核说「还要再试一次」：交给 arq 的重投机制（**延迟由内核算**）
        raise Retry(defer=planned.defer) from planned

    # ⚠️ 结果挂在 ctx 上：arq 的 `after_job_end(ctx)` **拿不到函数的返回值**，
    # 而「这次是不是进了死信」要传到那里才能让重放成功的死信被关掉
    # （见 `after_job_end_hook`）。ctx 是每个任务一份的字典（`{**self.ctx, **job_ctx}`），
    # 所以挂在那里不会串任务。
    ctx["job_outcome"] = outcome  # type: ignore[index]
    if outcome.status == "dead_letter" and outcome.error is not None:
        # 死信已经落盘，**再抛**是为了让 arq 的重试计数与失败指标也对上
        # （否则它会记成「任务成功」，而监控里看不到任何异常）。
        raise outcome.error
    return _result_payload(outcome, user_id=data.get("user_id"))


def _result_payload(outcome: JobOutcome, *, user_id: int | None = None) -> dict[str, Any]:
    """任务返回值：**只放能 JSON 化的东西**（它在 Redis 里躺 24 小时）。

    ⚠️ `user_id` **必须**在结果里（即使任务自己的返回值里没有）：
    `GET /api/tasks/{job_id}` 的越权判定在「任务已完成、`arq:job:*` 已被删除」之后
    只能从结果里读发起人。少了它，那一步会静默地变成「不判」（见 `app/api/tasks.py`
    的 `_owner` 与 runbook §10.1）。

    ⚠️ 不把异常对象放进来：arq 在失败时会自己 pickle 异常（这也是为什么
    我们**不能**把 `KeyError` 这类不可恢复错误「吞掉」—— 吞掉就等于让监控失去信号）。
    """
    value = outcome.value
    body: dict[str, Any] = dict(value) if isinstance(value, Mapping) else {"value": value}
    body["job_id"] = outcome.job_id
    body["status"] = outcome.status
    body["attempts"] = outcome.attempts
    if user_id is not None:
        body.setdefault("user_id", int(user_id))
    return body


# ── 生命周期 ─────────────────────────────────────────────────────────────────


async def startup(ctx: dict[str, Any]) -> None:
    """worker 启动：建死信表与队列连接、自检、写一次队列读数。"""
    from app.queue.store import PostgresDeadLetterStore

    ctx["dead_letters"] = PostgresDeadLetterStore()
    try:
        from app.queue.backend import default_queue

        ctx["queue"] = await default_queue()
    except Exception as exc:  # noqa: BLE001 - 队列连不上由 arq 自己重试连接
        logger.warning("建默认队列连接失败（worker 仍会尝试消费）：%s", exc)

    problems = selfcheck(ctx)
    if problems:
        # 不是致命错误（worker 仍能跑），但必须吼：缺死信表 = 「零任务丢失」没有落点
        for problem in problems:
            logger.error("worker 自检未通过：%s", problem)
    else:
        logger.info("worker 就绪：任务=%s max_tries=%d", ", ".join(sorted(_RUNNERS)), MAX_ATTEMPTS)
    await record_queue_gauges(ctx)


async def shutdown(ctx: dict[str, Any]) -> None:
    """worker 退出：关连接池（漏掉它会让进程挂着不退出）。"""
    closed = await close_default_queues()
    logger.info("worker 关闭：已释放 %d 个队列连接池", closed)


async def after_job_end_hook(ctx: Mapping[str, Any]) -> None:
    """任务结束之后：**关闭重放成功的那条死信**（见模块 docstring 最后一节）。"""
    outcome = ctx.get("job_outcome")
    if not (isinstance(outcome, JobOutcome) and outcome.status == "succeeded"):
        return
    inner = ctx.get("job_payload") or {}
    if not isinstance(inner, Mapping):
        return
    dlq_key = str(inner.get("dlq_key") or "")
    if not dlq_key:
        return
    store = _store_of(ctx, None)
    if store is None:
        logger.warning("重放任务 %s 成功，但没有死信表可写（%s 会一直留在待处理列表里）", outcome.job_id, dlq_key)
        return
    try:
        store.resolve(dlq_key, resolution="replayed_ok", replayed_job_id=outcome.job_id)
    except Exception as exc:  # noqa: BLE001 - 关闭失败不该让任务失败
        logger.warning("关闭死信记录失败（%s）：%s", dlq_key, exc)
    else:
        logger.info("重放成功，已关闭死信：%s（重放任务 %s）", dlq_key, outcome.job_id)


async def dead_letter_watch(ctx: Mapping[str, Any]) -> str:
    """巡检死信积压（cron，每分钟一次）并写指标。

    为什么值得一个定时任务：死信是**人工兜底**的入口（A1），而「有没有人真的在看」
    只有两个可观测信号 —— 待处理条数、以及**最老的那条躺了多久**。
    后者才是值得告警的那个（条数多可能只是刚发生了一批抖动）。
    """
    store = _store_of(ctx, None)
    if store is None:
        return "没有死信表，跳过巡检"
    try:
        pending = int(store.count_pending())  # type: ignore[attr-defined]
        age = store.oldest_pending_age()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - 巡检失败只记日志
        logger.warning("死信巡检失败：%s", exc)
        return f"巡检失败：{exc}"
    metrics = _metrics()
    metrics.gauge("shopsage_queue_dead_letter_pending", float(pending))
    if age is not None:
        metrics.gauge("shopsage_queue_dead_letter_oldest_seconds", float(age))
    if age is not None and age > DEAD_LETTER_ALERT_SECONDS:
        logger.error(
            "有死信超过 %.0f 小时无人处理（%d 条，最老 %.1f 小时）：人工兜底流程断了（A1 失效）",
            DEAD_LETTER_ALERT_SECONDS / 3600,
            pending,
            age / 3600,
        )
    return f"待处理死信 {pending} 条（最老 {'无' if age is None else f'{round(age)} 秒'}）"


async def record_queue_gauges(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """把队列现状写成指标并返回一份结构化读数（启动与巡检各一次）。"""
    queue = ctx.get("queue")
    metrics = _metrics()
    data: dict[str, Any] = {}
    if isinstance(queue, RedisJobQueue):
        try:
            gauges = await queue.gauges()
        except Exception as exc:  # noqa: BLE001 - 读不到就不写，不抛
            logger.warning("读队列现状失败：%s", exc)
            return data
        data = {
            "depth": gauges.depth,
            "deferred": gauges.deferred,
            "dead_letters": gauges.dead_letters,
            "oldest_dead_letter_seconds": gauges.oldest_dead_letter_seconds,
            "hint": gauges.scale_hint(),
        }
        metrics.gauge(QUEUE_DEPTH, float(gauges.depth))
        logger.info(
            "队列现状：立即可取=%d 延迟=%d 待处理死信=%d → %s",
            gauges.depth,
            gauges.deferred,
            gauges.dead_letters,
            gauges.scale_hint(),
        )
    return data


def selfcheck(ctx: Mapping[str, Any] | None = None) -> list[str]:
    """worker 配置自检：返回**问题清单**（空 = 一切就绪）。

    五项检查都对应一次真实的、静默的故障：

    | 检查 | 不做会怎样 |
    |---|---|
    | 注册表与任务名清单一致 | 某个任务名没有实现 → 入队成功、执行时 `KeyError` |
    | 每个任务都有重试策略 | 未登记策略的任务只有 1 次机会（静默地不重试） |
    | `max_tries > 所有策略的 attempts` | arq 抢先判定 → **死信永远为空**（本模块的头号坑） |
    | 死信表可达 | 「零任务丢失」没有落点，失败只留在日志里 |
    | 任务名是持久化契约 | 改名会让「重启前入队、重启后执行」的任务找不到函数 |

    最后一条检查的是「任务名有没有前缀」：arq 的 job 数据里存的是函数名，
    没有前缀的名字（例如 `run_report`）在同一个 Redis 里很容易与别的项目撞名。
    """
    problems: list[str] = []
    names = set(_RUNNERS)
    missing_impl = sorted(set(FUNCTIONS) - names)
    if missing_impl:
        problems.append(f"这些任务名没有实现：{missing_impl}（在 _RUNNERS 里补上）")
    without_policy = sorted(name for name in names if name not in DEFAULT_POLICIES)
    if without_policy:
        problems.append(
            f"这些任务没有登记重试策略：{without_policy}（会退化成「一次都不重试」，"
            "在 jobs.DEFAULT_POLICIES 里补上）"
        )
    unprefixed = sorted(name for name in names if "." not in name)
    if unprefixed:
        problems.append(f"任务名应当带命名空间前缀（如 `shopsage.`）：{unprefixed}")
    max_policy = max((policy.attempts for policy in DEFAULT_POLICIES.values()), default=1)
    if MAX_ATTEMPTS <= max_policy:
        problems.append(
            f"arq 的 max_tries={MAX_ATTEMPTS} 必须**大于**最大重试额度 {max_policy}："
            "否则「额度用尽」那一次不会进函数，死信队列永远是空的"
        )
    store = ctx.get("dead_letters") if ctx else None
    available = getattr(store, "available", None)
    if callable(available) and not available():
        problems.append(
            "死信表不可达（queue_dead_letters）：先跑 `uv run alembic upgrade head`。"
            "缺它时任务失败不会有任何持久留痕 —— 而「零任务丢失」这条验收就没有落点"
        )
    return problems


def _settings_from_env() -> RedisSettings:
    """从 `.env` 的 `REDIS_URL` 造 arq 的连接参数（读不到就用默认值，见类 docstring）。"""
    try:
        from app.config import get_settings

        url = str(getattr(get_settings(), "redis_url", "") or "")
        if url:
            return redis_settings_from_url(url)
    except Exception as exc:  # noqa: BLE001 - 配置不可读不该让 worker 起不来（自检会报）
        logger.warning("读 REDIS_URL 失败，worker 使用 arq 默认连接参数：%s", exc)
    return RedisSettings()


class WorkerSettings:
    """arq 的入口配置（`uv run arq app.queue.worker.WorkerSettings`）。

    ⚠️ `redis_settings` 在**类定义期**从 `.env` 读：arq 的 CLI 用
    `get_kwargs(settings_cls)` 取这些属性（它不会去调 `.env` 的加载），
    而「队列连哪个 Redis」只有一处真相（`Settings.redis_url`）。
    读不到配置时**不抛**（否则连 `--help` 都跑不了），退回一个明显不对的默认值，
    由 `startup` 的自检把它变成一条能照做的日志。
    """

    redis_settings = _settings_from_env()
    queue_name = DEFAULT_QUEUE_NAME
    functions = registered_functions()
    #: 比最大重试额度多一档（见模块 docstring：本文件最要紧的一个数字）
    max_tries = MAX_ATTEMPTS
    job_timeout = JOB_TIMEOUT_SECONDS
    keep_result = RESULT_TTL_SECONDS
    #: 一次同时跑几个任务。**默认 4**（不是 arq 的 10）：长任务多是 IO + 模型调用，
    #: 而 C1（成本）怕的是「一次打太多模型」。要扩容就调这个数，或直接加进程。
    max_jobs = 4
    #: 轮询间隔：0.5s 是 arq 的默认值。队列里没活时它就是 worker 的空转频率。
    poll_delay = 0.5
    on_startup = startup
    on_shutdown = shutdown
    after_job_end = after_job_end_hook
    #: 保留任务：每分钟巡检死信积压（**它不进业务队列**，也不受业务 max_tries 影响）
    cron_jobs = [cron(dead_letter_watch, second={0}, run_at_startup=True)]


__all__ = [
    "DEAD_LETTER_ALERT_SECONDS",
    "DEAD_LETTER_CHECK_SECONDS",
    "MAX_ATTEMPTS",
    "WorkerSettings",
    "after_job_end_hook",
    "bridge_job",
    "dead_letter_watch",
    "record_queue_gauges",
    "registered_functions",
    "runner_for",
    "selfcheck",
    "shutdown",
    "startup",
]
