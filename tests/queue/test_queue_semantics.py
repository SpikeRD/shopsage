"""队列语义的离线用例：**重试分级 → 死信 → 重放**（`12.queue.md` §7 的第 2、3 条）。

这四件事（长期任务返回 job_id、重试到死信、从死信重放、延迟任务）在本文件里
全部用**内存队列**验完 —— 不连 Redis、不连库、不调模型。

为什么能做到：`app/queue/core.py` 刻意不 import arq（见那里的 docstring），
而 `tests/queue/fake_queue.py` 复刻了 arq 的四条关键语义（重复 _job_id、
defer_by、job_try、Retry），并用**生产的** `worker.bridge_job` 执行任务。
所以这里跑的是真代码，只是传输层换成了内存。
"""

from __future__ import annotations

import pytest

from app.llm.errors import PermanentLLMError, RetryableLLMError
from app.queue.core import PermanentTaskError, RetryTask, classify, execute_job
from app.queue.jobs import DEFAULT_POLICIES, FUNC_KB_REBUILD, FUNC_REPORT, RetryPolicy
from app.tools.errors import PermanentToolError, RetryableToolError, ToolContractError

#: 测试用的退避策略：**无抖动**（断言「等了几秒」时不能容忍随机）
POLICY = RetryPolicy.jitless(attempts=3, base_delay=1.0, max_delay=10.0)


class Boom(RuntimeError):
    """一个「没见过的异常」——`classify` 应当把它判成**不可重试**。"""


# ── ① 错误分类（`classify`）─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RetryTask("限流"), True),
        (RetryableToolError("库在重启"), True),
        (RetryableLLMError("429"), True),
        (PermanentTaskError("缺 user_id"), False),
        (PermanentToolError("约束冲突"), False),
        (ToolContractError("枚举值非法"), False),
        (PermanentLLMError("401"), False),
        (Boom("代码有 bug"), False),
        (KeyError("payload 少了一个键"), False),
        (ValueError("参数写错"), False),
    ],
)
def test_classify_merges_both_whitelists(exc, expected):
    """分类是**归并**第 5、6 步的白名单，只此一处。

    ⚠️ 最后三行是本表最要紧的：`Boom` / `KeyError` / `ValueError` 都是**没见过的异常**，
    默认方向必须是「不重试」—— 重投 100 次只会把同一个 bug 犯 100 次，
    还会把重试额度耗光，让真正「运气不好」的任务排在它后面。
    """
    assert classify(exc) is expected


def test_retry_task_can_override_the_delay():
    """任务可以用 `RetryTask(defer=...)` 指定「等多久」（它比策略更懂下游的限流头）。"""
    assert RetryTask("429", defer=42.0).defer == 42.0


# ── ② 执行内核的四条收场路径 ─────────────────────────────────────────────────


async def test_execute_job_success_returns_value_and_metrics():
    calls = {"n": 0}

    async def run():
        calls["n"] += 1
        return {"ok": True}

    outcome = await execute_job(
        job_id="q_a",
        function=FUNC_REPORT,
        kind="report",
        payload={},
        attempt=1,
        run=run,
        policy=POLICY,
    )
    assert outcome.status == "succeeded"
    assert outcome.value == {"ok": True}
    assert calls["n"] == 1


async def test_execute_job_plans_retry_when_budget_remains(dead_letters):
    """可恢复错误 + 还有额度 → `retry_planned`，**不进死信**。

    这一条与下一条合起来才是「重试分级」：可恢复错误进死信会让死信被噪音灌满
    （人工兜底的入口就没人看了）。
    """

    async def run():
        raise RetryableToolError("下游 503")

    with pytest.raises(Exception) as caught:  # RetryPlanned（内部信号）
        await execute_job(
            job_id="q_b",
            function=FUNC_REPORT,
            kind="report",
            payload={},
            attempt=1,
            run=run,
            policy=POLICY,
            dead_letters=dead_letters,
        )
    assert type(caught.value).__name__ == "RetryPlanned"
    assert caught.value.defer == 1.0  # base_delay（无抖动）
    assert dead_letters.records == {}


