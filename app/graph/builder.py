"""编译 LangGraph 图：`8.graph.md` §4 的拓扑在这里落成代码，并把上面几个模块接起来。

拓扑（`guardrail 横切` + `router 四选一` + `synthesis 收口`）：

```
START → guardrail_input ─┬─(blocked)──────────────────────────────────────→ END
                         └─→ router ─┬─ data ─────┐
                                     ├─ rag ──────┤
                                     ├─ refund_request ─┤   ← 变更 003：唯一写节点
                                     ├─ analysis ─┘
                                     └─ ticket / final ───────────────────────────┘
                                                    → synthesis → guardrail_output ─┬─→ END
                                                                                    └─(blocked)→ END
```

⚠️ 变更 003 的两处结构变化（都会在上面这张图里体现）：
1. **新增 `refund_request` 分支**（`route=refund`）：对客「我要退款」第一次有了真写路径。
   它汇到 synthesis —— 由合成节点把「已受理，单号 N」或「这单不能退，因为…」说给客户。
2. **`gate` 节点在默认图上不再出现**（`GATED_ROLES` 为空集）：变更 003 之前对客路径会
   在 `router` 之后插一道闸门，而批准人就是**发起人自己**。现在审批搬到退款单据上
   （`/api/refunds/decision`，要 `approve:refund`）。原语保留，见决策 D2。

与 `8.graph.md` §4 第 1 条逐一对应：

| 文档要求 | 落地 |
|---|---|
| 图结构 = 架构第 4 节的图 | 上面的拓扑；`tests/test_graph_builder.py` 用**边表**断言（不是看图看出来的） |
| checkpointer 外置 | `app/graph/checkpointer.py`；本模块只接受一个 saver 实例（谁来建、何时关是调用方的事） |
| `thread_id` = 会话 id | `checkpointer.thread_config()`；本模块不产生 thread_id |
| 条件边做路由，不硬编码 if/else | `app/graph/routing.py`；本模块只做 `add_conditional_edges(source, fn)` |
| 失败从断点重放 | `durability="sync"`（见下）+ 图级兜底只兜**永久**错误（`nodes.graph_fallback`） |

## 一个容易漏掉的参数：`durability="sync"`

LangGraph 1.x 默认是**异步**持久化（`durability="async"`）：节点结果先返回，
checkpoint 在后台写。它更快，但「重放」的前提是**落盘已经发生**——
默认值下，进程在写完之前挂掉，你会得到一个「节点跑过了、checkpoint 没有」的状态，
而重放恰恰要用它。所以本模块把 `TURN_DURABILITY = "sync"` 钉进**每一次调用**，
并提供 `graph_config()` 把配置装配收成一处（调用方自己拼 config 时最容易漏掉它）。

代价是每步多一次写库往返（实测单轮 4~6 个节点，本地库 < 10ms/次）。
这笔钱买的是 A2 那句「进程重启不丢状态」——换成异步持久化就只剩「大部分时候不丢」。

## 为什么节点用**工厂**逐个构造，而不是直接 `add_node("router", router_node)`

六个节点的签名是 `node(state, *, llm=None)`，而 `add_node` 传的是位置参数。
直接把它们注册进去，LangGraph 会把 `llm` 当成第二个**位置**参数之外的默认值 ——
看上去能跑，但**注入点就消失了**：测试只能靠 monkeypatch 掏内部实现，
第 7 步那条「六个 Agent 可独立调用、独立测试」的验收在此处被悄悄破坏。
工厂让注入点显式地存在于构建期（`GraphRuntime`），而不是藏在运行期的全局状态里。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.graph.checkpointer import thread_config
from app.graph.config import GraphRuntime
from app.graph.nodes import node_specs
from app.graph.routing import (
    after_data,
    after_gate,
    after_guardrail_input,
    after_guardrail_output,
    after_router_gate,
    route_by_intent,
)
from app.graph.state import (
    GraphStateDoc,
    NODE_ANALYSIS,
    NODE_DATA,
    NODE_GATE,
    NODE_GUARDRAIL_INPUT,
    NODE_GUARDRAIL_OUTPUT,
    NODE_RAG,
    NODE_REFUND_REQUEST,
    NODE_ROUTER,
    NODE_SYNTHESIS,
    ROUTE_NODES,
)

logger = logging.getLogger(__name__)

#: 立即持久化（理由见模块 docstring）。调用方用 `graph_config()` 就能拿到它。
TURN_DURABILITY: str = "sync"

#: 图的名字（`ainvoke` 的 trace、`draw_mermaid` 的标题都用它）
GRAPH_NAME: str = "shopsage"


def build_graph(runtime: GraphRuntime | None = None) -> StateGraph:
    """造**未编译**的 `StateGraph`：节点 + 边 + 条件边。

    未编译的图可以被单独检查（`get_graph()` / 边表），所以「图结构对不对」这件事
    不需要连数据库、也不需要真跑一轮就能验（`tests/test_graph_builder.py`）。
    """
    runtime = runtime or GraphRuntime()
    specs = node_specs(runtime)

    builder = StateGraph(GraphStateDoc)
    # ⚠️ 注册顺序 = 上面 `node_specs` 的字典顺序（`gate` 最后）。顺序不影响语义
    # （边才是语义），但 `get_graph().nodes` 的稳定顺序让「图结构断言」好写。
    for name, factory in specs.items():
        if name == NODE_GATE and not runtime.interrupts.enabled:
            continue  # 闸门只在开了策略的图上出现（见下）
        builder.add_node(name, factory(runtime))

    # ── 边 ────────────────────────────────────────────────────────────────────
    builder.add_edge(START, NODE_GUARDRAIL_INPUT)
    builder.add_conditional_edges(
        NODE_GUARDRAIL_INPUT, after_guardrail_input, {NODE_ROUTER: NODE_ROUTER, END: END}
    )
    if runtime.interrupts.enabled:
        builder.add_conditional_edges(
            NODE_ROUTER,
            after_router_gate(runtime),
            {NODE_GATE: NODE_GATE, **_route_map()},
        )
        builder.add_conditional_edges(
            NODE_GATE,
            after_gate,
            _route_map(),
        )
    else:
        builder.add_conditional_edges(NODE_ROUTER, route_by_intent, _route_map())
    for producer in (NODE_RAG, NODE_ANALYSIS, NODE_REFUND_REQUEST):
        builder.add_edge(producer, NODE_SYNTHESIS)
    # `data` 是个**岔路口**：`route=analysis` 时接着做分析，其余直接进 synthesis；
    # 被兜底标死（`blocked`）时直接结束（理由见 `routing.after_data`）。
    builder.add_conditional_edges(
        NODE_DATA,
        after_data,
        {NODE_ANALYSIS: NODE_ANALYSIS, NODE_SYNTHESIS: NODE_SYNTHESIS, END: END},
    )
    builder.add_edge(NODE_SYNTHESIS, NODE_GUARDRAIL_OUTPUT)
    # ⚠️ 条件边的目标映射里**必须**显式写 `END`，不能靠「返回 None 隐式落到 END」：
    # 映射的键会被 `get_graph()`（`draw_mermaid()` / 图结构断言）排序，隐式约定
    # 会让内部边带上 `None` 标签，排序时直接抛
    # `TypeError: '<' not supported between instances of 'NoneType' and 'str'`（§10.1 坑 3）。
    builder.add_conditional_edges(NODE_GUARDRAIL_OUTPUT, after_guardrail_output, {END: END})

    logger.info(
        "图已装配：节点=%s（checkpointer 由调用方在编译期注入）",
        ",".join(name for name in specs if name != NODE_GATE or runtime.interrupts.enabled),
    )
    return builder


def compile_graph(
    checkpointer: BaseCheckpointSaver | None = None,
    runtime: GraphRuntime | None = None,
) -> Any:
    """编译成可执行图：`await graph.ainvoke(state, graph_config("会话ID"))`。

    `checkpointer=None` 时 LangGraph 用**无持久化**模式：能跑通、但 `aget_state` 读不到、
    也谈不上断点重放。所以生产与「重放演示」都必须显式传 saver
    （`app/graph/checkpointer.open_checkpointer()` 负责建与关）。
    """
    return build_graph(runtime).compile(checkpointer=checkpointer, name=GRAPH_NAME)


def graph_config(thread_id: str, **extra: Any) -> dict[str, Any]:
    """装配一次调用的 config：`thread_id` + `durability="sync"`。

    把「容易漏掉的那一项」集中在这里：调用方自己拼 `{"configurable": {...}}` 时，
    `durability` 就变成了一条口头约定，而漏掉它的症状**不会报错** ——
    只是某次崩溃之后状态少了一截。
    """
    config = thread_config(thread_id, **extra)
    config["durability"] = TURN_DURABILITY
    return config


def _route_map() -> dict[str, str]:
    """条件边的**目的地映射**：`{分支函数的返回值: 真实节点名}`。

    ## ⚠️ 这里有一个只有「新增 route」时才会暴露的坑（变更 003 实测踩到）

    `route_by_intent` 返回的是**节点名**（`ROUTE_NODES.get(route)` 的**值**），
    不是 route 名。所以映射表的**键必须是节点名**：

    ```python
    # ❌ 错的（变更 003 之前一直是这样，而它「看起来能用」）
    {**ROUTE_NODES, NODE_SYNTHESIS: NODE_SYNTHESIS}
    # → {"data": "data", "rag": "rag", "analysis": "data", "ticket": "synthesis",
    #    "refund": "refund_request", "final": "synthesis", "synthesis": "synthesis"}
    #    键是 **route 名**，值是节点名。
    ```

    对 `data` / `rag` / `synthesis` 而言键与值**恰好相同**，所以一直没暴露；
    而 `route=refund` 的返回值是 `"refund_request"` —— 它不是这张表的键，
    于是 LangGraph 在 `_branch.py::_finish` 里 `self.ends["refund_request"]` 直接抛
    `KeyError: 'refund_request'`（**运行期**，不是编译期：
    编译只检查「映射的**值**是不是已注册节点」，不检查「分支函数会不会返回映射里没有的键」）。

    症状极具误导性：route 判对了（`route=refund` 能看见）、节点也注册了、
    `graph.get_graph()` 的边表**完全正确**（`router → refund_request` 在），
    但真跑一轮就 `KeyError`，最终表现为用户拿到一句空回答。

    正确写法是「把分支函数**所有可能的返回值**当作键」，即
    `set(ROUTE_NODES.values()) | {NODE_SYNTHESIS}` —— 两者都取**节点名**。

    ## 为什么必须包含 `synthesis`

    `ticket` / `final` 的目的地就是 `synthesis`，而 `route_by_intent` 在
    「route 是野值」时也会兜到它。漏掉它的症状同样是运行期 `KeyError`
    （历史上 `你好` 这种最无害的问题反而崩，见 runbook §10.1 坑 4）。
    """
    targets = set(ROUTE_NODES.values()) | {NODE_SYNTHESIS}
    return {node: node for node in targets}


__all__ = [
    "GRAPH_NAME",
    "TURN_DURABILITY",
    "build_graph",
    "compile_graph",
    "graph_config",
]
