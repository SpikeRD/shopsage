"""图结构（拓扑）的用例：**不跑节点、不连库**，只断言「边与节点长什么样」。

为什么值得单独一组：`8.graph.md` §8 第一条验收是「完整图 `router → data/rag/analysis →
synthesis` + guardrail 横切可跑通」。其中「横切」是**结构性质**，不是行为性质 ——
它可以被断言，而且必须在**不花任何 token、不连任何库**的前提下被断言（否则每次跑都要
真模型）。第 7 步已经把节点本身测透了，这一组只回答一个问题：
**这些节点被接成了文档里画的那张图吗？**

两条最容易出错、且错了不报错的地方，正是这组用例的主要目标：

1. **guardrail 的位置**：写在 router 之后（输入侧）与 synthesis 之后（输出侧）是设计契约；
   写反了图照样跑，只是「审的是上一轮的答案」。
2. **rag / analysis 的必经之路**：`route=data` 时**不该**经过 rag（§7 第一条用例）；
   这靠条件边的映射而不是「节点内部的 if」保证。
"""

from __future__ import annotations

import pytest

from app.graph import (
    NODE_ANALYSIS,
    NODE_DATA,
    NODE_GATE,
    NODE_GUARDRAIL_INPUT,
    NODE_GUARDRAIL_OUTPUT,
    NODE_RAG,
    NODE_ROUTER,
    NODE_SYNTHESIS,
    ROUTE_NODES,
    GraphRuntime,
    InterruptPolicy,
    build_graph,
    compile_graph,
)
from tests.graph_helpers import memory_saver

START = "__start__"
END = "__end__"


def edge_set(graph) -> set[tuple[str, str]]:  # noqa: ANN001
    """把图的边抽成 `{(源, 目标)}` 集合（条件边与静态边都在里面）。"""
    return {(edge.source, edge.target) for edge in graph.get_graph().edges}


def test_graph_nodes_match_the_documented_topology() -> None:
    """节点清单 == 六个 Agent + 两个护栏挂点时的「七个图节点」。"""
    nodes = set(compile_graph(runtime=GraphRuntime()).get_graph().nodes)
    expected = {
        NODE_GUARDRAIL_INPUT,
        NODE_ROUTER,
        NODE_DATA,
        NODE_RAG,
        NODE_ANALYSIS,
        NODE_SYNTHESIS,
        NODE_GUARDRAIL_OUTPUT,
    }
    assert expected <= nodes, f"图里缺节点：{sorted(expected - nodes)}"
    # 闸门默认**不注册**（InterruptPolicy.enabled=False）：默认图上不该有恒真空节点
    assert NODE_GATE not in nodes, "闸门默认不该出现在图上（会让「图==架构图」需要额外解释）"


def test_entry_and_exit_edges() -> None:
    """入口必须先进**输入侧**护栏（而不是直接进 router）。"""
    edges = edge_set(compile_graph(runtime=GraphRuntime()))
    assert (START, NODE_GUARDRAIL_INPUT) in edges, "入口没有先过输入护栏（D2 的第一道闸）"
    assert (START, NODE_ROUTER) not in edges, "router 不该能从 START 直达（会绕过输入护栏）"
    assert (NODE_GUARDRAIL_OUTPUT, END) in edges, "输出护栏之后必须能结束"


def test_guardrail_is_wired_on_both_sides() -> None:
    """「横切」= 输入侧在 router 之前、输出侧在 synthesis 之后（§4 第 1 条）。"""
    edges = edge_set(compile_graph(runtime=GraphRuntime()))
    assert (NODE_GUARDRAIL_INPUT, NODE_ROUTER) in edges, "输入护栏没有通向 router"
    assert (NODE_SYNTHESIS, NODE_GUARDRAIL_OUTPUT) in edges, "输出护栏没挂在 synthesis 之后"
    assert (NODE_GUARDRAIL_INPUT, END) in edges, "输入护栏必须能短路到 END（D2）"


def test_router_reaches_every_producer_and_they_converge_on_synthesis() -> None:
    """三条业务分支都从 router 可达，且**都汇到 synthesis**（汇合点唯一）。

    `analysis` 是**经** `data` 到达的（router → data → analysis）：
    分析节点的输入是 `rows`，而生成 rows 的只有 data 节点。
    所以这里断言的是「可达」，不是「直连」—— 图的可达性与拓扑形状是两件事。
    """
    edges = edge_set(compile_graph(runtime=GraphRuntime()))
    for producer in (NODE_DATA, NODE_RAG):
        assert (NODE_ROUTER, producer) in edges, f"router 到不了 {producer}"
        assert (producer, NODE_SYNTHESIS) in edges, f"{producer} 没有汇到 synthesis"
    assert (NODE_ROUTER, NODE_DATA) in edges and (NODE_DATA, NODE_ANALYSIS) in edges, (
        "对内分析链路必须是 router → data → analysis（analysis 需要 rows）"
    )
    assert (NODE_ANALYSIS, NODE_SYNTHESIS) in edges


def test_data_path_does_not_pass_through_rag() -> None:
    """`route=data` 时不该经过 rag —— 结构上就没有 data → rag 这条边。

    这是 `8.graph.md` §7 第一条用例（`test_router_to_data_path`）的**结构版**：
    行为版（跑到哪几个节点）在 `test_graph_paths.py`，两条一起才完整 ——
    只测行为的话，「谁加了一条 data→rag 的边」不会红。
    """
    edges = edge_set(compile_graph(runtime=GraphRuntime()))
    assert (NODE_DATA, NODE_RAG) not in edges
    assert (NODE_RAG, NODE_SYNTHESIS) in edges


