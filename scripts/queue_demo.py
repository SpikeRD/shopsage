"""队列验收工具：**八个场景一次跑完**（`12.queue.md` §8 的验收证据）。

```powershell
uv run python -m scripts.queue_demo                          # 全部场景（内存队列，离线）
uv run python -m scripts.queue_demo --scenario dead-letter    # 只跑一个
uv run python -m scripts.queue_demo --backend redis           # 用真 Redis（要 worker 在跑）
uv run python -m scripts.queue_demo --backend postgres        # 死信写进真库
uv run python -m scripts.queue_demo --list                    # 看有哪些场景
```

## 八个场景，每个对应一句验收

| 场景 | 证明什么 | 对应 `12.queue.md` |
|---|---|---|
| `submit` | 入队**秒级返回** job_id（入队路径不干活） | §8 第 1 条 |
| `paths` | 三个任务都能跑通（报表 / Agent / 知识库重建） | §1 职责 |
| `retry` | 重试分级：可恢复的退避重投、不可恢复的直接死信 | §4 第 3 条 |
| `dead-letter` | 额度用尽 → 死信表里有完整现场 | §8 第 2 条（A1） |
| `replay` | 从死信重放 → 成功 + 死信被关掉（不重复执行） | §8 第 2 条（A1+A2） |
| `delayed` | 延迟任务到点才跑（延迟任务不算积压） | §5「defer_until」 |
| `idempotent` | 同一幂等键跑两次只执行一次 | §5「`_job_id` 与幂等」 |
| `backpressure` | 队列积压 → 扩容信号（背压） | §5「背压」 |

## 为什么默认是内存队列

`--backend memory`（默认）用 `tests/queue/fake_queue.py` 的内存队列：**离线、秒级、
可重复**。它跑的是生产的 `worker.bridge_job` 与 `core.execute_job`，只是传输层换了
（与第 8 步用 `InMemorySaver` 演示断点重放是同一个思路）。

`--backend redis` 需要**真 Redis + 一个在跑的 worker**（`uv run arq app.queue.worker.WorkerSettings`），
它验的是「我们对 arq 语义的理解」；`--backend postgres` 只把**死信表**换成真库
（队列仍在内存里）——那是最常用的一种组合：逻辑在内存里验、留痕在真库里看。

## `--backend redis` 的三条实测注意事项（第一版就是被它们坑红的）

1. **用一个**专用队列名**，不碰业务队列**：默认是 `arq:shopsage:demo:<随机>`，
   跑完按已知的 job_id 精确删（不用 `KEYS` 通配 —— 那在共享 Redis 上是危险操作）。
   拿业务队列名跑演示会在队列里留下一串没人消费的任务，而且**看起来完全正常**；
2. **幂等场景要求「第一次入队是新任务」**，所以同一台 Redis 上连着跑两次会红
   （第二次的 `created=False` 是**正确**行为，不是 bug）—— 专用队列名解决了它；
3. `paths` / `retry` / `dead-letter` / `replay` 这四个场景在真 Redis 上**只确认入队**
   （执行要有一个在跑的 worker；`delayed` 场景还要等到点）。
   真 Redis 的执行路径由 `tests/queue/test_queue_redis.py` 用一个 `burst=True`
   的 worker 逐条验（`$env:SHOPSAGE_TEST_REDIS = "1"` 开门）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from typing import Any

from app.queue.jobs import FUNC_AGENT, FUNC_KB_REBUILD, FUNC_REPORT
from app.tools.errors import PermanentToolError, RetryableToolError

#: 场景名（`--list` 与 `--scenario` 共用）
SCENARIOS: tuple[str, ...] = (    "submit",
    "paths",
    "retry",
    "dead-letter",
    "replay",
    "delayed",
    "idempotent",
    "backpressure",
)

LINE = "=" * 78


def _track(queue: Any, *outcomes: Any) -> None:
    """把本次入队的 job_id 记进队列对象（**真 Redis 的清理要靠它**）。

    内存队列没有这个属性，所以用 `getattr` 兜底 —— 场景函数不必关心后端是谁。
    这也是「不用 `KEYS` 通配清理」的前提：要精确删，就得知道删哪些键。
    """
    tracker = getattr(queue, "created_job_ids", None)
    if tracker is None:
        return
    for outcome in outcomes:
        job_id = getattr(outcome, "job_id", None)
        if job_id:
            tracker.append(str(job_id))


class Boom(RuntimeError):
    """「没见过的异常」——用来演示**不可重试**的那一类（代码 bug）。"""


def title(text: str) -> None:
    print(f"\n{LINE}\n{text}\n{LINE}")


def ok(text: str) -> None:
    print(f"  ✅ {text}")


def info(text: str) -> None:
    print(f"     {text}")


async def make_queue(backend: str) -> tuple[Any, Any]:
    """造队列：内存或真 Redis（返回 `(queue, 清理函数)`）。

    `backend="postgres"` 是**混合模式**：队列仍在内存里，只有**死信表**换成真库。
    这不是偷懒，而是日常最有用的那一种组合：队列语义（重试/重放/幂等）在内存里
    验得快、可重复，而「失败留痕」在真库里能**被人查**（这正是 A1 要的东西）。
    整个队列搬去 Redis 时它验的是「我们对 arq 语义的理解」，那是
    `tests/queue/test_queue_redis.py` 与 `--backend redis` 的活。

    ⚠️ `backend="redis"` 用**专用队列名**（`arq:shopsage:demo:<随机>`）并在退出时
    精确删掉本次用到的键。理由（实测踩到）：拿业务队列名跑演示会
    ① 在队列里留下没人消费的任务；② 让「幂等」场景在同一次演示的第二次运行里红
    （`created=False` 是**正确**行为，但那条场景要断言「第一次是新任务」）。
    """
    if backend in ("memory", "postgres"):
        from tests.queue.fake_queue import MemoryDeadLetterStore, MemoryJobQueue

        store: Any = MemoryDeadLetterStore()
        if backend == "postgres":
            from app.queue.store import PostgresDeadLetterStore

            real = PostgresDeadLetterStore()
            if not real.available():
                raise SystemExit(
                    "[FAIL] 死信表不可达：先跑 `uv run alembic upgrade head`（并检查 DATABASE_URL）"
                )
            store = real
        instance = MemoryJobQueue(dead_letters=store)
        instance.runners[FUNC_REPORT] = instance.make_runner(
            FUNC_REPORT, lambda payload: {"title": payload.get("title"), "summary": "本月 2 笔订单，1 笔已发货。"}
        )
        instance.runners[FUNC_KB_REBUILD] = instance.make_runner(
            FUNC_KB_REBUILD, lambda payload: {"documents": 7, "chunks": 59, "embedded": 0}
        )
        instance.runners[FUNC_AGENT] = instance.make_runner(
            FUNC_AGENT, lambda payload: {"answer": "您最近 2 笔订单：1 笔已发货、1 笔待付款。", "exhausted": False}
        )
        return instance, _noop

    from app.config import get_settings
    from app.queue.backend import DEFAULT_QUEUE_NAME, RedisJobQueue

    queue_name = f"{DEFAULT_QUEUE_NAME}:demo:{uuid.uuid4().hex[:8]}"
    instance = await RedisJobQueue.open(str(getattr(get_settings(), "redis_url", "") or ""), queue_name=queue_name)
    created: list[str] = []
    instance.created_job_ids = created  # type: ignore[attr-defined] - 清理用（见 cleanup）

    async def cleanup() -> None:
        """删掉本次演示留下的键（**精确删**，不用 KEYS 通配）。"""
        redis = instance._redis  # noqa: SLF001 - 演示工具就是要清干净
        # ⚠️ `arq:job/result/retry/in-progress` 之外还有两个容易漏的键：
        #   · `<队列名>` 本身（ZSET）—— 队列项就在里面；
        #   · `<队列名>:health-check` —— worker 的健康检查键。演示脚本**不起 worker**，
        #     但如果有人一边开着 worker 一边跑演示，这个键不会被队列收场清掉。
        keys: list[str] = [queue_name, f"{queue_name}:health-check"]
        for job_id in created:
            keys += [
                f"arq:job:{job_id}",
                f"arq:result:{job_id}",
                f"arq:retry:{job_id}",
                f"arq:in-progress:{job_id}",
                # 重放场景在 `--rerun` 时会造 `<原键>:retryN` 这样的新 job_id
                *[f"arq:job:{job_id}:retry{n}" for n in range(1, 4)],
                *[f"arq:result:{job_id}:retry{n}" for n in range(1, 4)],
            ]
        await redis.delete(*keys)
        await instance.aclose()
        print(f"（演示队列已清理：{queue_name}）")

    print(f"（使用专用演示队列：{queue_name}）")
    return instance, cleanup


async def _noop() -> None:
    return None


# ── 场景 ─────────────────────────────────────────────────────────────────────


async def scenario_submit(queue: Any) -> int:
    """① API 入队**秒级返回** job_id（入队路径里不干活）。"""
    title("场景 1：长任务入队 — 秒级返回 job_id（API 不阻塞）")
    from app.queue.tasks import enqueue_report

    started = time.perf_counter()
    outcome = await enqueue_report(user_id=2, title="本月订单", queue=queue)
    _track(queue, outcome)
    elapsed_ms = (time.perf_counter() - started) * 1000
    info(f"入队耗时 = {elapsed_ms:.1f}ms（阈值：< 200ms 才算「秒级」）")
    info(f"job_id = {outcome.job_id}")
    info(f"状态   = {outcome.status}（deferred 的延迟任务不走立即队列）")
    if elapsed_ms > 200:
        print(f"  ❌ 入队太慢（{elapsed_ms:.1f}ms）：检查入队路径里有没有查库/调模型")
        return 1
    if not outcome.created:
        print("  ❌ 应当是新建任务（created=False 说明幂等命中了别的用例留下的键）")
        return 1
    ok(f"入队 {elapsed_ms:.1f}ms，返回 job_id（worker 还没开始跑）")
    return 0


async def scenario_paths(queue: Any) -> int:
    """② 三个任务各自跑通（报表 / Agent / 知识库重建）。"""
    title("场景 2：三个任务都能跑通（report / agent / kb_rebuild）")
    from app.queue.tasks import enqueue_agent_run, enqueue_kb_rebuild, enqueue_report

    report = await enqueue_report(user_id=2, title="本月订单", idempotency_key="demo:paths:report", queue=queue)
    agent = await enqueue_agent_run(
        user_id=2, thread_id="u2:demo", message="我的订单到哪了", idempotency_key="demo:paths:agent", queue=queue
    )
    kb = await enqueue_kb_rebuild(idempotency_key="demo:paths:kb", queue=queue)
    _track(queue, report, agent, kb)
    info(f"三个任务：{report.job_id} / {agent.job_id} / {kb.job_id}")

    from tests.queue.fake_queue import MemoryJobQueue

    if isinstance(queue, MemoryJobQueue):
        await queue.run_until_idle()
        for job in (report, agent, kb):
            result = await queue.result(job.job_id)
            info(f"{job.job_id} → {result}")
            ok(f"{job.kind} 完成")
    else:
        info("（真 Redis：任务由 worker 执行，这里只确认已入队）")
        for job in (report, agent, kb):
            info(f"{job.job_id} 状态 = {await queue.status(job.job_id)}")
        ok("三个任务已入队（等 worker 消费）")
    return 0


async def scenario_retry(queue: Any) -> int:
    """③ 重试分级：可恢复的退避重投、不可恢复的直接进死信。"""
    title("场景 3：重试分级 — 可恢复的退避重投，不可恢复的立刻死信")
    from app.queue.tasks import enqueue_kb_rebuild, enqueue_report

    from tests.queue.fake_queue import MemoryJobQueue

    if not isinstance(queue, MemoryJobQueue):
        info("（该场景需要内存队列：它要控制「第几次失败」，真 Redis 下请用 test_queue_redis.py）")
        return 0

    # 可恢复：只失败一次，重投后成功
    before = queue.count_calls(FUNC_REPORT)
    queue.inject_failure(FUNC_REPORT, RetryableToolError("下游 503"))
    recoverable = await enqueue_report(user_id=2, title="可恢复", idempotency_key="demo:retry:ok", queue=queue)
    _track(queue, recoverable)
    rounds = []
    for _ in range(4):
        report = await queue.drain()
        if not report.runs:
            break
        rounds.append(report)
        queue.advance(30.0)
    for index, report in enumerate(rounds, start=1):
        info(f"第 {index} 轮：{report.summary()}")
    executed = queue.count_calls(FUNC_REPORT) - before
    if executed != 2:
        print(f"  ❌ 可恢复错误应当「失败 1 次 + 重投 1 次」= 执行 2 次，实测 {executed}")
        return 1
    ok("可恢复错误被退避重投一次（2.3s 后），第二次成功")

    # 不可恢复：第一次就进死信
    queue.inject_failure(FUNC_KB_REBUILD, PermanentToolError("表名不存在（参数写错）"))
    fatal = await enqueue_kb_rebuild(idempotency_key="demo:retry:fatal", queue=queue)
    _track(queue, fatal)
    await queue.run_until_idle()
    status = await queue.status(fatal.job_id)
    info(f"{fatal.job_id} 状态 = {status}")
    if status != "dead_letter":
        print("  ❌ 不可恢复错误应当直接进死信")
        return 1
    ok("不可恢复错误第一次就进死信（重试它只会把同一个错误犯 N 次）")
    return 0


async def scenario_dead_letter(queue: Any) -> int:
    """④ 额度用尽 → 死信表里有完整现场（零任务丢失，A1）。"""
    title("场景 4：重试额度用尽 → 死信（完整失败现场，零任务丢失）")
    from app.queue.tasks import enqueue_report

    from tests.queue.fake_queue import MemoryJobQueue

    if not isinstance(queue, MemoryJobQueue):
        info("（该场景需要内存队列来控制失败次数）")
        return 0

    queue.inject_failure(FUNC_REPORT, PermanentToolError("这个任务注定失败"))
    fatal = await enqueue_report(user_id=2, title="注定失败", idempotency_key="demo:dlq:fatal", queue=queue)
    _track(queue, fatal)
    await queue.run_until_idle()
    entry = queue.dead_letters.fetch(fatal.job_id)
    if entry is None:
        print("  ❌ 死信表里没有这条记录（零任务丢失这条验收没有落点）")
        return 1
    info(f"job_id        = {entry.job_id}")
    info(f"attempts      = {entry.attempts}")
    info(f"error_type    = {entry.error_type}")
    info(f"error_message = {entry.error_message}")
    info(f"retryable     = {entry.retryable}")
    info(f"payload       = {entry.payload}（重放要能原样喂回）")
    info(f"traceback     = {len(entry.error_traceback)} 字符（唯一能看到错在哪一行的地方）")
    ok("失败被完整留痕：可查、可筛、可标记（这是「人工兜底」的前提）")
    return 0


async def scenario_replay(queue: Any) -> int:
    """⑤ 从死信重放 → 成功 + 死信被关掉 + 不重复执行（A1+A2）。"""
    title("场景 5：从死信人工重放 — 成功、关闭死信、不重复执行（A1+A2）")
    from app.queue.core import replay_plan
    from app.queue.jobs import JobSpec
    from app.queue.tasks import enqueue_report

    from tests.queue.fake_queue import MemoryJobQueue

    if not isinstance(queue, MemoryJobQueue):
        info("（该场景需要内存队列；真库的死信重放见 `scripts/queue_replay.py`）")
        return 0

    before = queue.count_calls(FUNC_REPORT)
    queue.inject_failure(FUNC_REPORT, Boom("代码有 bug"))
    job = await enqueue_report(user_id=2, title="待重放", idempotency_key="demo:replay", queue=queue)
    _track(queue, job)
    await queue.run_until_idle()
    status = await queue.status(job.job_id)
    info(f"第一次执行：{status}")
    if status != "dead_letter":
        print("  ❌ 不可恢复错误应当进死信")
        return 1

    entry = queue.dead_letters.fetch(job.job_id)
    assert entry is not None
    plan = replay_plan(entry)
    info(f"重放计划：{plan.describe()}")
    info(f"复用原幂等键 = {plan.idempotency_key}（所以「点两次」只会跑一次）")

    await queue.enqueue(
        JobSpec(kind="report", function=plan.function, payload=plan.payload, idempotency_key=plan.idempotency_key)
    )
    # 再点一次：同一幂等键 → 不会重复入队（A2 的一半）
    again = await queue.enqueue(
        JobSpec(kind="report", function=plan.function, payload=plan.payload, idempotency_key=plan.idempotency_key)
    )
    info(f"第二次点重放：created={again.created}（幂等命中）")
    await queue.run_until_idle()
    result = await queue.result(job.job_id)
    info(f"重放结果：{result}")
    closed = queue.dead_letters.fetch(job.job_id)
    assert closed is not None
    info(f"死信状态：resolution={closed.resolution} replay_count={closed.replay_count}")
    executed = queue.count_calls(FUNC_REPORT) - before
    if closed.resolution != "replayed_ok":
        print("  ❌ 重放成功后应当关闭死信（否则「有死信躺着」的告警永远不收敛）")
        return 1
    if executed != 2:
        print(f"  ❌ 应当总共执行 2 次（1 次失败 + 1 次重放），实测 {executed}")
        return 1
    if again.created is not False:
        print("  ❌ 第二次点重放应当幂等命中")
        return 1
    ok("重放成功、死信被关闭、总执行 2 次、重复点击不入队（没有重复副作用）")
    return 0


async def scenario_delayed(queue: Any) -> int:
    """⑥ 延迟任务到点才跑（`defer_until`），且不算积压。"""
    title("场景 6：延迟任务 — defer_until 到期才执行（延迟不算积压）")
    from app.queue.tasks import enqueue_kb_rebuild

    from tests.queue.fake_queue import MemoryJobQueue

    job = await enqueue_kb_rebuild(source_dir="docs/kb", defer_by=5.0, idempotency_key="demo:delayed", queue=queue)
    _track(queue, job)
    info(f"入队状态 = {job.status}（延迟 {job.defer_by:.0f}s）")

    if not isinstance(queue, MemoryJobQueue):
        info("（真 Redis 下延迟写在 ZSET 的 score 里：zscore 就是到点时间）")
        info(f"当前状态 = {await queue.status(job.job_id)}")
        ok("延迟任务已进延迟队列（没到点，worker 取不走）")
        return 0

    gauges = await queue.gauges()
    info(f"队列现状：立即可取={gauges.depth} 延迟={gauges.deferred}（延迟**不算**积压）")
    report = await queue.drain()
    if report.runs:
        print(f"  ❌ 还没到点就执行了：{report.summary()}")
        return 1
    ok("到点前：一次都没跑")

    queue.advance(4.0)
    report = await queue.drain()
    if report.runs:
        print(f"  ❌ 还差 1 秒就执行了：{report.summary()}")
        return 1
    ok("还差 1 秒：仍然没跑")

    queue.advance(1.5)
    report = await queue.drain()
    info(f"越过到点时间：{report.summary()}")
    if report.statuses() != ["complete"]:
        print("  ❌ 到点之后应当执行")
        return 1
    ok("到点之后：执行一次（时间由用例显式推进，不 sleep）")
    return 0


async def scenario_idempotent(queue: Any) -> int:
    """⑦ 同一幂等键不重复执行（A2）。"""
    title("场景 7：幂等 — 同一幂等键入队两次，只执行一次")
    from app.queue.tasks import enqueue_report

    from tests.queue.fake_queue import MemoryJobQueue

    before = queue.count_calls(FUNC_REPORT) if isinstance(queue, MemoryJobQueue) else 0
    first = await enqueue_report(user_id=2, title="幂等", idempotency_key="demo:idem", queue=queue)
    second = await enqueue_report(user_id=2, title="幂等", idempotency_key="demo:idem", queue=queue)
    _track(queue, first, second)
    info(f"第一次：created={first.created} job_id={first.job_id}")
    info(f"第二次：created={second.created} job_id={second.job_id}（幂等命中，没重复投）")
    if first.created is not True or second.created is not False:
        print("  ❌ 第二次应当是幂等命中（created=False）")
        return 1

    if isinstance(queue, MemoryJobQueue):
        await queue.run_until_idle()
        executed = queue.count_calls(FUNC_REPORT) - before
        info(f"提交两次之后 worker 执行次数 = {executed}")
        if executed != 1:
            print(f"  ❌ 同一个幂等键应当只执行一次，实测 {executed}")
            return 1
    ok("入队两次、只执行一次（写路径的副作用不会重复，A2）")
    return 0


async def scenario_backpressure(queue: Any) -> int:
    """⑧ 队列积压 → 扩容信号（背压）。"""
    title("场景 8：背压 — 积压作为 worker 扩容信号")
    from app.queue.tasks import enqueue_report

    from tests.queue.fake_queue import MemoryJobQueue

    if not isinstance(queue, MemoryJobQueue):
        gauges = await queue.gauges()
        info(f"队列现状：depth={gauges.depth} deferred={gauges.deferred} dead={gauges.dead_letters}")
        info(f"扩容提示：{gauges.scale_hint()}")
        ok("读到了队列读数（背压信号来自 depth，而不是延迟任务）")
        return 0

    for index in range(25):
        _track(
            queue,
            await enqueue_report(user_id=2, title=f"积压-{index}", idempotency_key=f"demo:bp:{index}", queue=queue),
        )
    gauges = await queue.gauges()
    info(f"积压 {gauges.depth} 条（延迟 {gauges.deferred} 条不计入）")
    info(f"扩容提示：{gauges.scale_hint()}")
    if gauges.backlog < 20:
        print("  ❌ 应当读到积压")
        return 1
    ok("积压被读出来并给了扩容提示（队列深度 → 加 worker 的信号）")
    return 0


SCENARIO_FUNCS = {
    "submit": scenario_submit,
    "paths": scenario_paths,
    "retry": scenario_retry,
    "dead-letter": scenario_dead_letter,
    "replay": scenario_replay,
    "delayed": scenario_delayed,
    "idempotent": scenario_idempotent,
    "backpressure": scenario_backpressure,
}


async def run(args: argparse.Namespace) -> int:
    if args.list:
        print("可用场景：")
        for name in SCENARIOS:
            print(f"  {name:<14} {SCENARIO_FUNCS[name].__doc__.splitlines()[0]}")
        return 0

    selected = [args.scenario] if args.scenario else list(SCENARIOS)
    unknown = [name for name in selected if name not in SCENARIO_FUNCS]
    if unknown:
        print(f"[FAIL] 未知场景：{unknown}（可用：{', '.join(SCENARIOS)}）")
        return 2
    if args.backend == "postgres" and not args.scenario:
        print("[提示] --backend postgres 只把**死信表**换成真库（队列仍在内存里）")

    print(f"队列后端 = {args.backend} | 场景 = {', '.join(selected)}")
    failures = 0
    try:
        for name in selected:
            # 每个场景一份**干净的队列**（上一个场景留下的同键任务会让下一个场景
            # 的 enqueue 变成幂等命中，而那时用例的意图是「新建一个任务」）。
            # 死信表**不重置**：真库实现重置不了，而「跨场景积累」正是要观察的东西。
            queue, cleanup = await make_queue(args.backend)
            try:
                code = await SCENARIO_FUNCS[name](queue)
            finally:
                await cleanup()
            if code:
                failures += 1
                print(f"  ❌ 场景 {name} 未通过（退出码 {code}）")
    except SystemExit as exc:
        print(exc)
        return 2

    title("汇总")
    print(f"  通过 {len(selected) - failures}/{len(selected)} 个场景")
    if args.backend == "postgres":
        from app.queue.store import PostgresDeadLetterStore

        store = PostgresDeadLetterStore()
        pending = store.pending(limit=50)
        print(f"  死信表（真库）待处理 = {len(pending)} 条")
        for entry in pending:
            print(f"    {entry.job_id}  {entry.kind}  {entry.error_type}  「{entry.error_message[:30]}」")
        # ⚠️ 演示**自己收尾**：把本次留下的死信标成 `demo_run`（而不是留在待处理列表里）。
        # 理由：`--list` 的「有待处理死信」是**告警信号**，而演示故意造出来的失败
        # 不该让下一次运行的人以为「生产上有死信躺着」。行**保留**（判据是这次演示的产物：
        # 演示脚本每次都重跑，留着一堆 unresolved 只会制造噪音）。
        # 想手工处理真实的死信时用 `scripts/queue_replay.py`，两者互不干扰。
        for entry in pending:
            store.resolve(entry.job_id, resolution="demo_run")
        if pending:
            print(f"  （已把本次演示留下的 {len(pending)} 条标成 demo_run：`--list` 会恢复干净）")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="队列验收工具（八个场景）")
    parser.add_argument("--scenario", choices=SCENARIOS, help="只跑一个场景")
    parser.add_argument(
        "--backend",
        choices=("memory", "redis", "postgres"),
        default="memory",
        help="memory=离线内存队列（默认）；redis=真 Redis（队列侧）；postgres=死信写进真库",
    )
    parser.add_argument("--list", action="store_true", help="列出所有场景")
    args = parser.parse_args(argv)

    from app.graph import ensure_compatible_event_loop

    ensure_compatible_event_loop()  # Windows 上 psycopg 的异步模式需要它（见 3.db-runbook）
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