async def test_execute_job_dead_letters_when_budget_exhausted(dead_letters):
    """可恢复错误 + 额度用尽 → 进死信，**带完整失败现场**。"""

    async def run():
        raise RetryableToolError("下游 503")

    outcome = await execute_job(
        job_id="q_c",
        function=FUNC_REPORT,
        kind="report",
        payload={"user_id": 2, "title": "本月"},
        attempt=3,  # 第三次 = 额度用尽
        run=run,
        policy=POLICY,
        dead_letters=dead_letters,
        user_id=2,
    )
    assert outcome.status == "dead_letter"
    entry = dead_letters.records["q_c"]
    assert entry.retryable is True
    assert entry.attempts == 3
    assert entry.error_type == "RetryableToolError"
    assert "下游 503" in entry.error_message
    assert "RetryableToolError" in entry.error_traceback  # 完整栈（排障唯一能看到错在哪的地方）
    assert entry.payload == {"user_id": 2, "title": "本月"}  # 重放要能原样喂回
    assert entry.user_id == 2
    assert entry.policy is not None and entry.policy.attempts == 3


async def test_execute_job_dead_letters_permanent_errors_immediately(dead_letters):
    """不可恢复错误第**一次**就进死信（`retryable=False`）。

    这是「重试上限 + 死信缺一不可」里「死信」那一半：参数写错/代码有 bug 重试无意义，
    但**必须留痕** —— 否则它只存在于 Redis 的失败结果里（24 小时后消失）。
    """

    async def run():
        raise PermanentTaskError("报表任务缺少 user_id")

    outcome = await execute_job(
        job_id="q_d",
        function=FUNC_REPORT,
        kind="report",
        payload={},
        attempt=1,
        run=run,
        policy=POLICY,
        dead_letters=dead_letters,
    )
    assert outcome.status == "dead_letter"
    entry = dead_letters.records["q_d"]
    assert entry.retryable is False
    assert entry.attempts == 1


async def test_dead_letter_write_failure_is_reported_not_raised(dead_letters, caplog):
    """死信写不进去时：**吼出来、但不抛**（不能盖掉原始错误）。

    ⚠️ 这个分支值得一条用例：它的正确行为是「日志里有 ERROR + outcome 里没有死信」，
    而「顺手抛一个新异常」会盖掉原始错误（排障时最想看到的那个），
    也会让 arq 把两次失败混成一次。
    """
    dead_letters.fail_writes = True

    async def run():
        raise PermanentTaskError("参数错了")

    with caplog.at_level("ERROR"):
        outcome = await execute_job(
            job_id="q_e",
            function=FUNC_REPORT,
            kind="report",
            payload={},
            attempt=1,
            run=run,
            policy=POLICY,
            dead_letters=dead_letters,
        )
    assert outcome.status == "dead_letter"
    assert outcome.dead_letter is None  # 没落盘
    assert any("写死信记录失败" in record.message for record in caplog.records)


async def test_missing_dead_letter_store_is_loud(caplog):
    """没配死信表时**必须吼**（「零任务丢失」这条验收没有落点）。"""

    async def run():
        raise PermanentTaskError("参数错了")

    with caplog.at_level("ERROR"):
        await execute_job(
            job_id="q_f",
            function=FUNC_REPORT,
            kind="report",
            payload={},
            attempt=1,
            run=run,
            policy=POLICY,
            dead_letters=None,
        )
    assert any("没有配置死信表" in record.message for record in caplog.records)


# ── ③ 「重试 → 死信」的端到端（§7 第 2 条）──────────────────────────────────


