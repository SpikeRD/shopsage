"""共享状态的本步版本：**在 `GraphState` 之上只加「图怎么合并」这一件事**。

`8.graph.md` §2 要求对外暴露 `from app.graph.state import GraphState`。本模块照做，
但**不抄一份字段清单** —— 字段的真源仍是 `app/agents/state.py`（第 7 步的交付，
那边的 docstring 解释了为什么它落在 agents 而不是 graph）。本模块只做「图级」的三件事：

| 加的东西 | 为什么必须在图这一层 |
|---|---|
| 每个键的 reducer（`messages` 追加，其余 `LastValue`） | 「同一条 `thread_id` 的第二轮怎么合并」只有图知道：节点返回增量，图负责**合并成完整 state** |
| `GraphStateDoc`（`total=False` 的运行时 schema） | `StateGraph` 要的是「键 → 通道」的声明，而第 7 步的 `TypedDict` 是给静态检查看的 |
| 节点名 / 分支名常量 | 路由表与节点名必须只有一处真相，否则条件边的目的地字符串与 `add_node` 的键迟早对不上 |

## ⚠️ 每个键都必须显式声明 reducer（本步踩到的最贵的坑）

第一版只给 `messages` 加 `add_messages`、其余键保持「裸注解」。图**跑起来完全正确**
（checkpoint 里 18 个键一个不少），但 `ainvoke` 的返回值里只剩 `messages`：

```
$ graph.ainvoke(state, config)              # ← 只回 {'messages': [...]}
$ graph.get_output_jsonschema()             # ← 只有 messages 一个属性
$ graph.stream_channels_asis                # ← 22 个键（说明状态本身是全的）
```

根因：LangGraph 把「带注解的键」当作**显式通道**来推 `output_schema` / `output_channels`，
而 `ainvoke` 的返回值取自 `output_channels`。于是「没注解的键」在图里是隐式的，
`ainvoke` 的调用方（第 9 步 API）拿到手就少了一整份状态。
症状极具误导性：**测试全绿、checkpoint 正确、只有返回值不对**（实测见 runbook §10.1 坑 5）。

修法：给每个键声明 reducer。`messages` 用 `add_messages`（追加），
其余用 `LastValue`（**与本步想要的语义完全一致**：第二轮问了别的，`route` 就该是新的那个值）。
两个 reducer 都来自 `langgraph.channels`。

## 为什么 reducer 只给 `messages` 用「追加」

其余字段的语义都是**本轮覆盖**：留着上一轮的 `route=data` 会让条件边按旧意图分支 ——
那是脏读，不是「历史保留」。只有 `messages` 的语义是追加（对话历史本来就该累积）。

⚠️ `add_messages` 的副作用值得单独记住：它把 `dict` 形态的消息**归一成 `BaseMessage` 对象**。
于是第二次 `ainvoke` 之后 `state["messages"]` 里是 `HumanMessage` / `AIMessage`，
而不再是普通 dict —— 六个节点的取值口径（`state.messages_of` / `last_user_message`）
都用 `message.get("role")` / `message.get("content")`，对 `BaseMessage` **不成立**。
本模块因此提供 `to_openai_messages()` 作为出口，`app/graph/nodes.py` 的 `normalize_input`
用它把入口输入归一成普通 dict，两种形态因此不会同时存在于图内。
"""

from __future__ import annotations

from typing import Annotated, Any, Final, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langgraph.channels import LastValue
# ⚠️ `add_messages` **不在** `langchain_core.messages` 里（langchain-core 1.6 已移除），
# 它是 LangGraph 的 reducer：`langgraph.graph.message.add_messages`。
# 从 `langchain_core` import 会得到 `ImportError: cannot import name 'add_messages'`，
# 而报错里的建议（`trim_messages`）会把你带向完全无关的方向（§10.1 坑 2）。
from langgraph.graph.message import add_messages


