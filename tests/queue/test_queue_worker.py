"""worker 的桥接与配置用例（**内核结论 ↔ arq 动作**，`12.queue.md` §6 第 1、4 步）。

本文件验的是本步**最容易静默出错**的一处：arq 的重试机制与我们自己的「收场」之间
那条缝。三个可观察事实：

1. 内核说「还要重试」→ 桥必须 raise `arq.worker.Retry`（延迟由内核算）；
2. 内核说「进死信了」→ 桥必须**再抛**（让 arq 的重试计数与失败指标也对上，
   否则监控里看到的是「任务成功」）；
3. `max_tries` 必须**大于**最大重试额度（否则「额度用尽」那一次不进函数，
   死信队列永远是空的 —— 本步最贵的坑）。
"""

from __future__ import annotations

import pytest
from arq.worker import Retry

from app.queue import worker as worker_module
from app.queue.core import DeadLetterRecord, PermanentTaskError
from app.queue.jobs import (
    DEFAULT_POLICIES,
    FUNC_AGENT,
    FUNC_KB_REBUILD,
    FUNC_REPORT,
    FUNCTIONS,
    JOB_TIMEOUT_SECONDS,
    RetryPolicy,
)
from app.queue.worker import (
    MAX_ATTEMPTS,
    WorkerSettings,
    bridge_job,
    dead_letter_watch,
    registered_functions,
    runner_for,
    selfcheck,
)
from app.tools.errors import PermanentToolError, RetryableToolError
from tests.queue.fake_queue import MemoryDeadLetterStore

#: 无抖动的策略（断言「等了几秒」不能容忍随机）
POLICY = RetryPolicy.jitless(attempts=2, base_delay=3.0, max_delay=10.0)


def ctx_of(*, job_id: str = "q_test:1", attempt: int = 1, function: str = FUNC_REPORT, **extra: object) -> dict:
    """造一个 arq 形状的 ctx（实测 0.28：job_id / job_try / enqueue_time / score）。"""
    return {
        "job_id": job_id,
        "job_try": attempt,
        "function": function,
        "enqueue_time": None,
        "policy": POLICY,
        **extra,
    }


def payload_of(**inner: object) -> dict:
    return {"function": FUNC_REPORT, "kind": "report", "user_id": 2, "payload": dict(inner)}


# ── 桥：内核结论 → arq 动作 ──────────────────────────────────────────────────


async def test_bridge_success_returns_result_payload():
    """成功时返回可 JSON 化的结果（它在 Redis 里躺 24 小时）。"""

    async def run(_payload):
        return {"summary": "本月 2 笔订单"}

    ctx = ctx_of()
    result = await bridge_job(ctx, payload_of(title="本月"), runner=run, dead_letters=MemoryDeadLetterStore())
    assert result["summary"] == "本月 2 笔订单"
    assert result["job_id"] == "q_test:1"
    assert result["status"] == "succeeded"
    assert result["attempts"] == 1
    assert ctx["job_outcome"].status == "succeeded"


async def test_bridge_raises_arq_retry_with_the_kernel_delay():
    """内核说「重试」→ 桥 raise `Retry(defer=<内核算的秒数>)`。

    ⚠️ 断言的是**延迟的数值**（毫秒）：如果桥自己拍一个延迟（例如写死 1 秒），
    这里会红 —— 而那种错误在生产里的表现是「退避策略配置了但不生效」。
    """

    async def run(_payload):
        raise RetryableToolError("下游 503")

    with pytest.raises(Retry) as caught:
        await bridge_job(
            ctx_of(attempt=1), payload_of(title="x"), runner=run, dead_letters=MemoryDeadLetterStore()
        )
    assert caught.value.defer_score == int(POLICY.delay_for(1) * 1000)


async def test_bridge_reraises_when_the_job_was_dead_lettered():
    """进死信之后**再抛**：让 arq 也把这次记成失败（否则监控里显示「成功」）。"""

    async def run(_payload):
        raise PermanentToolError("表名不存在")

    store = MemoryDeadLetterStore()
    with pytest.raises(PermanentToolError):
        await bridge_job(ctx_of(attempt=1), payload_of(), runner=run, dead_letters=store)
    assert "q_test:1" in store.records
    entry: DeadLetterRecord = store.records["q_test:1"]
    assert entry.retryable is False
    assert entry.payload == {}  # 原样留痕（重放要能喂回）


