"""路径与路由的用例：**真的把图跑起来**，断言「走了哪几个节点、最后是什么状态」。

对应 `8.graph.md` §7 的前两条：

| 文档 §7 的用例 | 本文件 |
|---|---|
| `test_router_to_data_path` | `test_route_data_runs_data_then_synthesis` |
| `test_guardrail_short_circuits` | `test_guardrail_short_circuit_skips_every_downstream_node` |
| （文档没写但必须有） | `final` / `ticket` / `rag` / `analysis` 四条分支，以及「未知 route」的兜底 |

**「走了哪几个节点」怎么断言**：用 `stream_mode="updates"` 读每个超级步的增量 ——
它就是 checkpoint 的写入序列，所以断言它等于断言「checkpoint 里记的路径」，
而不是另造一套探针（另造的探针只会证明探针自己被调用了）。
"""

from __future__ import annotations

import pytest

from app.agents import data as data_mod
from app.graph import GraphRuntime, compile_graph, graph_config
from app.graph.state import (
    NODE_ANALYSIS,
    NODE_DATA,
    NODE_GUARDRAIL_INPUT,
    NODE_GUARDRAIL_OUTPUT,
    NODE_RAG,
    NODE_ROUTER,
    NODE_SYNTHESIS,
)
from tests.graph_helpers import AgentFakeLLM, build_test_graph, customer_state, fake_query_result, memory_saver

SQL = "SELECT order_id, status FROM orders WHERE user_id = :uid LIMIT :n"
ROWS = [{"order_id": 1024, "status": "shipped"}, {"order_id": 1025, "status": "pending"}]


@pytest.fixture
def patched_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """把真库执行器换掉（离线）；**只**换这一个出口，其余全是真实现。"""
    monkeypatch.setattr(data_mod, "execute_readonly_query", fake_query_result(ROWS))


def data_fake(**overrides: object) -> AgentFakeLLM:
    """一条 `data` 路径的完整假模型（router / data / synthesis / guardrail 四份回执）。"""
    replies: dict[str, object] = {
        "router": {"route": "data", "certainty": 0.95, "reason": "问自己的订单"},
        "data": {"sql": SQL, "params": {"uid": 999, "n": 10}, "reason": "查订单"},
        "synthesis": {"answer": "您最近 2 笔订单：1 笔已发货、1 笔待付款。", "cited": []},
        "guardrail": {"allowed": True, "reason": "ok"},
    }
    replies.update(overrides)
    return AgentFakeLLM(**replies)  # type: ignore[arg-type]


async def run_path(graph, state: dict, thread: str) -> tuple[list[str], dict]:  # noqa: ANN001
    """跑一轮，返回 `([节点名...], 最终 state)`。"""
    visited: list[str] = []
    final: dict = {}
    async for chunk in graph.astream(state, graph_config(thread), stream_mode="updates"):
        for node, update in chunk.items():
            visited.append(node)
            final.update(update or {})
    snapshot = await graph.aget_state(graph_config(thread))
    return visited, dict(snapshot.values)


# ── data 路径（§7 第一条用例）────────────────────────────────────────────────


async def test_route_data_runs_data_then_synthesis(patched_query: None) -> None:
    """`route=data` → `data → synthesis`，**不经过 rag**（`8.graph.md` §7 第一条）。"""
    graph = build_test_graph(data_fake(), search=lambda **_: [])
    visited, state = await run_path(graph, customer_state("我的订单到哪了"), "t-data")

    assert visited == [
        NODE_GUARDRAIL_INPUT,
        NODE_ROUTER,
        NODE_DATA,
        NODE_SYNTHESIS,
        NODE_GUARDRAIL_OUTPUT,
    ], f"实际路径：{visited}"
    assert NODE_RAG not in visited and NODE_ANALYSIS not in visited
    assert state["route"] == "data"
    assert len(state["rows"]) == 2
    assert state["sql_attempts"] == 1
    assert state["answer"].startswith("您最近 2 笔订单")


async def test_data_path_rewrites_the_model_supplied_user_id(patched_query: None) -> None:
    """对客路径的绑定值由**程序**覆写：模型给 `uid=999`，实际绑定的是 2。

    图这一层的价值在于它证明了「覆写在**图里**也生效」——
    第 7 步已经有单节点用例，但如果图喂进去的 state 少了 `user_id`（例如第 9 步忘了写），
    覆写就会在真实链路上静默失效。
    """
    seen: dict = {}
    graph_fake = data_fake()
    graph = build_test_graph(graph_fake, search=lambda **_: [])
    import app.agents.data as mod

    original = mod.execute_readonly_query

    def capture(sql, params=None, **kwargs):  # noqa: ANN001, ANN202
        seen.update(params or {})
        return ROWS, {"row_count": len(ROWS)}

    mod.execute_readonly_query = capture
    try:
        await run_path(graph, customer_state("我的订单到哪了", user_id=2), "t-uid")
    finally:
        mod.execute_readonly_query = original

    assert seen["uid"] == 2, f"绑定参数被模型的值污染了：{seen}"


# ── 其余四条分支 ─────────────────────────────────────────────────────────────