class GraphStateDoc(TypedDict, total=False):
    """`StateGraph` 的**运行时 schema**：字段与 `app/agents/state.py` 逐字一致，外加 reducer。

    为什么每个键都要写 `Annotated[..., LastValue]`（而不是留裸注解）：见模块 docstring
    的「最贵的坑」—— 裸注解的键不进 `output_channels`，`ainvoke` 就不会返回它们。

    为什么 `total=False`：`StateGraph(GraphStateDoc)` 把每个声明键当成可更新通道，
    而 `ainvoke(initial_state)` 只需要**入口那几个键**（`messages` / `user_id` / `role`）。
    声明成必填会让类型检查器把「只传三个键」判成非法 ——
    而那正是「六个节点各自返回增量、由图层层合并」的正确用法。
    """

    # ── 输入 ──────────────────────────────────────────────────────────────────
    #: 对话历史（OpenAI 格式）。**唯一**用「追加」语义的键。
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: Annotated[int, LastValue]
    role: Annotated[str, LastValue]

    # ── 身份（router 写，别人只读）─────────────────────────────────────────────
    permissions: Annotated[frozenset[str], LastValue]
    is_customer: Annotated[bool, LastValue]

    # ── 分支 ─────────────────────────────────────────────────────────────────
    route: Annotated[str, LastValue]
    route_confidence: Annotated[str, LastValue]
    route_reason: Annotated[str, LastValue]

    # ── 产物 ─────────────────────────────────────────────────────────────────
    sql: Annotated[str, LastValue]
    sql_attempts: Annotated[int, LastValue]
    rows: Annotated[list[dict[str, Any]], LastValue]
    row_meta: Annotated[dict[str, Any], LastValue]
    docs: Annotated[list[Any], LastValue]
    chart_spec: Annotated[dict[str, Any] | None, LastValue]
    analysis: Annotated[dict[str, Any], LastValue]
    answer: Annotated[str, LastValue]
    citations: Annotated[list[str], LastValue]
    refusals: Annotated[list[str], LastValue]

    # ── 护栏（横切）──────────────────────────────────────────────────────────
    blocked: Annotated[bool, LastValue]
    guardrail_stage: Annotated[str, LastValue]
    guardrail_rules: Annotated[list[str], LastValue]
    guardrail_message: Annotated[str, LastValue]


class GraphState(GraphStateDoc, total=False):  # type: ignore[misc]
    """对外暴露的名字（`8.graph.md` §2 的那一行 import）。

    它继承 `GraphStateDoc`，所以**既有 reducer 又有全部字段**；
    与 `app/agents/state.GraphState` 的字段集合由 `tests/graph/test_graph_state.py`
    逐键比对钉住（两处漂移会立刻变红，而不是等到第 9 步运行时才发现少了一个键）。
    """


# ── 节点名与分支名（一处真相，路由表与 add_node 都从这里取）─────────────────────

#: 图里**真正**注册的节点名。
#:
#: ⚠️ 六个 Agent 对应**七个**图节点：`guardrail` 出现两次（输入侧在 router 之前、
#: 输出侧在 synthesis 之后），这是 `8.graph.md` §4 第 1 条「横切」的落地方式 ——
#: 同一份代码、两个方向，靠 `stage=` 区分。用**两个节点名**而不是一个自环，
#: 是因为 checkpoint 按节点名记「这一步是谁跑的」，同名会让重放时看不出卡在哪一侧。
#: 另有 `gate` 一个**条件注册**的节点（只在 `InterruptPolicy.enabled` 时出现）。
NODE_GUARDRAIL_INPUT: Final[str] = "guardrail_input"
NODE_ROUTER: Final[str] = "router"
NODE_DATA: Final[str] = "data"
NODE_RAG: Final[str] = "rag"
NODE_ANALYSIS: Final[str] = "analysis"
NODE_SYNTHESIS: Final[str] = "synthesis"
#: 输出侧护栏：synthesis 之后。
#:
#: ⚠️ 它同时承担「通过时把这一轮问答追加进 `messages` 历史」这第二件事。
#: 为什么不单独设一个 `finish` 节点：LangGraph 的 `add_conditional_edges` 允许把
#: **节点**作为目的地，而这会让 `get_graph()`（`draw_mermaid()` / 图结构断言）内部
#: 的边遍历抛 `TypeError: '<' not supported between instances of 'NoneType' and 'str'`
#: —— 图**跑起来是好的**，但「图结构对不对」这件事变得不可验证（实测，见 runbook §10.1 坑 3）。
#: 宁可让收尾动作落在已经存在的节点里（它本来就知道这一轮是否通过），
#: 也不要为了一个动作多一个节点、并把图的可检查性赔进去。
NODE_GUARDRAIL_OUTPUT: Final[str] = "guardrail_output"
#: 人工审批闸门（`InterruptPolicy.enabled` 为真时才注册，见 `builder.build_graph`）。
#: 挂在 router 之后、处理节点之前：敏感操作要**在花任何代价之前**停住。
NODE_GATE: Final[str] = "gate"

