"""human-in-the-loop 的用例：**闸门的策略、位置与「框架级中断确实可用」**（`8.graph.md` §5 第四条）。

分两半：

* **策略与位置、以及中断语义**（本步的交付 + 第 9 步的收紧）：`InterruptPolicy` 决定要不要停、
  闸门挂在 router 之后；停下来的语义是 `interrupt()` **真挂起**（checkpoint 停在 `gate`），
  由批准通道用 `Command(resume=...)` 决定放行还是拒绝（fail-closed）。
* **框架级中断**（接缝的可用性证明）：LangGraph 的 `interrupt()` 能在**带 checkpointer** 的
  图上真正挂起，并用 `Command(resume=...)` 恢复 —— 主图的闸门用的就是同一套能力，
  这里再用一张最小图独立验一次（版本升级时它是第一道警报）。

> ⚠️ 第 9 步对闸门的改动：`make_gate` 从「记录 refusal + 拒绝」改成**真 `interrupt()`**，
> 因为批准通道（`POST /api/chat/approve`）已经落地。挂起与拒绝是**两种状态**：
> 前者还没人做决定（API 返回 202），后者已有结论（200 + `blocked`）。
> 因此「谁进了 `visited`」的断言也随之变化：`interrupt()` 的那一步**不算跑完一个节点**
> （节点没有返回增量），所以 `visited` 里没有 `gate`。
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
    NODE_GUARDRAIL_OUTPUT,
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


async def test_sensitive_route_is_gated_until_someone_approves() -> None:
    """开闸门后：敏感分支**停在闸门上等人工点头**（不落 refusal、不进处理节点）。

    这是「fail-closed」的可观察形式：**没有批准就什么都不做**。
    对客路径上一句「这件事没办成」远好于「悄悄办成了但没人批」。

    ⚠️ 与第 8 步交付时的差别（第 9 步改的）：那时闸门是「记录 + 拒绝」，
    因为还没有批准通道；现在 `POST /api/chat/approve` 落地了，闸门改成
    **真的 `interrupt()` 挂起** —— 挂起时 checkpoint 停在 `gate`，
    `Turn.interrupt` 带出「问审批人的那句话」，用户得到的是一句
    「需要人工确认」而不是「已按未执行处理」（后者是**没人批**的结论，
    而现在还没人做出决定）。
    """
    fake = _llm("ticket", answer="这项操作已经为你执行。")
    graph = compile_graph(
        InMemorySaver(),
        GraphRuntime(llm=fake, interrupts=InterruptPolicy(enabled=True)),
    )
    turn = await run_turn(graph, customer_state("帮我投诉物流"), thread_id="t-ticket")

    assert turn.visited == [NODE_GUARDRAIL_INPUT, NODE_ROUTER], (
        f"实际路径：{turn.visited}（`interrupt()` 的那一步不算跑完一个节点，"
        "所以 gate 不在 visited 里：它没有返回增量，而是把控制流交给了批准通道）"
    )
    assert turn.pending_approval is True
    assert turn.interrupt is not None
    assert turn.interrupt["question"].strip(), "挂起时必须给审批人一句能读懂的问题"
    assert turn.interrupt["interrupt_id"], "中断 id 要能被回传（批准通道用它对齐）"
    assert NODE_SYNTHESIS not in turn.visited, "挂起时不该合成回答（这一轮还没结论）"
    assert "data" not in turn.visited, "闸门之后不该再查库（敏感操作还没被批准）"

    snapshot = await graph.aget_state(graph_config("t-ticket"))
    assert snapshot.next == (NODE_GATE,), "checkpoint 必须停在闸门上（这是可恢复的前提）"


@pytest.mark.parametrize(
    ("approved", "expect_blocked", "expect_refusal"),
    [(True, False, False), (False, True, True)],
)
async def test_resuming_the_gate_decides_the_outcome(
    approved: bool, expect_blocked: bool, expect_refusal: bool
) -> None:
    """批准 / 不批准 → 两条**不同**的收尾（`Command(resume=...)` 的语义在图上生效）。

    | 决策 | `blocked` | `refusals` | 用户的回答 |
    |---|---|---|---|
    | 批准 | False | 无 | 正常回答（图继续跑到 synthesis） |
    | 不批准 | **True** | **有** `REJECTED_REASON` | 由 synthesis 如实转述「没执行」 |

    ⚠️ 「不批准」这条最容易出错：闸门标死之后图**还会走完 synthesis 与输出护栏**，
    而 `guardrail_output` 在通过时会写 `blocked=False` —— 那会把「没执行」伪装成
    「答完了」（实测踩到，见 runbook §10.1 坑 9）。所以这里对 `blocked` 的断言
    是**必须**的，不能只断言 `refusals`。
    """
    from langgraph.types import Command

    fake = _llm("ticket", answer="这项操作已经为你执行。")
    graph = compile_graph(
        InMemorySaver(),
        GraphRuntime(llm=fake, interrupts=InterruptPolicy(enabled=True)),
    )
    thread = f"t-gate-{'yes' if approved else 'no'}"
    await run_turn(graph, customer_state("帮我投诉物流"), thread_id=thread)

    turn = await run_turn(
        graph,
        Command(resume={"approved": approved, "reason": "", "approver_user_id": 9}),
        thread_id=thread,
    )

    assert turn.visited == [NODE_GATE, NODE_SYNTHESIS, NODE_GUARDRAIL_OUTPUT], f"实际路径：{turn.visited}"
    assert turn.blocked is expect_blocked
    assert bool(turn.state.get("refusals")) is expect_refusal
    if not approved:
        assert any(REJECTED_REASON in reason for reason in turn.state["refusals"])
    assert turn.answer.strip(), "无论批准与否，用户都要拿到一句话"


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
