"""`tests/queue/` 的装置：**内存队列 + 内存死信表 + 假任务**。

三条纪律（与 `tests/api/conftest.py` 同口径）：

1. **不连真 Redis**：默认队列就是 `MemoryJobQueue`（`12.queue.md` §7 的五条用例里
   有四条与传输层无关，见 `fake_queue.py` 的 docstring）；
2. **不连真库**：死信表用 `MemoryDeadLetterStore`；真库那两条用例单独打 `@pytest.mark.db`
   （依赖不可用时 `tests/conftest.py` 会自动 skip 并说明原因）；
3. **不花真钱**：`report` 任务的模型注入 `AgentFakeLLM`，`agent` 任务注入假图。

⚠️ 真 Redis 那一组（`test_queue_redis.py`）**不在这里**：它用 `SHOPSAGE_TEST_REDIS=1`
显式开门（理由见那个文件），因为「没起 Redis 就红一片」会让本步的验收基线不可用。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.queue.jobs import FUNC_KB_REBUILD, FUNC_REPORT
from tests.queue.fake_queue import MemoryDeadLetterStore, MemoryJobQueue


@pytest.fixture
def dead_letters() -> MemoryDeadLetterStore:
    return MemoryDeadLetterStore()


@pytest.fixture
def queue(dead_letters: MemoryDeadLetterStore) -> Iterator[MemoryJobQueue]:
    """一个装好的内存队列：`report` 与 `kb_rebuild` 两个任务的 runner 已注册。

    ⚠️ 默认 runner 是「立刻成功、返回一份可断言的结果」（`ok` 用一个计数器证明
    它跑过几次）。用例想制造失败就 `queue.inject_failure(...)`，
    想换实现就 `queue.runners[FUNC_X] = ...` —— 两种都是显式的。
    """
    from tests.graph_helpers import AgentFakeLLM

    instance = MemoryJobQueue(dead_letters=dead_letters)
    # 假模型是给 `agent` 任务的用例准备的（它们会把图注入成假的，
    # 但 report 任务的真实实现 `run_report` 会调 `generate_json`）——
    # 放在这里让「换回真实现」的用例也能直接跑（见 test_queue_dead_letter.py 的
    # `test_run_report_with_fake_model_produces_summary`）。
    instance.fake_llm = AgentFakeLLM(report={"summary": "本月共 2 笔订单，其中 1 笔已发货。"})  # type: ignore[attr-defined]
    instance.runners[FUNC_REPORT] = instance.make_runner(
        FUNC_REPORT,
        lambda payload: {
            "title": payload.get("title"),
            "user_id": payload.get("user_id"),
            "summary": "本月共 2 笔订单，其中 1 笔已发货。",
            "row_count": 2,
        },
    )
    instance.runners[FUNC_KB_REBUILD] = instance.make_runner(
        FUNC_KB_REBUILD,
        lambda payload: {"documents": 7, "chunks": 59, "embedded": 12, "source_dir": payload.get("source_dir", "")},
    )
    yield instance


@pytest.fixture
def report_payload() -> dict[str, Any]:
    """一份标准的 report 任务入参（用例改其中一项就够，不必每次重写整份）。"""
    return {"user_id": 2, "title": "本月订单"}