#: `route` → **第一个要跑的**节点名（条件边的 `path_map`）。取值域见 `app.agents.state.ROUTES`。
#:
#: ⚠️ `analysis` 指向 `data` 而**不是** `analysis` 节点，这不是笔误：
#: `analysis_node` 的输入是 `rows`（它刻意不自己生成 SQL，避免出现第二处 SQL 生成逻辑），
#: 所以「对内趋势分析」这条链路的第一个节点必然是 `data`。真正决定「要不要接着做分析」
#: 的是 `data` 之后的分支（`routing.after_data`）—— 见 `8.graph.md` §4 第 1 条与
#: `tests/graph/test_graph_paths.py::test_route_analysis_runs_data_then_analysis`。
#: 若这里写成 `analysis`，症状是「analysis 报『没有可分析的数据行』」：
#: 图照跑、日志有话说，但**没有任何地方报错**（实测，见 runbook §10.1 坑 7）。
ROUTE_NODES: Final[dict[str, str]] = {
    "data": NODE_DATA,
    "rag": NODE_RAG,
    "analysis": NODE_DATA,
    # ticket / final 是**写操作与直达出口**：第 9 步落地的 ticket 节点与
    # human-in-the-loop 闸门挂在这里（`8.graph.md` §5 第四条）。
    # 现在两者都直达 synthesis —— synthesis 会照着 `refusals` 如实作答，
    # 于是「工单能力还没接上」是一句实话，而不是一个 500。
    "ticket": NODE_SYNTHESIS,
    "final": NODE_SYNTHESIS,
}

#: 条件边的名字（LangGraph 用它给分支起名，测试与图可视化都读它）
BRANCH_AFTER_GUARDRAIL: Final[str] = "after_guardrail_input"
BRANCH_ROUTE: Final[str] = "route_by_intent"
BRANCH_AFTER_OUTPUT_GUARDRAIL: Final[str] = "after_guardrail_output"


# ── `messages` 的两种形态之间的转换 ──────────────────────────────────────────


def to_openai_messages(messages: Any) -> list[dict[str, Any]]:
    """把 `messages` 归一成节点认识的形状：`{"role": ..., "content": ...}` 的普通 dict。

    **为什么必须有这个函数**（实测踩到过，见 runbook §10.1 坑 2）：
    `add_messages` 会把 dict 归一成 `HumanMessage` 之类的对象，而六个节点的取值口径
    （`app/agents/state.messages_of` / `last_message`）读的是 `message.get("role")`。
    `BaseMessage` 没有 `.get()` —— 第二轮请求会直接 `AttributeError`，
    而第一轮完全正常，症状是「单轮测试全绿、多轮一用就崩」。

    `RemoveMessage`（删除指令）原样保留：把它当成普通消息转成 dict 会让 reducer
    把「删除」当成「追加一条」。
    """
    if not isinstance(messages, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, RemoveMessage):
            out.append(message)  # type: ignore[arg-type]
        elif isinstance(message, BaseMessage):
            out.append({"role": _role_of(message), "content": _text_of(message)})
        elif isinstance(message, dict):
            out.append(dict(message))
    return out


#: `BaseMessage` 类型 → OpenAI 的 role 名。刻意用 `isinstance` 而不是 `message.type`：
#: `type` 对自定义消息是类名小写，靠它会漏掉第三方子类（而漏掉的表现是「角色变成 user」，
#: 静默且难查）。
_ROLE_BY_TYPE: Final[tuple[tuple[type[BaseMessage], str], ...]] = (
    (SystemMessage, "system"),
    (HumanMessage, "user"),
    (AIMessage, "assistant"),
)


def _role_of(message: BaseMessage) -> str:
    for cls, role in _ROLE_BY_TYPE:
        if isinstance(message, cls):
            return role
    return str(getattr(message, "type", "") or "user")


def _text_of(message: BaseMessage) -> str:
    """取消息文本；内容是多段（多模态）时只保留文本段。"""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


__all__ = [
    "BRANCH_AFTER_GUARDRAIL",
    "BRANCH_AFTER_OUTPUT_GUARDRAIL",
    "BRANCH_ROUTE",
    "GraphState",
    "GraphStateDoc",
    "NODE_ANALYSIS",
    "NODE_DATA",
    "NODE_GATE",
    "NODE_GUARDRAIL_INPUT",
    "NODE_GUARDRAIL_OUTPUT",
    "NODE_RAG",
    "NODE_ROUTER",
    "NODE_SYNTHESIS",
    "ROUTE_NODES",
    "to_openai_messages",
]
