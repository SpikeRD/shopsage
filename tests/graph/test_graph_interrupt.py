"""human-in-the-loop 的用例：**闸门的策略、位置与「框架级中断确实可用」**（`8.graph.md` §5 第四条）。

分两半：

* **策略与位置**（本步的交付）：`InterruptPolicy` 决定要不要停、闸门挂在 router 之后；
  停下来的语义是「记录 refusal + 不执行处理节点」（fail-closed，而不是「没人批就放行」）。
* **框架级中断**（接缝的可用性证明）：LangGraph 的 `interrupt()` 能在**带 checkpointer** 的
  图上真正挂起，并用 `Command(resume=...)` 恢复 —— 第 9 步的 API 落地批准通道时用的是它。
  这一半用一张最小图来验：主图的 ticket 节点属第 9 步，现在没有可中断的写操作。
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.channels import LastValue

from app.agents import data as data_mod
from app.graph import (
    NODE_DATA,
    NODE_GATE,
    NODE_GUARDRAIL_INPUT,
    NODE_ROUTER,
    NODE_SYNTHESIS,
    GraphRuntime,
    InterruptPolicy,
    REJECTED_REASON,
    SENSITIVE_ROUTES,
    compile_graph,
    graph_config,
    run_turn,
)
from app.graph.routing import after_gate, after_router_gate, should_gate
from tests.graph_helpers import AgentFakeLLM, build_test_graph, customer_state, fake_query_result


def _llm(route: str, answer: str = "已为你登记。") -> AgentFakeLLM:
    return AgentFakeLLM(
        router={"route": route, "certainty": 0.9, "reason": "test"},
        data={
            "sql": "SELECT order_id FROM orders WHERE user_id = :uid LIMIT :n",
            "params": {"n": 5},
            "reason": "查订单",
        },
        synthesis={"answer": answer, "cited": []},
        guardrail={"allowed": True, "reason": "ok"},
    )


# ── 策略（纯函数，不需要跑图）───────────────────────────────────────────────


def test_policy_is_off_by_default() -> None:
    """默认**关闭**：中断会改变 `ainvoke` 的返回形态，默认打开会让所有调用方都得处理它。"""
    policy = InterruptPolicy()
    assert policy.enabled is False
    assert policy.requires_approval({"route": "ticket"}) is False
    assert SENSITIVE_ROUTES == ("ticket",)


def test_policy_flags_only_sensitive_routes_when_enabled() -> None:
    policy = InterruptPolicy(enabled=True)
    assert policy.requires_approval({"route": "ticket"}) is True
    assert policy.requires_approval({"route": "data"}) is False
    assert policy.requires_approval({}) is False, "没有 route 时不该停（router 还没跑）"


def test_gate_appears_only_where_the_policy_says_so() -> None:
    """闸门的位置：`router` 之后、处理节点之前（在花任何代价之前停住）。"""
    runtime = GraphRuntime(interrupts=InterruptPolicy(enabled=True))
    assert after_router_gate(runtime)({"route": "ticket"}) == NODE_GATE
    assert after_router_gate(runtime)({"route": "data"}) == NODE_DATA
    assert should_gate(runtime, {"route": "ticket"}) is True
    # 未批准 → synthesis（如实转述）；已批准（blocked=False）→ 回到原意图
    assert after_gate({"route": "ticket", "blocked": True}) == NODE_SYNTHESIS
    assert after_gate({"route": "ticket", "blocked": False}) == NODE_SYNTHESIS
    assert after_gate({"route": "data", "blocked": False}) == NODE_DATA


# ── 端到端：闸门开着的图上跑一条敏感分支 ──────────────────────────────────────


async def test_sensitive_route_is_gated_and_the_write_path_is_never_touched() -> None:
    """开闸门后：敏感分支**不进处理节点**，落一条 refusal 由 synthesis 如实转述。

    这是「fail-closed」的可观察形式：没有批准通道时**不放行**。
    对客路径上一句「这件事没办成」远好于「悄悄办成了但没人批」。
    """
    fake = _llm("ticket", answer="这项操作需要人工确认，暂时没有为你执行。")
    graph = compile_graph(
        InMemorySaver(),
        GraphRuntime(llm=fake, interrupts=InterruptPolicy(enabled=True)),
    )
    turn = await run_turn(graph, customer_state("帮我投诉物流"), thread_id="t-ticket")

    assert turn.visited == [NODE_GUARDRAIL_INPUT, NODE_ROUTER, NODE_GATE, NODE_SYNTHESIS, "guardrail_output"]
    assert any(REJECTED_REASON in reason for reason in turn.state["refusals"])
    assert "data" not in turn.visited, "闸门之后不该再查库（敏感操作还没被批准）"
    assert turn.answer.startswith("这项操作")


async def test_non_sensitive_route_bypasses_the_gate_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """闸门开着时，**普通分支连闸门节点都不进**（闸门不是「一律先停一下」）。

    这一条刻意断言「gate 不在 visited 里」：闸门是**条件节点**，
    普通路径上一次多余的节点调度就是一次多余的 checkpoint ——
    而 checkpoint 是每轮最贵的写操作之一（`durability="sync"` 下每步一次落库）。
    """
    monkeypatch.setattr(
        data_mod,
        "execute_readonly_query",
        lambda sql, params=None, **kw: ([{"order_id": 1}], {"row_count": 1}),
    )
    fake = _llm("data", answer="你有一笔订单。")
    graph = compile_graph(
        InMemorySaver(),
        GraphRuntime(llm=fake, interrupts=InterruptPolicy(enabled=True)),
    )
    turn = await run_turn(graph, customer_state("我的订单到哪了"), thread_id="t-data-gate")

    assert NODE_GATE not in turn.visited, f"普通分支不该经过闸门：{turn.visited}"
    assert turn.visited == [NODE_GUARDRAIL_INPUT, NODE_ROUTER, NODE_DATA, NODE_SYNTHESIS, "guardrail_output"]
    assert turn.state["route"] == "data"
    assert not turn.state.get("refusals")
    assert turn.blocked is False


# ── 框架级中断：`interrupt()` + `Command(resume=...)` 在带 saver 的图上确实可用 ──


class ApprovalState(TypedDict, total=False):
    """最小审批状态（与主图状态无关；只用来验证框架机制）。"""

    request: Annotated[str, LastValue]
    approved: Annotated[bool, LastValue]
    executed: Annotated[bool, LastValue]


def test_langgraph_interrupt_and_command_resume_work_with_a_checkpointer() -> None:
    """`interrupt()` 挂起 + `Command(resume=True/False)` 恢复：**本仓依赖的框架能力可用**。

    为什么值得一条用例（而不是「文档说有」）：第 9 步要把批准通道接到闸门上，
    届时若这个能力在本仓的依赖版本上不成立（版本升级、saver 不兼容），
    故障会出现在「线上第一次有人点批准」的时刻。这里把它钉在单测里，
    顺带把「挂起时发生了什么」记成可执行的文档：

    * 挂起时 `ainvoke` 的返回值里有 `__interrupt__`，且 checkpoint 的 `next` 停在那一刻；
    * 恢复时**节点函数从头重跑** —— 所以 `interrupt()` 之前的副作用必须幂等
      （这正是主图把「写 refusal」放在挂起之前的理由，见 `nodes.make_gate`）。
    """
    import asyncio

    runs: list[int] = []

    async def approval_node(state: ApprovalState, config: RunnableConfig) -> dict[str, Any]:
        del config
        runs.append(len(runs) + 1)
        decision = interrupt({"question": "是否批准建工单？"})
        return {"approved": bool(decision), "executed": bool(decision)}

    builder = StateGraph(ApprovalState)
    builder.add_node("approval", approval_node)
    builder.add_edge(START, "approval")
    builder.add_edge("approval", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    cfg = graph_config("t-hil")

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], tuple[str, ...]]:
        paused = await graph.ainvoke({"request": "建工单"}, cfg)
        next_at_pause = tuple((await graph.aget_state(cfg)).next or ())
        resumed = await graph.ainvoke(Command(resume=True), cfg)
        return paused, resumed, next_at_pause

    paused, resumed, next_at_pause = asyncio.run(scenario())

    assert "__interrupt__" in paused, f"应当在 interrupt() 处挂起，实际返回 {paused}"
    assert next_at_pause == ("approval",), "挂起时断点应当停在审批节点上"
    assert runs == [1, 2], "恢复时节点函数**从头重跑**（所以它前面的副作用必须幂等）"
    assert resumed.get("approved") is True and resumed.get("executed") is True


def test_rejected_approval_stops_before_any_side_effect() -> None:
    """拒绝批准时：没有副作用发生（`executed` 保持 False）。"""
    import asyncio

    writes: list[str] = []

    async def approval_node(state: ApprovalState, config: RunnableConfig) -> dict[str, Any]:
        del config
        decision = interrupt({"question": "是否批准？"})
        if not decision:
            return {"approved": False, "executed": False}
        writes.append("建工单")
        return {"approved": True, "executed": True}

    builder = StateGraph(ApprovalState)
    builder.add_node("approval", approval_node)
    builder.add_edge(START, "approval")
    builder.add_edge("approval", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    cfg = graph_config("t-hil-reject")

    async def scenario() -> dict[str, Any]:
        await graph.ainvoke({"request": "建工单"}, cfg)
        return await graph.ainvoke(Command(resume=False), cfg)

    result = asyncio.run(scenario())

    assert result.get("approved") is False
    assert result.get("executed") is False
    assert writes == [], "拒绝批准后不该发生任何写操作"