async def test_retry_then_dead_letter_end_to_end(queue, dead_letters):
    """连续失败 N 次 → 进死信队列，**零任务丢失**。

    这是 `12.queue.md` §7 的第 2 条用例，跑在**生产的** `bridge_job` + `execute_job` 上。
    断言的是三个可观察事实：

    1. 任务真被调用了 3 次（= 策略的额度，不是 2 次也不是 5 次）；
    2. 三次退避是 1s → 2s（指数）；
    3. 死信表里有一条完整现场（`attempts=3`、错误类型、payload）。
    """
    from app.queue.tasks import enqueue_report
    from tests.queue.fake_queue import MemoryJobQueue

    assert isinstance(queue, MemoryJobQueue)
    # 三次都失败（第 3 次就是额度用尽）
    queue.inject_failure(FUNC_REPORT, *[RetryableToolError("下游 503")] * 3)

    outcome = await enqueue_report(user_id=2, title="本月", queue=queue)
    assert outcome.created is True

    # 每一轮都要推进时间到「重投点」才能再跑（重试是把任务推到未来）
    delays: list[float] = []
    for _ in range(4):
        report = await queue.drain()
        if not report.runs:
            break
        delays += [item.delay for item in report.runs if item.status == "retry"]
        queue.advance(5.0)  # 越过任何退避（base_delay=2s）
        if any(item.status == "failed" for item in report.runs):
            break

    assert queue.count_calls(FUNC_REPORT) == 3, "应当恰好执行 3 次（策略额度）"
    # ⚠️ 生产策略**带 ±20% 抖动**（`DEFAULT_POLICIES[FUNC_REPORT].jitter=0.2`），
    # 所以这里断言的是**区间 + 倍率**，而不是精确的 2.0/4.0。
    # 抖动是刻意的（一批同时失败的任务不该同时回来），而「确定性退避」由
    # `test_retry_policy_is_exponential_and_capped` 用 `jitless()` 单独钉住。
    #
    # ⚠️ 倍率的边界也必须**按抖动算出来**（本仓实测踩到）：两个独立抖动的样本，
    # 比值区间是 `(1-j)/(1+j) * 2 ~ (1+j)/(1-j) * 2`，抖动 0.2 时约 1.33~3.0。
    # 写死「1.7~2.3」会随机红（实测撞到过 3.98/1.62 = 2.46）。
    jitter = DEFAULT_POLICIES[FUNC_REPORT].jitter
    first, second = delays[0], delays[1]
    assert 2.0 * (1 - jitter) <= first <= 2.0 * (1 + jitter), f"第一次退避应当 ≈2s（实测 {first}）"
    assert 4.0 * (1 - jitter) <= second <= 4.0 * (1 + jitter), f"第二次退避应当 ≈4s（实测 {second}）"
    ratio = second / first
    low = 2.0 * (1 - jitter) / (1 + jitter)
    high = 2.0 * (1 + jitter) / (1 - jitter)
    assert low <= ratio <= high, f"退避应当大致翻倍（实测 {first} → {second}，比值 {ratio:.2f}）"
    entry = dead_letters.records[outcome.job_id]
    assert entry.attempts == 3
    assert entry.retryable is True
    assert entry.payload["user_id"] == 2
    assert await queue.status(outcome.job_id) == "dead_letter"


# ── ④ 从死信重放（§7 第 3 条）────────────────────────────────────────────────


async def test_replay_from_dead_letter_succeeds_and_closes_the_entry(queue, dead_letters):
    """从死信重放 → 最终成功，并且**死信被关掉**（列表收敛）。

    「关掉」这一步容易被漏：少了它，死信列表永远不收敛，
    「有死信躺着」这条告警会一直响（而那正是 A1 失效的形态）。
    """
    from app.queue.core import replay_plan
    from app.queue.tasks import enqueue_report

    queue.inject_failure(FUNC_REPORT, *[RetryableToolError("下游 503")] * 3)
    outcome = await enqueue_report(user_id=2, title="本月", queue=queue)
    for _ in range(4):
        report = await queue.drain()
        queue.advance(5.0)
        if not report.runs:
            break
    assert await queue.status(outcome.job_id) == "dead_letter"

    # 人工重放：复用原幂等键（同一 job_id → 不会重复执行）
    entry = dead_letters.fetch(outcome.job_id)
    assert entry is not None
    plan = replay_plan(entry)
    assert plan.job_id == outcome.job_id  # 同一个 id = 同一个幂等键
    assert plan.payload["dlq_key"] == outcome.job_id  # 成功之后要能关掉这条死信

    spec = __import__("app.queue.jobs", fromlist=["JobSpec"]).JobSpec(
        kind="report", function=plan.function, payload=plan.payload, idempotency_key=plan.idempotency_key
    )
    replay = await queue.enqueue(spec)
    report = await queue.drain()
    assert report.statuses() == ["complete"], "重放应当一次成功（不再注入失败）"
    assert queue.count_calls(FUNC_REPORT) == 4  # 前 3 次失败 + 第 4 次成功

    closed = dead_letters.fetch(outcome.job_id)
    assert closed is not None
    assert closed.resolution == "replayed_ok"
    assert closed.replay_count == 1
    assert replay.created is True


async def test_replay_plan_reuses_the_key_unless_explicitly_rerun(queue, dead_letters):
    """重放默认**复用原键**；显式重跑（`key_suffix`）才产生新键。

    前者保证「点两次只跑一次」（A2：写路径的副作用不重复）；
    后者是「原始输入就是错的、人工修好参数之后确实要再跑一次」的场景 ——
    而它必须**显式**（`retry2` 后缀），否则「重放」与「再跑一次」就分不清了。
    """
    from app.queue.core import replay_plan

    queue.inject_failure(FUNC_REPORT, *[RetryableToolError("x")] * 3)
    from app.queue.tasks import enqueue_report

    outcome = await enqueue_report(user_id=2, title="本月", queue=queue)
    for _ in range(4):
        report = await queue.drain()
        queue.advance(5.0)
        if not report.runs:
            break
    entry = dead_letters.fetch(outcome.job_id)
    assert entry is not None

    first = replay_plan(entry)
    third = replay_plan(entry, key_suffix="retry3")
    assert first.job_id == outcome.job_id
    assert third.job_id == f"{outcome.job_id}:retry3"
    assert third.job_id != first.job_id