def test_route_table_covers_every_documented_route() -> None:
    """`state.ROUTES` 的每个取值都必须在路由表里有目的地（新增 route 忘改表会红）。"""
    from app.agents.state import ROUTES

    missing = [route for route in ROUTES if route not in ROUTE_NODES]
    assert missing == [], f"这些 route 没有目的地：{missing}（改 router 的取值域时要同步改 ROUTE_NODES）"


def test_conditional_edge_map_is_keyed_by_node_names() -> None:
    """★条件边的映射表**必须用节点名当键**（变更 003 实测踩到的静默 bug）。

    ## 这条防的是什么（一个「编译期不报、边表也正确」的坑）

    `route_by_intent` 返回的是**节点名**（`ROUTE_NODES.get(route)` 的**值**），
    而 LangGraph 用它去查条件边的 `ends` 映射。变更 003 之前那张表写的是
    `{**ROUTE_NODES, NODE_SYNTHESIS: NODE_SYNTHESIS}` —— 键是 **route 名**、值是节点名。

    对 `data` / `rag` / `synthesis` 而言键与值**恰好相同**，所以一直没暴露；
    而 `route=refund` 的返回值是 `"refund_request"`，它不是那张表的键 →
    `_branch.py::_finish` 里 `self.ends["refund_request"]` 直接抛 `KeyError`。

    ## 为什么它极难被发现（所以必须有一条用例）

    * **编译期不报**：编译只检查「映射的**值**是不是已注册节点」，
      不检查「分支函数会不会返回映射里没有的键」；
    * **`get_graph()` 的边表完全正确**（`router → refund_request` 在），
      所以「图结构断言」全绿；
    * route 也判对了（`route=refund` 能看见）；
    * 症状只在**真跑一轮**时出现，最终表现为用户拿到一句**空回答**。

    这条用例直接把「分支函数所有可能的返回值」与「映射表的键」对齐 ——
    新增一条 route 时，只要忘改映射就会立刻红。
    """
    from app.graph.builder import _route_map
    from app.graph.routing import route_by_intent
    from app.graph.state import NODE_GATE, NODE_SYNTHESIS

    mapping = _route_map()

    # 1) 分支函数在**任何** route 取值下的返回值，都必须是映射表里的键
    from app.agents.state import ROUTES

    possible_returns = {route_by_intent({"route": route}) for route in ROUTES}
    # 野值 route 会兜到 synthesis（`route_by_intent` 的兜底），也要能查到
    possible_returns.add(route_by_intent({"route": "a_wild_value_nobody_registered"}))
    missing = sorted(node for node in possible_returns if node not in mapping)
    assert missing == [], (
        f"这些分支目的地不是条件边映射表的键：{missing}。"
        "映射表必须用**节点名**当键（分支函数返回的是节点名，不是 route 名）——"
        "写错的话真跑一轮会 KeyError，而编译期与边表都不会报"
    )

    # 2) 映射的值必须是自身（`{node: node}`），也就是「返回值 → 真实节点名」不变形
    for key, node in mapping.items():
        assert key == node, f"映射 {key!r} → {node!r} 不是恒等映射：分支返回值必须就是节点名"

    # 3) 终点（synthesis）必须在里面 —— `ticket` / `final` 与野值兜底都靠它
    assert NODE_SYNTHESIS in mapping

    # 4) 开了闸门时 `after_router_gate` 还会返回 `gate`，调用点负责把它并进映射
    #    （这里断言常量存在，让「并进映射」那一步有据可查）
    assert NODE_GATE != NODE_SYNTHESIS


def test_graph_is_drawable_and_names_itself() -> None:
    """图必须能画出来（`draw_mermaid` 走 `get_graph()`）。

    单独的用例：本步第一版用 `error_handler=` 时这里会抛
    `TypeError: '<' not supported between instances of 'NoneType' and 'str'`，
    而**图跑起来完全正常** —— 也就是说「图结构可检查」这件事本身需要被守住（§10.1 坑 3）。
    """
    graph = compile_graph(runtime=GraphRuntime())
    mermaid = graph.get_graph().draw_mermaid()
    for node in (NODE_GUARDRAIL_INPUT, NODE_ROUTER, NODE_DATA, NODE_RAG, NODE_ANALYSIS, NODE_SYNTHESIS):
        assert node in mermaid
    assert graph.get_name() == "shopsage"


def test_uncompiled_graph_can_be_built_without_checkpointer() -> None:
    """`build_graph()` 返回未编译的图：装配与「谁来持久化」是两件独立的事。"""
    from langgraph.graph import StateGraph

    assert isinstance(build_graph(), StateGraph)


@pytest.mark.parametrize("enabled", [False, True])
def test_gate_is_registered_only_when_the_policy_is_on(enabled: bool) -> None:
    """闸门按策略条件注册：默认图上没有它，开了策略它必须**在**图上。

    为什么接受「图有两种形状」：把闸门做成恒真空节点会让默认路径每次多一次
    节点调度与一条 checkpoint；而「敏感操作要人工点头」是**逐调用方**的策略，
    不是全局事实。代价是「图结构」有两个版本，所以这条用例把两个版本都钉住。
    """
    runtime = GraphRuntime(interrupts=InterruptPolicy(enabled=enabled))
    nodes = set(compile_graph(memory_saver(), runtime).get_graph().nodes)
    assert (NODE_GATE in nodes) is enabled
    edges = edge_set(compile_graph(memory_saver(), runtime))
    if enabled:
        assert (NODE_ROUTER, NODE_GATE) in edges
        assert (NODE_GATE, NODE_SYNTHESIS) in edges
        assert (NODE_GATE, NODE_DATA) in edges