async def test_bridge_exhausted_retryable_goes_to_dead_letter():
    """额度用尽（可恢复错误）→ 进死信 + 抛出（而不是重投）。"""

    async def run(_payload):
        raise RetryableToolError("仍然 503")

    store = MemoryDeadLetterStore()
    with pytest.raises(RetryableToolError):
        await bridge_job(ctx_of(attempt=2), payload_of(), runner=run, dead_letters=store)  # POLICY 只有 2 次
    assert store.records["q_test:1"].retryable is True
    assert store.records["q_test:1"].attempts == 2


async def test_bridge_unknown_exception_is_not_retried():
    """**没见过的异常不重试**（`KeyError` 这类是代码 bug，重投 100 次结果一样）。"""

    async def run(_payload):
        raise KeyError("payload 少了一个键")

    store = MemoryDeadLetterStore()
    with pytest.raises(KeyError):
        await bridge_job(ctx_of(attempt=1), payload_of(), runner=run, dead_letters=store)
    assert store.records["q_test:1"].retryable is False


async def test_bridge_uses_the_registry_when_no_runner_is_injected():
    """不注入 runner 时走**生产注册表**（`_RUNNERS`）—— 注入点只在测试里用。"""
    ctx = ctx_of(function=FUNC_REPORT)
    # 生产的 `run_report` 缺 user_id → PermanentTaskError（不可恢复）
    with pytest.raises(PermanentTaskError):
        await bridge_job(ctx, {"function": FUNC_REPORT, "kind": "report", "payload": {}}, dead_letters=MemoryDeadLetterStore())


async def test_bridge_replays_when_stored_payload_says_so():
    """重放：payload 里带 `dlq_key`，成功之后那条死信被关掉（`after_job_end_hook` 的活）。

    本用例只验「桥把 payload 原样交给任务」（任务能看到 `dlq_key`）；
    关闭死信那一步由 `test_queue_semantics.py::test_replay_from_dead_letter_succeeds...`
    与 `tests/queue/test_queue_worker.py::test_after_job_end_closes_the_entry` 覆盖。
    """
    seen: dict = {}

    async def run(payload):
        seen.update(payload)
        return {"ok": True}

    await bridge_job(
        ctx_of(),
        {"function": FUNC_REPORT, "kind": "report", "payload": {"dlq_key": "q_old", "title": "x"}},
        runner=run,
        dead_letters=MemoryDeadLetterStore(),
    )
    assert seen["dlq_key"] == "q_old"
    assert seen["title"] == "x"


async def test_after_job_end_closes_the_replayed_entry():
    """重放成功 → `after_job_end_hook` 把原死信标成 `replayed_ok`（列表才会收敛）。"""
    from app.queue.core import JobOutcome
    from app.queue.worker import after_job_end_hook

    store = MemoryDeadLetterStore()
    store.record(
        DeadLetterRecord(
            job_id="q_old", function=FUNC_REPORT, kind="report", payload={"dlq_key": "q_old"}, attempts=1
        )
    )
    await after_job_end_hook(
        {
            "job_outcome": JobOutcome(job_id="q_old", status="succeeded", attempts=1),
            "job_payload": {"dlq_key": "q_old"},
            "dead_letters": store,
        }
    )
    entry = store.fetch("q_old")
    assert entry is not None
    assert entry.resolution == "replayed_ok"
    assert entry.replayed_job_id == "q_old"
    assert store.count_pending() == 0


async def test_after_job_end_ignores_failed_jobs():
    """失败的任务不该关掉任何死信（否则「失败」与「已处理」就分不清了）。"""
    from app.queue.core import JobOutcome
    from app.queue.worker import after_job_end_hook

    store = MemoryDeadLetterStore()
    store.record(DeadLetterRecord(job_id="q_old", function=FUNC_REPORT, kind="report"))
    await after_job_end_hook(
        {
            "job_outcome": JobOutcome(job_id="q_old", status="dead_letter", attempts=1),
            "job_payload": {"dlq_key": "q_old"},
            "dead_letters": store,
        }
    )
    assert store.count_pending() == 1


# ── WorkerSettings 与自检 ────────────────────────────────────────────────────


def test_registered_functions_cover_every_task_with_real_names():
    """注册给 arq 的函数名就是**持久化契约里的名字**（`shopsage.run_report` 等）。"""
    names = sorted(item.name for item in registered_functions())
    assert names == sorted(FUNCTIONS)
    for item in registered_functions():
        assert item.timeout_s == JOB_TIMEOUT_SECONDS
        assert item.keep_result_s is not None and item.keep_result_s > 0


