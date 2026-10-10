"""条件边的路由函数：**图的分支只有这里一处真相**（`8.graph.md` §4 第 4 条）。

为什么路由是几个独立的小函数、而不是塞进节点里的 `if`：

* **可测**：每个判据都能单独喂 state 断言目的地，不必构造整张图（§7 的用例里有直接调它们的）；
* **可看**：`get_graph().draw_mermaid()` 画的就是这里的返回值，路由写进节点里就画不出来；
* **可审**：与越权相关的分支（护栏拦截、敏感操作闸门）集中在一处，改错时只有一处要读。

三条不变式（写在这里而不是散在注释里，因为它们都属于「错了也不报错」的那一类）：

1. **护栏短路优先于任何业务分支**：`blocked` 为真时一律不再往下走。
   顺序写反的后果是「被拦下的注入请求照样查了库」，而日志上看不出区别。
2. **路由目的地必须是已注册的节点**：`ROUTE_NODES` 漏一个键时 LangGraph 会在编译期
   报未知节点（这是好事，fail-fast），但**路由表里有、图里没注册**是编译期就会拦住的。
3. **闸门在路由之后、处理节点之前**：敏感操作要**在花任何代价之前**停住 ——
   生成 SQL、检索、调用写路径都还没发生。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END

from app.graph.config import GraphRuntime
from app.graph.state import (
    NODE_GATE,
    NODE_ROUTER,
    NODE_SYNTHESIS,
    ROUTE_NODES,
)


def after_guardrail_input(state: Any) -> str:
    """输入侧护栏之后：被拦 → `END`（短路）；否则 → `router`。

    这是 D2（「失败即短路」）在图上的落点。被拦时返回 `END` 而不是「跳过若干节点去 synthesis」：
    护栏自己已经给了落地话术（`guardrail_message`），再走一遍 synthesis 等于
    多给模型一次机会把拦截理由说漏，而这一步本该是**终点**。
    """
    if bool((state or {}).get("blocked")):
        return END
    return NODE_ROUTER


def route_by_intent(state: Any) -> str:
    """按 `route` 分流到 data / rag / analysis / synthesis。

    ⚠️ **未知 route 一律落到 `synthesis`**（不抛异常）：route 的取值域在 router 里
    已被归一（`_normalize_route` 有兜底），到这里还出现未知值只可能是
    ① 有人手工 `aupdate_state` 写了野值，或 ② 新增了 route 却忘了登记 `ROUTE_NODES`。
    两种情况都不该让用户的请求变成 500 —— 落到 synthesis 会照着 `refusals` 如实作答。
    但**不静默**：这里打一条 WARNING，因为 ② 是必须被发现（并补一行路由表）的开发期错误。
    """
    import logging

    route = str((state or {}).get("route") or "")
    target = ROUTE_NODES.get(route)
    if target is None:
        logging.getLogger(__name__).warning(
            "route=%r 不在路由表里（state.ROUTES 有新增但 ROUTE_NODES 忘了登记？），"
            "保守落到 synthesis 由 refusals 如实作答",
            route,
        )
        return NODE_SYNTHESIS
    return target


def after_data(state: Any) -> str:
    """`data` 之后：`route=analysis` 时接着做分析；已被兜底标死时直接收尾。

    为什么 `analysis` 是 `data` 的**下游**而不是并列的一条分支：分析节点的输入是 `rows`，
    而生成 rows 的只有 data 节点（`analysis_node` 刻意不自己生成 SQL —— 那会出现第二处
    SQL 生成逻辑）。所以「对内趋势」这条链路是 `data → analysis → synthesis`。

    两个判据的顺序不能换：

    1. **先看 `blocked`**：data 被兜底标死（永久失败）时不能再进 analysis 浪费一次模型调用，
       也不能进 synthesis —— 兜底已经给出了落地话术，图应当直接结束；
    2. 再看 `route`：判据用 `route` 而不是「rows 非空」，因为 rows 为空时 analysis
       会自己落一条「没有可分析的数据行」的 refusal（那是它该说的话），
       用 rows 判空会把这条信息吃掉。
    """
    import logging

    from app.graph.state import NODE_ANALYSIS, NODE_SYNTHESIS

    if bool((state or {}).get("blocked")):
        return END
    route = str((state or {}).get("route") or "")
    if route == "analysis":
        return NODE_ANALYSIS
    if route not in ROUTE_NODES:
        logging.getLogger(__name__).warning("route=%r 不在路由表里，按直通 synthesis 处置", route)
    return NODE_SYNTHESIS


def after_guardrail_output(state: Any) -> str:
    """输出侧护栏之后：通过 → `END`（图结束）；被拦 → 也是 `END`，但状态不同。

    两种情况都到 `END`，差别在**状态**而不在拓扑：

    * 通过：`guardrail_output` 节点已顺手把这一轮问答追加进 `messages`（见 `nodes.py`）；
    * 被拦：`blocked=True` + `answer` 被换成安全话术，**不追加历史** ——
      否则下一轮的 router 会读到一条本不该存在的 assistant 回答（安全话术），
      把「拦截」伪装成「答过了」，多轮对话从此建立在假历史上。

    ⚠️ 这个函数**必须存在**（哪怕当前两个分支都返回 `END`）：它是「图怎么结束」
    的唯一落点。第 9 步要在这里接 SSE 收尾/计费结算时，改动只有这一处。
    """
    del state
    return END


def should_gate(runtime: GraphRuntime, state: Any) -> bool:
    """当前 state 是否需要人工审批（`InterruptPolicy` 的唯一消费点）。

    单独一个函数（而不是在节点里直接读 policy）的理由：
    「要不要停」在**三个地方**都要问 —— 图的构建期（决定挂不挂闸门节点）、
    运行期的分支函数、测试（断言策略生效）。三处各写一遍判断，就是三处可以漂移的真相。
    """
    return runtime.interrupts.requires_approval(state)


def after_router_gate(runtime: GraphRuntime) -> Any:
    """router 之后：需要审批 → `gate` 节点；否则 → 按意图分流。

    闸门是一道**真实的节点**（而不是路由里的一个分支）：它要能写状态 ——
    「未批准」必须落一条 `refusals`，否则 synthesis 不知道这件事没做，会照着空材料编。
    """

    def route(state: Any) -> str:
        if should_gate(runtime, state):
            return NODE_GATE
        return route_by_intent(state)

    return route


def after_gate(state: Any) -> str:
    """闸门之后：已批准 → 走原意图；未批准 → `synthesis`（如实转述 refusal）。

    判据复用 `blocked`（而不是新造一个 `approved` 键）：图里「不能继续往下走」
    应该只有一种表达，多一个语义相同的键，条件边就会有两处需要同步的真相。
    """
    if bool((state or {}).get("blocked")):
        return NODE_SYNTHESIS
    return route_by_intent(state)


__all__ = [
    "after_data",
    "after_gate",
    "after_guardrail_input",
    "after_guardrail_output",
    "after_router_gate",
    "route_by_intent",
    "should_gate",
]