# ── ⑤ 延迟任务（§7 第 4 条）─────────────────────────────────────────────────


async def test_delayed_task_only_runs_after_its_time(queue):
    """`defer_until` 到期才执行：**到点之前一次都不跑**。

    ⚠️ 时间是**显式推进**的（`advance`），不是 `sleep` ——
    用 `sleep` 的用例在 CI 上会变成 flaky（机器慢一点就红），而显式时间旅行
    既快又确定（见 `fake_queue.MemoryJobQueue.advance`）。
    """
    from app.queue.tasks import enqueue_kb_rebuild

    outcome = await enqueue_kb_rebuild(source_dir="docs/kb", defer_by=3.0, queue=queue)
    assert outcome.status == "deferred"
    assert await queue.status(outcome.job_id) == "deferred"

    assert (await queue.drain()).runs == [], "还没到点，不该执行"
    assert queue.count_calls(FUNC_KB_REBUILD) == 0

    queue.advance(2.0)  # 还差 1 秒
    assert (await queue.drain()).runs == []
    assert queue.count_calls(FUNC_KB_REBUILD) == 0

    queue.advance(1.5)  # 越过到点时间
    report = await queue.drain()
    assert report.statuses() == ["complete"]
    assert queue.count_calls(FUNC_KB_REBUILD) == 1

    gauges = await queue.gauges()
    assert gauges.deferred == 0


async def test_deferred_jobs_do_not_count_as_backlog(queue):
    """延迟任务**不算积压**（它是定时报表的正常形态，不是「排队的活」）。

    把延迟任务算进扩容信号，会让 worker 每天定时被加一批（上午 8 点的日报高峰）。
    """
    from app.queue.tasks import enqueue_kb_rebuild

    await enqueue_kb_rebuild(source_dir="docs/kb", defer_by=600.0, queue=queue)
    gauges = await queue.gauges()
    assert gauges.depth == 0
    assert gauges.deferred == 1
    assert gauges.scale_hint() == "积压正常"


# ── ⑥ 幂等（§7 第 5 条）─────────────────────────────────────────────────────


async def test_idempotent_enqueue_does_not_run_twice(queue):
    """同一幂等 key：**只执行一次**（第二次入队是「幂等命中」）。"""
    from app.queue.tasks import enqueue_report

    first = await enqueue_report(user_id=2, title="本月", queue=queue)
    second = await enqueue_report(user_id=2, title="本月", queue=queue)
    assert first.job_id == second.job_id
    assert first.created is True
    assert second.created is False

    await queue.run_until_idle()
    assert queue.count_calls(FUNC_REPORT) == 1

    # 第三次：任务已经**跑完并从队列里移除**了 —— 这时同一个键会真的再跑一次，
    # 因为「已完成」与「排队中」是两种状态（真 Redis 的 `enqueue` 用显式状态检查挡住它，
    # 见 `backend.RedisJobQueue.enqueue` 的说明）。本用例把这个边界钉住，
    # 免得将来有人以为「幂等键 = 永不再跑」。
    third = await enqueue_report(user_id=2, title="本月", queue=queue)
    assert third.created is True
    await queue.run_until_idle()
    assert queue.count_calls(FUNC_REPORT) == 2


async def test_different_params_produce_different_jobs(queue):
    """换一个参数就是**另一个任务**（幂等键从参数派生，不是从任务名派生）。"""
    from app.queue.tasks import enqueue_report

    first = await enqueue_report(user_id=2, title="本月", queue=queue)
    second = await enqueue_report(user_id=2, title="上月", queue=queue)
    assert first.job_id != second.job_id
    await queue.run_until_idle()
    assert queue.count_calls(FUNC_REPORT) == 2


async def test_explicit_idempotency_key_wins(queue):
    """调用方给了幂等键就用它 —— 于是**参数不同也能命中同一个任务**。

    这是「业务幂等键」的用途：上游单据号、图侧那一轮的 `chat:<thread>:<轮次>`。
    """
    from app.queue.tasks import enqueue_report

    first = await enqueue_report(user_id=2, title="本月", idempotency_key="biz:2026-10", queue=queue)
    second = await enqueue_report(user_id=2, title="随便改个标题", idempotency_key="biz:2026-10", queue=queue)
    assert first.job_id == second.job_id
    assert second.created is False