async def test_route_rag_runs_the_retrieval_node_only() -> None:
    """`route=rag` → 只跑 `rag`（`search=` 注入一个假后端，第 10 步的真实后端还没落地）。"""
    fake = AgentFakeLLM(
        router={"route": "rag", "certainty": 0.9, "reason": "问政策"},
        synthesis={"answer": "七天无理由退货，[1]", "cited": [1]},
        guardrail={"allowed": True, "reason": "ok"},
    )

    class Doc:
        source = "退货政策.md"
        text = "自签收之日起七天内可无理由退货。"

        def citation(self) -> str:
            return "退货政策.md"

    graph = build_test_graph(fake, search=lambda **_: [Doc()])
    visited, state = await run_path(graph, customer_state("退货政策是什么"), "t-rag")

    assert visited == [NODE_GUARDRAIL_INPUT, NODE_ROUTER, NODE_RAG, NODE_SYNTHESIS, NODE_GUARDRAIL_OUTPUT]
    assert NODE_DATA not in visited, "rag 路径不该查库"
    assert len(state["docs"]) == 1
    assert state["citations"] == ["退货政策.md"], "引用必须由程序从编号映射回出处"


async def test_route_analysis_requires_internal_role_and_data() -> None:
    """`route=analysis` 走 `data → analysis`（analysis 需要 rows，且要内部权限）。

    这里刻意用**内部角色**：对客请求被判成 analysis 时 router 会降级成 data
    （第 7 步的行为），所以「走到 analysis」本身就是一条权限相关的路径。
    """
    fake = AgentFakeLLM(
        router={"route": "analysis", "certainty": 0.9, "reason": "看趋势"},
        data={"sql": SQL, "params": {"n": 10}, "reason": "按月聚合"},
        analysis={"headline": "两个月各 12 单", "bullets": ["1 月 12 单"], "caveats": []},
        synthesis={"answer": "两个月各 12 单。", "cited": []},
        guardrail={"allowed": True, "reason": "ok"},
    )
    graph = build_test_graph(fake, search=lambda **_: [])
    state_in = customer_state("最近两个月的订单趋势", user_id=2, role="internal_employee")
    # ⚠️ 内部路径也要查库：把执行器换掉（rows 是 analysis 的输入）
    original = data_mod.execute_readonly_query
    data_mod.execute_readonly_query = fake_query_result([{"month": "2024-01", "cnt": 12}])
    try:
        visited, state = await run_path(graph, state_in, "t-analysis")
    finally:
        data_mod.execute_readonly_query = original

    assert visited == [
        NODE_GUARDRAIL_INPUT,
        NODE_ROUTER,
        NODE_DATA,
        NODE_ANALYSIS,
        NODE_SYNTHESIS,
        NODE_GUARDRAIL_OUTPUT,
    ], f"实际路径：{visited}"
    assert state["analysis"]["headline"] == "两个月各 12 单"
    assert state["is_customer"] is False and "read:all_orders" in state["permissions"]


@pytest.mark.parametrize("route", ["final", "ticket"])
async def test_direct_routes_go_straight_to_synthesis(route: str) -> None:
    """`final` / `ticket` 不进任何检索节点 —— 但**仍要过输入与输出两侧护栏**。

    这是「条件边而不是硬编码 if/else」的可见收益：新增一条 route 只需改 `ROUTE_NODES`，
    护栏与合成的接线不用动。
    """
    fake = AgentFakeLLM(
        router={"route": route, "certainty": 0.8, "reason": "直达"},
        synthesis={"answer": "在的，有什么可以帮你？", "cited": []},
        guardrail={"allowed": True, "reason": "ok"},
    )
    graph = build_test_graph(fake)
    visited, state = await run_path(graph, customer_state("你好"), f"t-{route}")

    assert visited == [NODE_GUARDRAIL_INPUT, NODE_ROUTER, NODE_SYNTHESIS, NODE_GUARDRAIL_OUTPUT]
    assert state["answer"] == "在的，有什么可以帮你？"
    # `refusals` 在这条路径上**不出现**（而不是空列表）：没有任何节点写过它。
    # 这是 `total=False` 状态契约的正常表现 —— `final` 路径确实什么都没拒绝。
    assert not state.get("refusals")


async def test_router_overwrites_a_bogus_route_from_the_entry_state(patched_query: None) -> None:
    """有人在入口塞了野 route：**router 才是 route 的作者**，它会覆盖掉。

    这条与下一条一起构成「野值兜底」的完整证据：入口的野值不会活过 router；
    万一路由表漏登记了新 route，`route_by_intent` 会兜到 synthesis 并打 WARNING。
    """
    graph = build_test_graph(data_fake())
    state_in = customer_state("我的订单到哪了")
    state_in["route"] = "unknown_branch"  # 模拟上游写错（entry 层不该有人写 route）

    visited, state = await run_path(graph, state_in, "t-bogus-route")

    assert state["route"] == "data", "router 必须覆盖入口里的野值（它是 route 的唯一作者）"
    assert state["answer"], "覆盖之后这一轮照样答得出来"


def test_unknown_route_reaching_the_branch_function_warns(caplog: pytest.LogCaptureFixture) -> None:
    """直接调分支函数：野值 → synthesis + WARNING（不是静默兜底）。

    这条防的是「`state.ROUTES` 新增了取值，却忘了在 `ROUTE_NODES` 里登记」——
    那时图不会报错，只会「什么都没查就回答」，而日志里的这一行是唯一的线索。
    """
    import logging

    from app.graph.routing import route_by_intent

    with caplog.at_level(logging.WARNING):
        target = route_by_intent({"route": "unknown_branch"})

    assert target == NODE_SYNTHESIS
    assert any("unknown_branch" in record.getMessage() for record in caplog.records)