def test_worker_max_tries_is_one_more_than_the_largest_budget():
    """**本步最要紧的一个数字**：arq 的 max_tries 必须比最大重试额度多一档。

    理由（读 arq 0.28 的 `Worker.run_job`）：`if job_try > max_tries` 时 arq
    **根本不会调用函数** —— 如果让它来判定「额度用尽」，死信就永远不会被写。
    """
    largest = max(policy.attempts for policy in DEFAULT_POLICIES.values())
    assert WorkerSettings.max_tries == largest + 1
    assert MAX_ATTEMPTS == largest + 1


def test_selfcheck_reports_the_max_tries_trap(monkeypatch):
    """把 `MAX_ATTEMPTS` 改小 → 自检必须报出来（这个数字改错不会报错，只会静默失效）。"""
    monkeypatch.setattr(worker_module, "MAX_ATTEMPTS", 2)
    problems = worker_module.selfcheck({"dead_letters": MemoryDeadLetterStore()})
    assert any("max_tries" in item for item in problems)


def test_selfcheck_is_clean_with_a_working_store():
    """一切就绪时自检**没有问题**（这一条同时是「注册表与任务名清单一致」的断言）。"""
    assert selfcheck({"dead_letters": MemoryDeadLetterStore()}) == []


def test_selfcheck_detects_a_missing_implementation(monkeypatch):
    """任务名清单里有、注册表里没有 → 报出来（入队成功但执行时 KeyError 的那类错）。"""
    monkeypatch.delitem(worker_module._RUNNERS, FUNC_AGENT)
    problems = worker_module.selfcheck({"dead_letters": MemoryDeadLetterStore()})
    assert any(FUNC_AGENT in item and "没有实现" in item for item in problems)


def test_selfcheck_detects_a_store_that_is_not_reachable():
    """死信表连不上 → 报出来（否则「零任务丢失」没有落点，而失败只留在日志里）。"""

    class Broken(MemoryDeadLetterStore):
        def available(self) -> bool:
            return False

    problems = selfcheck({"dead_letters": Broken()})
    assert any("死信表不可达" in item for item in problems)


def test_runner_for_rejects_unknown_functions():
    """没实现的任务名要**明确报错**（而不是静默返回 None，让它变成一个 500）。"""
    with pytest.raises(KeyError):
        runner_for("shopsage.not_implemented")


async def test_dead_letter_watch_writes_gauges_and_returns_a_line(dead_letters, monkeypatch):
    """死信巡检：写两个 gauge、返回一句话（cron 每分钟调它）。"""
    from app.observability.metrics import Metrics, set_default_metrics

    metrics = Metrics()
    set_default_metrics(metrics)
    try:
        dead_letters.record(
            DeadLetterRecord(job_id="q_w", function=FUNC_REPORT, kind="report", attempts=3, retryable=True)
        )
        line = await dead_letter_watch({"dead_letters": dead_letters})
    finally:
        set_default_metrics(None)

    assert "待处理死信 1 条" in line
    snapshot = metrics.snapshot()["gauges"]
    assert snapshot["shopsage_queue_dead_letter_pending"] == 1.0
    assert "shopsage_queue_dead_letter_oldest_seconds" in snapshot


async def test_dead_letter_watch_survives_without_a_store():
    """没有死信表时巡检**不抛**（cron 里的异常会污染 worker 日志）。"""
    assert "跳过巡检" in await dead_letter_watch({})


def test_worker_settings_exposes_the_arq_contract():
    """arq 的 CLI 靠这些属性名工作（改名会让 `uv run arq ...` 直接起不来）。"""
    for attribute in ("redis_settings", "queue_name", "functions", "max_tries", "job_timeout", "on_startup"):
        assert hasattr(WorkerSettings, attribute)
    assert WorkerSettings.queue_name.startswith("arq:")
    assert WorkerSettings.cron_jobs, "死信巡检 cron 不能少（它才是「有没有人在看」的信号）"


def test_every_runner_maps_to_a_registered_function():
    """注册表的值必须是可调用的（而不是占位 None —— 那种错只在执行时才炸）。"""
    for name, runner in worker_module._RUNNERS.items():
        assert callable(runner), name
