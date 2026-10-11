"""六个 Agent → 七个图节点：**注入点绑定 + 状态归一 + 图级兜底**。

本模块是「第 7 步的节点」与「第 8 步的图」之间唯一的一层胶水，它只做三件事，
每一件都是图**必须**做、而节点**不该**做（否则六个 Agent 就不独立了）的事：

1. **注入点绑定**：把 `llm=` / `search=` 提前绑好，图节点退化成 `fn(state) -> dict`。
   为什么不在节点里读配置：那样「第 7 步的节点可独立测试」这条验收会被破坏 ——
   测试得改全局配置才能换掉模型。
2. **状态归一**（`normalize_input`）：把 `messages` 从 `BaseMessage` 转回普通 dict。
   `add_messages` 会把 dict 归一成对象，而节点的取值口径读 `message.get("role")`；
   第二轮请求会因此 `AttributeError`，而第一轮正常 —— 典型的「单轮全绿、多轮崩」。
3. **图级兜底的素材**（`failure_update`）：节点抛异常时**不当场兜底**（那样会吃掉断点），
   而是让异常穿透到 `app/graph/run.py` 的回合驱动，由那里把失败翻成 `refusals` +
   落地话术。本模块只提供那个翻译函数 —— 它是「出错之后怎么收场」的唯一实现。
   `8.graph.md` §5 第三条说「失败要从断点重放」，但重放的前提是**状态还在**；
   让异常穿透到 `ainvoke` 的调用方，状态不会丢，却会让用户看到一个 500 ——
   而对客路径上一次 500 的代价（信任）比一次「我查不到」高得多。
   ⚠️ **可重试错误（`RetryableToolError` / `RetryableLLMError`）刻意不兜**：
   那是第 12 步队列要重投的，兜住等于把可恢复故障伪装成「查不到」。

## 为什么兜底写在节点包装里，而不是用 LangGraph 的 `error_handler=`

LangGraph 1.2 确实提供了 `add_node(..., error_handler=..., destinations=...)`，
本步**第一版就是用它写的**，图也跑得通 —— 但它会让 `get_graph()` 抛
`TypeError: '<' not supported between instances of 'NoneType' and 'str'`
（`_draw.py` 遍历边时对「带标签的隐式边」和「显式边」一起排序，`error_handler`
生成的内部节点产出的边标签是 `None`）。后果不是「图坏了」，而是**图结构不再可验证**：
`draw_mermaid()` 与「边表断言」这两条最有价值的验收手段同时失效（实测见 runbook §10.1 坑 3）。
所以改成：兜底留在节点内部（一个 `try/except`，行为完全一样），
换来 `get_graph()` 可用、图结构可断言。代价是「失败后跳哪里」从声明式变成命令式 ——
这笔交换是划算的，因为前者是框架细节，后者是我们的验收标准。

## 为什么 `guardrail` 是两个节点

`8.graph.md` §4 第 1 条要求 guardrail 横切（router 后 + synthesis 后）。落地成
`guardrail_input` / `guardrail_output` 两个**独立的图节点**，而不是一个自环节点：
checkpoint 按节点名记录「这一步是谁跑的」，同名自环会让重放时看不出卡在哪一侧，
也让「输出侧必须挂在 synthesis 之后」这条接线在图上不可见（本仓用图结构断言钉住它）。

## 为什么「把问答追加进历史」在 `guardrail_output` 里，而不是单独一个节点

`add_messages` 只负责**合并**，不负责**决定追加什么**，所以需要一个落点。落点有三个候选：

* 单独一个 `finish` 节点 —— **不可选**：条件边一旦把「节点」当目的地，
  `get_graph()` 就会抛上面那个 `TypeError`（同样的坑），图结构又变得不可验证；
* 调用方（第 9 步 API）自己写 —— 每个调用方都要记得写，漏一处就是「多轮失忆」；
* `guardrail_output` 通过时顺手写 —— **选它**：这一轮「可否进历史」正是它刚判过的结论，
  而被拦下的一轮**不该**进历史（安全话术会污染下一轮的 router 判断）。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.agents.agent_llm import JsonLLM
from app.agents.analysis import analysis_node
from app.agents.data import data_node
from app.agents.guardrail import INPUT_STAGE, OUTPUT_STAGE, guardrail_node
from app.agents.rag import rag_node
from app.agents.router import router_node
from app.agents.state import GraphState as AgentGraphState
from app.agents.synthesis import synthesis_node
from app.graph.config import APPROVAL_QUESTION, REJECTED_REASON, GraphRuntime
from app.graph.routing import should_gate
from app.graph.state import (
    NODE_ANALYSIS,
    NODE_DATA,
    NODE_GATE,
    NODE_GUARDRAIL_INPUT,
    NODE_GUARDRAIL_OUTPUT,
    NODE_RAG,
    NODE_ROUTER,
    NODE_SYNTHESIS,
    to_openai_messages,
)
from app.observability.tracing import trace_span

logger = logging.getLogger(__name__)

#: 图级兜底给用户的落地话术（**固定文本**，不允许模型现编）。
#:
#: 刻意与 `guardrail.BLOCKED_CUSTOMER_MESSAGE` 分开：那条是「请求不合规」，
#: 这条是「系统这次没做到」，两者对用户的含义完全不同 ——
#: 用同一句话会让合规拦截看起来像系统故障，也会让故障看起来像用户的问题。
FALLBACK_MESSAGE: Final[str] = (
    "抱歉，这次没能查到需要的信息，请稍后重试；如果一直这样，请联系人工客服。"
)

#: 网络型节点（异常时按「这份材料没拿到」兜底，继续走 synthesis）
NETWORK_NODES: Final[tuple[str, ...]] = (NODE_DATA, NODE_RAG, NODE_ANALYSIS)

#: 节点名 → 可读的中文角色（日志与 `refusals` 里用，避免出现 `guardrail_output 挂了` 这种中英混排）
NODE_LABELS: Final[dict[str, str]] = {
    NODE_GUARDRAIL_INPUT: "输入护栏",
    NODE_ROUTER: "路由",
    NODE_DATA: "数据查询",
    NODE_RAG: "知识库检索",
    NODE_ANALYSIS: "数据分析",
    NODE_SYNTHESIS: "回答合成",
    NODE_GUARDRAIL_OUTPUT: "输出护栏",
    NODE_GATE: "人工审批闸门",
}


def normalize_input(state: Any) -> AgentGraphState:
    """把图给的 state 归一成六个节点认识的形状（`messages` 转回普通 dict）。

    见模块 docstring 第 2 条：`add_messages` 把 dict 变成 `HumanMessage` 之后，
    `last_user_message()` / `messages_of()` 的 `.get("role")` 会失效。
    归一放在**每个节点的入口**（而不是图入口一次），因为恢复执行（`Command(resume=...)`）
    与 `aupdate_state` 都会重新喂入 checkpoint 里存下来的对象形态。
    """
    normalized: dict[str, Any] = dict(state) if isinstance(state, dict) else {}
    normalized["messages"] = to_openai_messages(normalized.get("messages"))
    return normalized  # type: ignore[return-value]


def failure_update(node_name: str, error: Exception, state: Any) -> dict[str, Any]:
    """把一次**永久性**失败翻成要写进 state 的增量（图级兜底的唯一实现）。

    调用方是 `app/graph/run.py` 的回合驱动（**不是**节点自己）：
    节点里的 `except` 会让「断点」消失，理由见 `safe_node` 的 docstring。

    两类处置（本模块唯一的「按节点名分支」，所以写清理由）：

    * **网络型节点**（data / rag / analysis）：写 `refusals` 后**继续**往下走 ——
      少一份材料不等于答不了，synthesis 的材料边界会告诉模型「这块没拿到」；
    * **收口型节点**（synthesis / guardrail_output）：**必须停下**。
      synthesis 崩了就没有 `answer`，放行只会让空回答出网；
      guardrail_output 崩了等于**没有审查**，而 fail-open 的审查比没有审查更危险
      （它会让人以为审过了）。两者的处置都是「给一句安全话术 + 标 blocked + 不再往下走」。
    """
    label = NODE_LABELS.get(node_name, node_name)
    continues = node_name in NETWORK_NODES
    reason = f"{label}这一步没做成（{type(error).__name__}）：{error}"
    logger.warning("图级兜底：%s → %s", reason, "继续（只少一份材料）" if continues else "中止本轮")
    out: dict[str, Any] = {
        "refusals": list((state or {}).get("refusals") or []) + [reason],
        "row_meta": {
            **((state or {}).get("row_meta") or {}),
            "failed_node": node_name,
            "error_kind": type(error).__name__,
        },
    }
    if not continues:
        # `guardrail_stage="fallback"` 是刻意的新取值：埋点（第 11 步）要能区分
        # 「护栏拦下的」与「系统没做到的」——两者的运营动作完全不同（前者查攻击，后者查故障）。
        out |= {
            "blocked": True,
            "guardrail_stage": "fallback",
            "guardrail_rules": [f"node_failure:{node_name}"],
            "guardrail_message": FALLBACK_MESSAGE,
            "answer": FALLBACK_MESSAGE,
        }
    return out


def safe_node(fn: Any) -> Any:
    """把一个 Agent 节点包成图节点：**归一 state → 埋点 → 调用**。

    输入位置参数**原样透传**（`xxxx_node(state)` / `guardrail_node(state, stage=...)`），
    返回值原样返回（**增量语义不变**），所以「谁写了哪个键」依然可断言。

    ⚠️ 这里**刻意不 try/except**（本步第一版是 catch 的，改动理由很重要）：
    节点抛异常时如果就地兜底成 `blocked` 并返回，LangGraph 会认为**这一步成功完成**、
    于是把状态推进到"已完成"，`next` 变成空元组 —— 断点**就没了**：
    故障被"吃掉"的同时，重放所需的 checkpoint 位置也一起丢了（实测见 runbook §10.1 坑 8）。
    异常穿透出去时，LangGraph 既不写这一步的 checkpoint、也不推进 `next`，
    于是 `aget_state().next == (失败节点,)`，`ainvoke(None, cfg)` 就能从断点重放。

    那么「图级兜底」在哪？在**回合边界**（`app/graph/run.py` 的 `Turn`）：
    调用方在那里决定「重放几次」「重放不成怎么对用户说」。这样两件事同时成立：
    状态可重放（异常穿出去），用户不会看到 500（边界兜底）。

    ## 第 11 步：埋点为什么落在这里（而不是六个 Agent 各埋一次）

    `safe_node` 是**七个节点唯一的共同入口**（`node_specs()` 把它们全交给它包装），
    所以在这里包一层 span，就同时满足两件事：

    * 文档 §4 第 1 条要的「每个 Agent 节点都埋点」是**结构性成立**的 ——
      新增一个节点忘了埋点这件事**不可能发生**（没有别的入口）；
    * 六个 Agent 模块继续保持「不知道可观测性存在」—— 它们的注入点里
      没有 `telemetry=` 这种参数，于是「第 7 步的节点可独立测试」这条验收不受影响。

    埋点的 span 名是 `node.<节点名>`，耗时进 `AGENT_DURATION{node=...}`。
    B1（「P99 高但不知道慢在哪」）的答案就是这两样东西拼出来的：
    **按 `node` 标签分组看 P99**，哪个节点高就去 trace 里看它内部那几步。
    """
    node_name = getattr(fn, "__node_name__", getattr(fn, "__name__", "node"))

    async def node(state: AgentGraphState, config: RunnableConfig) -> dict[str, Any]:
        del config  # 图会把 RunnableConfig 透传进来；六个节点的契约里没有它
        # `trace_span` 是**同步**上下文管理器，而本函数是 async —— 这里可以正常 `with`：
        # span 的存活期就是 `await fn(...)` 这一段，OTEL 用 contextvars 传递，
        # 所以 `await` 期间 span 仍是「当前 span」，六个节点内部的 LLM/检索 span 会自动挂到它下面。
        with trace_span(f"node.{node_name}", kind="node", attributes={"node": node_name}) as handle:
            # `stage` 记路由结论（`""` = 还没路由，`data`/`rag`/… = 已知分支）。
            # 一次请求的 span 树里有了它，「这一步为什么跑」不用再去翻 state。
            handle.set_attributes({"stage": str(state.get("route") or "")})
            return await fn(normalize_input(state))

    node.__name__ = node_name
    return node


def _named(fn: Any, name: str) -> Any:
    """给闭包起个节点名（`safe_node` 靠它决定兜底策略）。"""
    fn.__node_name__ = name
    return fn


def make_guardrail_input(runtime: GraphRuntime) -> Any:
    """输入侧护栏节点（router 之前，零 token）。"""

    async def node(state: AgentGraphState) -> dict[str, Any]:
        return await guardrail_node(state, stage=INPUT_STAGE)

    return safe_node(_named(node, NODE_GUARDRAIL_INPUT))


def make_router(runtime: GraphRuntime) -> Any:
    """路由节点：意图分类 + 权限注入（`8.graph.md` §4 第 4 条的落点）。"""

    async def node(state: AgentGraphState) -> dict[str, Any]:
        return await router_node(state, llm=runtime.llm)

    return safe_node(_named(node, NODE_ROUTER))


def make_data(runtime: GraphRuntime) -> Any:
    """数据节点：NL→SQL → 守卫 → 执行（对客路径强制行级过滤）。"""

    async def node(state: AgentGraphState) -> dict[str, Any]:
        return await data_node(
            state,
            llm=runtime.llm,
            row_limit=runtime.row_limit,
            max_attempts=runtime.max_attempts,
        )

    return safe_node(_named(node, NODE_DATA))


def make_rag(runtime: GraphRuntime) -> Any:
    """检索节点：未接真实后端时落 `refusals`（不假装「检不到」）。"""

    async def node(state: AgentGraphState) -> dict[str, Any]:
        return await rag_node(
            state, search=runtime.search, top_k=runtime.top_k, min_score=runtime.min_score
        )

    return safe_node(_named(node, NODE_RAG))


def make_analysis(runtime: GraphRuntime) -> Any:
    """分析节点：权限闸门 + 结构化结论 + 可选图表。"""

    async def node(state: AgentGraphState) -> dict[str, Any]:
        return await analysis_node(state, llm=runtime.llm, chart_type=runtime.chart_type)

    return safe_node(_named(node, NODE_ANALYSIS))


def make_synthesis(runtime: GraphRuntime) -> Any:
    """合成节点：按角色三套模板，写 `answer` + `citations`。"""

    async def node(state: AgentGraphState) -> dict[str, Any]:
        return await synthesis_node(state, llm=runtime.llm)

    return safe_node(_named(node, NODE_SYNTHESIS))


def make_guardrail_output(runtime: GraphRuntime) -> Any:
    """输出侧护栏（synthesis 之后）+ **通过时把这一轮问答追加进历史**。

    两件事合成一个节点，理由见模块 docstring 最后一段（`finish` 节点会让
    `get_graph()` 抛错）。追加的是 `{"role": "assistant", "content": answer}`，
    交给 `add_messages` 合并 —— 节点只返回**新增那一条**，不拼整段历史。
    """

    async def node(state: AgentGraphState) -> dict[str, Any]:
        # ⚠️ 这一行是「人工审批拒绝」能如实送达用户的前提（实测踩到，见 runbook §10.1 坑 9）：
        # 闸门把这一轮标死（`blocked=True` + refusal）之后，图还会走到这里，而
        # `guardrail_node` 在**通过**时会顺手写 `blocked=False` —— 于是
        # `turn.blocked` 变回 False，第 9 步的 API 据此返回 `delivered`，
        # 用户看到的是「这一轮正常答完了」，而实际上敏感操作**没有执行**。
        #
        # 第一版把「守住结论」写成「blocked 为真时直接返回护栏输出」——**没用**：
        # 输出侧护栏在**通过**时返回的就是 `{"blocked": False, ...}`，
        # 于是「跳过合并」跳掉的正是要守的那个值（`probe` 实测：守卫生效了，
        # 但出口仍是 `blocked=False`）。正确做法是**记住上游结论再原样写回**。
        already_blocked = bool(state.get("blocked"))
        outcome = await guardrail_node(state, stage=OUTPUT_STAGE, llm=runtime.llm)
        if already_blocked and not outcome.get("blocked"):
            # 守上游结论（见上面的长注释）。这一轮**不进历史**：
            # checkpointer 里的 state 会留下 `blocked=True` 与安全话术，
            # 但 `messages` 不受影响 —— 下一轮读不出「答过了」的假历史。
            return {**outcome, "blocked": True}
        if outcome.get("blocked"):
            # 被输出侧护栏拦下的一轮同样**不追加**历史（理由同上）：
            # 安全话术要是进了 messages，下一轮的 router 会把它当成「答过了」。
            return outcome
        # 通过时：历史里存的必须是**真正出网的那段文本**（= outcome 里的 answer）
        return {**outcome, **_answer_message({**state, **outcome})}

    return safe_node(_named(node, NODE_GUARDRAIL_OUTPUT))


def _answer_message(state: AgentGraphState) -> dict[str, Any]:
    """把这一轮的回答包成一条待追加的 assistant 消息（没有回答就什么都不追加）。

    ⚠️ 调用方传进来的必须是**合并了本节点产出之后**的状态：输出侧护栏拦下时
    `answer` 已被换成安全话术，历史里该存的是那段话术（真正出网的文本），
    而不是被拦下的原文。
    """
    answer = str(state.get("answer") or "")
    if not answer:
        return {}
    return {"messages": [{"role": "assistant", "content": answer}]}


def make_gate(runtime: GraphRuntime) -> Any:
    """人工审批闸门（human-in-the-loop，`8.graph.md` §5 第四条）。

    它只在 `InterruptPolicy.enabled` 为真时被注册进图（`builder.build_graph`）。
    三条分支，**顺序不能换**：

    1. **不需要停** → 原样放行（`blocked=False`）；
    2. **需要停且还没批** → `interrupt({"question": ...})`：本次执行在这一点**挂起**，
       checkpoint 记下 `next=('gate',)`，等批准通道用 `Command(resume=...)` 恢复；
    3. **恢复时**：`interrupt()` 的返回值就是批准通道传进来的决策（本项目传一个 dict）。
       批准 → 放行；拒绝 → 落 `REJECTED_REASON` 并标 `blocked`（fail-closed）。

    ⚠️ 第 3 条里「写 refusal」必须在**恢复之后**才发生 —— 这是 LangGraph 的语义决定的：
    `interrupt()` 抛出的控制流会丢弃本次调用的返回值，且恢复时**整个函数从头重跑**。
    若在 `interrupt()` 之前写状态，那条 refusal 要么丢失、要么被写两遍
    （幂等是重放的配套前提，§5 第三条）。所以状态只在「有结论」的那两个分支里写。

    ⚠️ 挂起时**不写** `blocked=True`：挂起不是拒绝。把它标成 blocked 会让
    「等人工点头」与「已被拦下」在状态上无法区分 —— 第 9 步的 API 于是没法决定
    该返回 202（等批准）还是 200（被拦）。挂起的事实由 `Turn.interrupt` 表达
    （它是从流的 `__interrupt__` 块里读出来的，见 `app/graph/run.py`）。
    """

    async def node(state: AgentGraphState) -> dict[str, Any]:
        if not should_gate(runtime, state):
            return {"blocked": False}
        decision = interrupt({"question": APPROVAL_QUESTION, "route": str(state.get("route") or "")})
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        if approved:
            logger.info(
                "gate：route=%r 已获人工批准（approver=%s），放行到处理节点",
                state.get("route"),
                (decision or {}).get("approver_user_id") if isinstance(decision, dict) else "?",
            )
            return {"blocked": False}
        reason = str(
            (decision or {}).get("reason") if isinstance(decision, dict) else ""
        ) or REJECTED_REASON
        logger.info("gate：route=%r 未获人工批准，记录 refusal 并跳过处理节点", state.get("route"))
        return {
            "blocked": True,
            "refusals": list(state.get("refusals") or []) + [reason],
        }

    return safe_node(_named(node, NODE_GATE))


def node_specs(runtime: GraphRuntime) -> dict[str, Any]:
    """图节点清单：`节点名 → 构造函数`（顺序即装配顺序，但边由 `builder` 决定）。

    为什么节点名用 `NODE_*` 常量而不是字面量：条件边的目的地是**字符串**，
    写错一个字不会报错、只会让图跑到 `END` 上去（症状是「分支之后什么都没发生」）。
    常量让「路由表」与「add_node 的键」在类型层面同源。
    """
    return {
        NODE_GUARDRAIL_INPUT: make_guardrail_input,
        NODE_ROUTER: make_router,
        NODE_DATA: make_data,
        NODE_RAG: make_rag,
        NODE_ANALYSIS: make_analysis,
        NODE_SYNTHESIS: make_synthesis,
        NODE_GUARDRAIL_OUTPUT: make_guardrail_output,
        NODE_GATE: make_gate,
    }


__all__ = [
    "FALLBACK_MESSAGE",
    "NETWORK_NODES",
    "NODE_LABELS",
    "JsonLLM",
    "failure_update",
    "make_analysis",
    "make_data",
    "make_gate",
    "make_guardrail_input",
    "make_guardrail_output",
    "make_rag",
    "make_router",
    "make_synthesis",
    "node_specs",
    "normalize_input",
    "safe_node",
]
