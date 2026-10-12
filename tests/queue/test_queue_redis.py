"""真 Redis 的用例：**证明「我们对 arq 语义的理解」是对的**（默认跳过）。

```powershell
$env:SHOPSAGE_TEST_REDIS = "1"      # ← 关掉本文件的 skip 守卫
uv run pytest -q -p no:cacheprovider tests/queue/test_queue_redis.py -v
```

## 为什么要有这组用例（它验的不是业务逻辑）

队列语义（延迟、重试、幂等、死信）在 `test_queue_semantics.py` 里已经用**内存队列**
验完了，而且那里跑的是生产的 `bridge_job` + `execute_job`。那组用例的唯一前提是
「`tests/queue/fake_queue.py` 对 arq 语义的复刻是对的」—— 本文件就是那个前提的**证据**：

| 我们以为的 arq 行为 | 本文件怎么证 |
|---|---|
| 重复 `_job_id` 入队不覆盖（返回 `None`） | `test_second_enqueue_with_the_same_id_is_a_noop` |
| `_defer_by` 把任务放进 ZSET 的 score | `test_deferred_score_is_the_planned_run_time` |
| 到点之前 worker 取不走它 | `test_worker_in_burst_mode_skips_a_deferred_job` |
| 任务的第一个位置参数原样到达函数 | `test_worker_in_burst_mode_runs_the_task` |
| 重试以 `INCR arq:retry:<job_id>` 计数 | `test_retry_count_lives_in_redis` |

⚠️ 为什么默认跳过（而不是「连不上就红」）：本机没有 Redis 时，整组用例会红一片，
而它们的失败原因（依赖栈没起）与本步的代码质量无关 —— 那会让「本步的验收基线」
失去意义。这与第 8 步 Redis checkpointer 的处置同源
（见 `scripts/redis_checkpointer_probe.py` 与 `8.graph-runbook.md` §4.3）。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import pathlib
import time
import uuid

import pytest

from app.queue.backend import (
    DEFAULT_QUEUE_NAME,
    QueueUnavailable,
    RedisJobQueue,
    redis_settings_from_url,
)
from app.queue.core import RetryPlanned, execute_job
from app.queue.jobs import FUNC_REPORT, JobSpec, RetryPolicy
from app.queue.tasks import enqueue_report

#: 关掉 skip 守卫的环境变量（与 `SHOPSAGE_TEST_REDIS_CHECKPOINTER` 同一形状）
GATE = "SHOPSAGE_TEST_REDIS"

#: 单个任务 / 单个 burst 的墙钟上限（秒）
TEST_TIMEOUT = 20.0


def _gate_open() -> bool:
    return str(os.environ.get(GATE, "")).strip() in {"1", "true", "yes", "on"}


pytestmark = pytest.mark.skipif(
    not _gate_open(),
    reason=(
        "需要真 Redis（本机没有时整组会红，与代码质量无关）："
        f'设 `$env:{GATE} = "1"` 并在 .env 里配好 REDIS_URL 后重跑'
    ),
)


def _redis_url() -> str:
    from app.config import get_settings

    return str(getattr(get_settings(), "redis_url", "") or "")


def _job_id() -> str:
    """一个本次用例专属的 job_id（带随机后缀，便于清理与断言）。"""
    return f"q_test{uuid.uuid4().hex[:8]}:redis"


def _spec(job_id: str, *, defer_by: float = 0.0) -> JobSpec:
    """造一个 `JobSpec`（用显式的 job_id）。"""
    return JobSpec(
        kind="report",
        function=FUNC_REPORT,
        payload={"user_id": 2, "title": "真 Redis 报表"},
        idempotency_key=job_id[2:] if job_id.startswith("q_") else job_id,
        user_id=2,
        defer_by=defer_by,
    )


@pytest.fixture
async def queue():
    """一个用**专用队列名**的真 Redis 队列（不污染业务队列）。

    ⚠️ 队列名带随机后缀：`arq:shopsage:default:test:<随机>`。这样清理可以按
    **已知的键**精确删，不必用 `KEYS` 通配 —— 那在生产 Redis 上是个危险操作，
    而且 `KEYS arq:*` 会扫到别人的任务。

    用例把 `enqueue()` 得到的 job_id 记进 `queue.created_job_ids`，退出时逐个删干净。
    """
    name = f"{DEFAULT_QUEUE_NAME}:test:{uuid.uuid4().hex[:8]}"
    instance = await RedisJobQueue.open(_redis_url(), queue_name=name)
    created: list[str] = []
    instance.created_job_ids = created  # type: ignore[attr-defined] - 测试装置用它做清理
    try:
        yield instance
    finally:
        try:
            redis = instance._redis  # noqa: SLF001 - 装置就是要清干净
            # ⚠️ 队列键与**健康检查键**都要删：arq 每个 worker 会写
            # `arq:<queue_name>:health-check`（实测：清理时漏掉它，跑一轮真 Redis 用例
            # 就会在共享实例上留下几十个这种键）。
            keys: list[str] = [instance.queue_name, f"{instance.queue_name}:health-check"]
            for job_id in created:
                keys += [
                    f"arq:job:{job_id}",
                    f"arq:result:{job_id}",
                    f"arq:retry:{job_id}",
                    f"arq:in-progress:{job_id}",
                ]
            await redis.delete(*keys)
        except Exception:  # noqa: BLE001 - 清理失败不该让用例失败
            pass
        await instance.aclose()


async def _enqueue(queue, job_id: str, *, defer_by: float = 0.0):
    """入队并登记 job_id（清理用），返回 `EnqueueOutcome`。"""
    outcome = await queue.enqueue(_spec(job_id, defer_by=defer_by))
    queue.created_job_ids.append(outcome.job_id)
    return outcome


# ── 连通性与入队 ─────────────────────────────────────────────────────────────


async def test_redis_is_reachable(queue) -> None:
    """能连上（`REDIS_URL` 配对了、Redis 活着）。"""
    assert await queue.ping() is True


def test_connection_settings_give_a_longer_timeout_than_arq_default() -> None:
    """`conn_timeout` 必须比 arq 默认的 1 秒大（入队是请求路径上的一次写，跨主机容易踩线）。"""
    settings = redis_settings_from_url(_redis_url())
    assert settings.conn_timeout >= 5


async def test_enqueue_then_read_back(queue) -> None:
    """入队 → 状态是 `queued`（还没被取走）→ 深度 +1。"""
    before = await queue.depth()
    outcome = await _enqueue(queue, _job_id())
    assert outcome.created is True
    assert outcome.status == "queued"
    assert await queue.status(outcome.job_id) == "queued"
    assert await queue.depth() == before + 1


async def test_second_enqueue_with_the_same_id_is_a_noop(queue) -> None:
    """**重复 `_job_id` 不重复入队**（`created=False`）—— 这是幂等入队的实现基础。

    ⚠️ 这条是本文件最值钱的一条：内存队列的「幂等命中」完全建立在这个行为上。
    """
    job_id = _job_id()
    first = await _enqueue(queue, job_id)
    second = await queue.enqueue(_spec(job_id))
    assert first.created is True
    assert second.created is False
    assert first.job_id == second.job_id


async def test_unknown_job_reports_not_found(queue) -> None:
    """没投过的 job_id → `not_found`（而不是抛错）。"""
    assert await queue.status("q_test:never_enqueued") == "not_found"


async def test_owner_is_readable_from_the_enqueued_parameters(queue) -> None:
    """`owner()` 从 **job 的入队参数**里读发起人（排队中也能做越权判定）。"""
    outcome = await _enqueue(queue, _job_id())
    assert await queue.owner(outcome.job_id) == 2


# ── 延迟任务 ─────────────────────────────────────────────────────────────────


async def test_deferred_job_lands_in_the_delayed_queue(queue) -> None:
    """`defer_by` 的任务：状态 `deferred`，且**只出现在延迟计数里**。

    ⚠️ 这里**不能**断言「深度不变」：arq 的队列就是一个 ZSET，
    `zcard`（我们的 `depth()`）会把**还没到点**的任务也数进去 ——
    真正区分「积压」与「定时」的是 `zcount(now, +inf)`（我们的 `deferred()`）。
    第一版断言了深度不变，于是它红了，而代码是对的（见 runbook §10.1）。
    """
    before_deferred = await queue.deferred()
    outcome = await _enqueue(queue, _job_id(), defer_by=30.0)

    assert outcome.status == "deferred"
    assert await queue.status(outcome.job_id) == "deferred"
    assert await queue.deferred() == before_deferred + 1


async def test_backlog_and_depth_are_two_different_numbers(queue) -> None:
    """`depth()`（ZSET 全部）与 `deferred()`（还没到点）是两个量，用途不同。

    扩容信号只该看「立刻可取的」那部分，所以 `QueueGauges` 把两者分开暴露 ——
    这条用例把「分开」这件事从文档变成事实。
    """
    await _enqueue(queue, _job_id())
    await _enqueue(queue, _job_id(), defer_by=600.0)
    depth = await queue.depth()
    deferred = await queue.deferred()
    assert depth >= deferred >= 1
    assert depth > deferred  # 至少有一个是「立刻可取」的


async def test_deferred_score_is_the_planned_run_time(queue) -> None:
    """延迟任务的 ZSET score = 到点时间（`now + defer_by`，毫秒）。

    这条把「延迟任务」从「一个我们相信的行为」变成「一个可读的数字」。
    """
    outcome = await _enqueue(queue, _job_id(), defer_by=25.0)
    score = await queue._redis.zscore(queue.queue_name, outcome.job_id)  # noqa: SLF001
    expected = (time.time() + 25.0) * 1000
    assert score is not None
    assert abs(float(score) - expected) < 5_000  # 5 秒容差（跨主机时钟/网络）


# ── worker（burst 模式：跑完队列就退出）──────────────────────────────────────


async def test_worker_in_burst_mode_runs_the_task(queue) -> None:
    """**真 worker 消费**：入队的任务被 `bridge_job` 执行，结果写回 Redis。

    用一个 `burst=True` 的 worker（跑完即退出）—— 它是 arq 官方的「一次性消费」模式，
    正好适合用例：不必起一个常驻进程、也不会与其他用例抢任务。
    """
    calls: list[str] = []

    async def runner(payload):
        calls.append(str(payload.get("title")))
        return {"summary": f"报表：{payload.get('title')}"}

    outcome = await _enqueue(queue, _job_id())
    await _run_burst(queue, runner=runner, limit_seconds=TEST_TIMEOUT)

    assert calls == ["真 Redis 报表"], "任务没有被 worker 执行（或参数没到）"
    assert await queue.status(outcome.job_id) == "complete"
    result = await queue.result(outcome.job_id)
    assert result["summary"] == "报表：真 Redis 报表"
    assert result["job_id"] == outcome.job_id
    assert result["status"] == "succeeded"


async def test_worker_in_burst_mode_skips_a_deferred_job(queue) -> None:
    """延迟任务**到点之前不被取走**（burst 跑一轮之后它还在延迟队列里）。

    这是 `12.queue.md` §7 第 4 条（`defer_until 到期才执行`）在**真 arq** 上的版本。
    """
    calls: list[str] = []

    async def runner(payload):
        calls.append("ran")
        return {"ok": True}

    outcome = await _enqueue(queue, _job_id(), defer_by=30.0)
    await _run_burst(queue, runner=runner, limit_seconds=5.0)

    assert calls == [], "延迟任务不该被执行"
    assert await queue.status(outcome.job_id) == "deferred"


async def test_retry_count_lives_in_redis(queue) -> None:
    """可恢复错误 → `Retry(defer=...)` → **arq 的重投机制真的被走了一遍**。

    这条证明三件事（三件都是内存队列**假设**的 arq 行为）：

    1. 我们的退避（内核算的秒数）真的把任务推到了未来；
    2. `ctx["job_try"]` 由 arq 用 `INCR arq:retry:<job_id>` 维护，**重投时从 1 变 2**；
    3. `Worker.jobs_retried` 记下了这次重投（而不是「任务直接失败」）。

    ⚠️ 实测的一个重要细节（第一版用例就是被它坑红的）：
    **`arq:retry:<job_id>` 这个键在任务收场时会被删掉**
    （`finish_job` 的 `delete_keys += [retry_key_prefix + job_id, ...]`），
    所以「任务跑完之后去 Redis 里读重试计数」永远读不到 ——
    能读到的证据是 `job_try`（任务自己看到的）与 `Worker.jobs_retried`。
    """
    from app.tools.errors import RetryableToolError

    seen_tries: list[int] = []

    async def runner(payload):
        seen_tries.append(len(seen_tries) + 1)
        if len(seen_tries) == 1:
            raise RetryableToolError("下游 503")
        return {"summary": "第二次成功"}

    outcome = await _enqueue(queue, _job_id())
    worker = await _run_burst(queue, runner=runner, limit_seconds=8.0)

    assert len(seen_tries) >= 2, f"重投应当跑第二轮（实测 {len(seen_tries)} 轮）"
    assert worker.jobs_retried >= 1, "arq 应当记下一次重投"

    result = await queue.result(outcome.job_id)
    assert result["summary"] == "第二次成功"
    assert result["attempts"] >= 2, f"任务看到的 job_try 应当 >= 2（实测 {result['attempts']}）"
    # 重试键在收场时被 arq 删掉（见 docstring）——这条是「实测」而不是「以为」
    assert await queue._redis.get(f"arq:retry:{outcome.job_id}") is None  # noqa: SLF001


# ── 内核信号（与 Redis 无关，放在这里做对照）─────────────────────────────────


async def test_execute_job_plans_retry_for_a_retryable_error() -> None:
    """「重投延迟由**内核**算」这条在离线用例里也成立（这里做一次对照）。

    真 worker 只负责把 `RetryPlanned.defer` 翻成 arq 的 `Retry(defer=...)`
    （见 `worker.bridge_job`）—— 所以这个数字不该由传输层决定。
    """
    policy = RetryPolicy.jitless(attempts=2, base_delay=7.0, max_delay=7.0)

    async def run():
        from app.tools.errors import RetryableToolError

        raise RetryableToolError("下游 503")

    with pytest.raises(RetryPlanned) as caught:
        await execute_job(
            job_id="q_test:redis:signal",
            function=FUNC_REPORT,
            kind="report",
            payload={},
            attempt=1,
            run=run,
            policy=policy,
        )
    assert caught.value.defer == 7.0


def test_queue_unavailable_error_is_actionable() -> None:
    """空 `REDIS_URL` 的报错必须**能照着做**（而不是一句「连接失败」）。"""
    with pytest.raises(QueueUnavailable) as caught:
        redis_settings_from_url("")
    assert "REDIS_URL" in str(caught.value)


def test_enqueue_report_is_async_and_accepts_an_injected_queue() -> None:
    """入队函数是协程、且接受注入的队列（`12.queue.md` §2 的用法前提）。"""
    assert inspect.iscoroutinefunction(enqueue_report)
    assert "queue" in inspect.signature(enqueue_report).parameters


def test_cleanup_keys_are_derived_not_discovered() -> None:
    """本文件的清理**不用 `KEYS` 通配**（那是生产 Redis 上的危险操作）。

    这条是「装置本身也要能被 review」的一条：源码级断言，防止将来有人图省事
    换成通配扫描 —— 那会在共享 Redis 上删掉别人的任务。
    """
    body = pathlib.Path(__file__).read_text(encoding="utf-8")
    banned = "redis." + "keys("
    assert banned not in body, "清理不许用 KEYS 通配：按已知的 job_id 精确删"


# ── 助手 ─────────────────────────────────────────────────────────────────────


async def _run_burst(queue, *, runner, limit_seconds: float):
    """起一个 `burst=True` 的**真 worker** 跑一轮（跑完队列就退出），返回那个 `Worker`。

    ⚠️ 三处刻意的设置：

    * `handle_signals=False`：测试进程里装信号处理器会让 pytest 自己的 Ctrl-C 处理失效
      （而且在 Windows 上 arq 支持的信号集也与 Linux 不同）；
    * 任务注册用的是**生产的那一套名字**（`FUNC_REPORT` 就是
      `registered_functions()` 注册的名字）+ 注入的 runner —— 于是这条路径验的是
      「真 worker 能调到 `bridge_job`」，而不是一个平行实现；
    * 返回 `Worker` 让用例能读 `jobs_retried` / `jobs_complete` / `jobs_failed`
      —— 这三个计数是「重投机制真的走了一遍」的**直接证据**。
    """
    from arq.worker import Worker, func

    from app.queue.worker import MAX_ATTEMPTS, bridge_job

    functions = [
        func(
            _make_bridge(bridge_job, runner),
            name=FUNC_REPORT,
            timeout=limit_seconds,
            keep_result=3600,
        )
    ]
    worker = Worker(
        functions,
        queue_name=queue.queue_name,
        redis_pool=queue._redis,  # noqa: SLF001 - 复用同一个连接池（避免两个池各自持锁）
        burst=True,
        handle_signals=False,
        max_jobs=1,
        poll_delay=0.05,
        max_tries=MAX_ATTEMPTS,
        job_timeout=limit_seconds,
    )
    task = asyncio.create_task(worker.async_run())
    deadline = time.monotonic() + limit_seconds
    while not task.done() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return worker


def _make_bridge(bridge_job, runner):
    """把注入的 runner 绑进桥函数（arq 只接受一个 `(ctx, payload)` 协程）。

    ⚠️ 这里补上 `ctx["function"]`：生产的 `_bridge_for` 会做这件事
    （arq 自己不往 ctx 里放函数名），少了它 `policy_for("")` 会落到
    「不重试」那一档 —— 症状是「可恢复的错误第一次就进死信」（本仓实测踩到）。
    """

    async def bridge(ctx, payload):
        ctx.setdefault("function", FUNC_REPORT)
        return await bridge_job(ctx, payload, runner=runner)

    bridge.__qualname__ = FUNC_REPORT
    return bridge
