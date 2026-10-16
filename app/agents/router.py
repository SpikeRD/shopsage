"""Router Agent：**意图识别 + 客户/员工分流 + 权限上下文注入**（`7.agents_module.md` §1、§4 第 4 条）。

它做三件事，顺序不能换：

1. **意图分类**（LLM）：把用户那句话归到 `data` / `rag` / `analysis` / `ticket` / `final` 之一。
2. **权限注入**（查表，**不问模型**）：`role` → 权限点集合 → 写进 state。
   这是 `inject_identity()` 的调用点，也是 §4 第 4 条的全部内容。
3. **保守兜底**：分类不确定时**不抛异常、不猜高风险的 `data`**，而是走 `rag` 并把
   `route_confidence` 标成 `low`。

为什么是「意图分类」而不是一个大 prompt 全干
------------------------------------------

`7.agents_module.md` §5 第一条：单 Agent 大 prompt 的意图是模糊的 —— 它既要判断
「该查库还是查知识库」，又要生成 SQL、又要控语气，出问题时你分不清是哪一环坏了。
拆开之后每一环的输入输出都能单独测、单独调 prompt，也能单独统计准确率（第 16 步 golden set）。

为什么兜底走 `rag` 而不是 `data`
-------------------------------

`rag` 是**只读且无副作用**的路径（查不到就如实说「知识库里没有」），
`data` 则会执行 SQL、消耗数据库资源、并且对客路径要处理越权。
不确定时选代价小的那条 —— 判错了最多是「答非所问一次」，
反过来（该 rag 却生成了 SQL）会污染审计日志与指标，排查时噪声更大。

⚠️ 本模块**不做**的三件事：不做实体抽取（问题里的订单号由 data 节点的 SQL 参数承载）、
不查库（router 必须能在没有数据库的环境里跑，否则「每个 Agent 独立可测」就不成立）、
不判断权限点是否够（那是各节点消费 `permissions` 时的事）。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from app.agents.agent_llm import JsonLLM, build_system_prompt, generate_json
from app.agents.state import (
    ROUTES,
    GraphState,
    inject_identity,
    last_user_message,
    role_of,
)
from app.security.policy import CUSTOMER_ROLE

logger = logging.getLogger(__name__)

#: 保守兜底路径（理由见模块 docstring）
FALLBACK_ROUTE: Final[str] = "rag"

#: 低置信度的判据：模型自报的 certainty 低于它就标 `low`。
#: 为什么用模型的**自报值**而不是自己算一个分数：第 7 步还没有校准过的打分器，
#: 拿一个来路不明的数字当阈值，比让模型给个粗略的自评更不可靠。
#: `low` 的用途是**留痕**（第 11 步埋点、第 16 步看错分样本），不是改变分支 ——
#: 真正的分支靠 `ROUTES` 校验 + 兜底。
LOW_CONFIDENCE: Final[float] = 0.5

ROUTER_SYSTEM: Final[str] = build_system_prompt(
    "你是电商智能助手的**意图分流器**。你只判断「这个问题该交给哪个处理者」，"
    "不回答问题本身、不生成 SQL、不猜用户数据。\n\n"
    "可选的路线（只能是其中之一）：\n"
    "- data：需要查**数据库里的具体业务数据**（订单、退款、库存、客户自己的记录等），"
    "典型问法「我的订单到哪了」「这个月退货了几单」。\n"
    "- rag：需要查**知识库/文档/政策**（退货政策、保修条款、流程说明），"
    "典型问法「退货政策是什么」「怎么开票」。\n"
    "- analysis：**对内**的数据聚合与解读（趋势、分布、对比、排行），"
    "典型问法「最近三个月的销售趋势」「各类目销量占比」。\n"
    "- refund：要求**申请退款 / 退钱 / 退货退款**，典型问法「我要退款」「这单帮我退了吧」"
    "「订单 1024 退一下」。\n"
    "- ticket：要求**建工单 / 报修 / 投诉**，典型问法「帮我报修」「我要投诉物流」。\n"
    "- final：不需要任何检索，纯寒暄、澄清、对上一轮的确认，典型问法「你好」「谢谢」。\n\n"
    "判定要点：\n"
    "1. 「政策/流程/怎么算」类问题永远不是 data —— 即使问法里带业务名词；\n"
    "2. analysis 只对**内部员工**开放，对客问题里出现「趋势/占比/排行」也归 data；\n"
    "3. **「退款」与「投诉/报修」必须分开**：前者是 refund（要退钱），后者是 ticket（建单）；\n"
    "4. 拿不准时选 rag，并在 certainty 上如实给低分；\n"
    "5. 只输出 JSON，不要解释。",
    output_schema='{"route": "data|rag|analysis|refund|ticket|final", '
    '"certainty": 0~1 的小数, "reason": "不超过 20 字的判断依据"}',
)

# ── few-shot：固定、可回归的示例 ─────────────────────────────────────────────
# `7.agents_module.md` §5 第二条的原话是「示例固定可回归」——
# 示例写进 prompt 之后就不再是「某个工程师的临场发挥」，第 16 步的 golden set
# 才能稳定地衡量「改 prompt 到底让准确率升了还是降了」。
# 五条示例刻意覆盖四个边界：政策类带业务名词、对内/对客的 analysis 差异、寒暄、建单。
ROUTER_EXAMPLES: Final[tuple[tuple[str, str], ...]] = (
    ("我的订单 1024 什么时候能到？", "data"),
    ("退货政策是什么？七天无理由怎么算的？", "rag"),
    ("最近三个月各类目的销售占比是什么样的？", "analysis"),
    ("物流太慢了，帮我建个工单投诉一下。", "ticket"),
    # ⚠️ 变更 003 新增的示例：它是 refund 与 ticket 的**边界样本**。
    # 这两者最容易混（都要「处理一下订单」），而它们的写路径与权限点完全不同：
    # refund 走 request:refund 落一张退款单，ticket 走 write:ticket 建工单。
    # 示例里不放一条「退款」样本，模型就会把「我要退款」判成 ticket ——
    # 结果是客户拿到一张工单，而他的钱一分没动。
    ("这单我不要了，帮我退款。", "refund"),
    ("你好", "final"),
)


def build_router_messages(text: str, *, role: str, is_internal: bool) -> list[dict[str, Any]]:
    """拼 router 的 prompt（**纯函数**，便于单独断言 prompt 内容）。

    当前问题单独放在最后一条 user 消息里、历史示例放在前面：
    模型对「最后一条 user 消息」的注意力最集中（这也是为什么修复轮只追加 user 消息）。
    """
    transcript = "\n".join(f"用户：{question}\n路线：{route}" for question, route in ROUTER_EXAMPLES)
    scope = "内部员工（可以走 analysis 做数据聚合）" if is_internal else "外部客户（只能看自己的数据）"
    return [
        {"role": "system", "content": ROUTER_SYSTEM},
        {"role": "user", "content": f"以下是示例，只用来对齐判定口径：\n{transcript}"},
        {"role": "assistant", "content": "明白，我按这套口径判定。"},
        {"role": "user", "content": f"提问者身份：{scope}\n当前问题：{text}"},
    ]


def _normalize_route(value: Any) -> tuple[str, bool]:
    """把模型给的路线归一成 `ROUTES` 里的一个；返回 `(路线, 是否为保守兜底)`。

    ⚠️ 未知取值**不报错**：模型偶尔会吐出 `"sql"` / `"knowledge"` 这类同义改写，
    为此让整个请求失败，用户看到的是「系统故障」，而它本可以走保守路径正常回答。
    但**也不静默**：兜底一定打 WARNING 并写进 `route_reason`，
    否则「路由准确率」会被兜底悄悄拉高，第 16 步看到的数字就是假的。
    """
    route = str(value or "").strip().lower()
    if route in ROUTES:
        return route, False
    return FALLBACK_ROUTE, True


def _normalize_certainty(value: Any) -> float:
    """把 `certainty` 夹到 `[0, 1]`；给不出就是 0（等于「没把握」，走保守解释）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(number, 0.0), 1.0)


async def classify(
    text: str, *, role: str = CUSTOMER_ROLE, llm: JsonLLM | None = None
) -> dict[str, Any]:
    """只做「分类」这一步，返回 `{"route", "certainty", "reason", "fell_back"}`。

    单独暴露（而不是塞进 `router_node`）是为了让**第 16 步的评测**能直接调它：
    评测要的是「分类准不准」，不需要构造整个 state，也不该被权限注入的逻辑干扰。
    """
    is_internal = role != CUSTOMER_ROLE
    messages = build_router_messages(text, role=role, is_internal=is_internal)
    payload = await generate_json(messages, llm=llm, agent="router", temperature=0.0)

    route, fell_back = _normalize_route(payload.get("route"))
    certainty = _normalize_certainty(payload.get("certainty"))
    reason = str(payload.get("reason", "")).strip()[:120]

    if fell_back:
        logger.warning(
            "router 收到未登记的路线 %r（提问者角色 %s），按保守口径落到 %s。"
            "若这类取值反复出现，说明 prompt 的取值域没被模型理解，要去改 prompt 而不是加兜底。",
            payload.get("route"),
            role,
            FALLBACK_ROUTE,
        )
        reason = f"模型给出的路线无法识别（{payload.get('route')!r}），保守落到 {FALLBACK_ROUTE}"
        # ⚠️ 兜底的置信度**必须归零**：模型可能给一个「路线不认识、但很自信」的组合
        # （实测：`{"route": "sql", "certainty": 0.9}` 会让兜底被标成 high confidence）。
        # 不归零的后果是「兜底把路由准确率的低分掩盖掉」——
        # 这条置信度是第 11/16 步筛错分样本的依据，它必须反映**真实把握**，而不是模型的口气。
        certainty = 0.0
    elif route == "analysis" and not is_internal:
        # 对客问题里出现「趋势/占比」很常见，但 analysis 是对内能力（要读全站数据）。
        # 这里降级到 data 并注明原因：对客走 analysis 会直接撞上 RBAC（PermissionDenied），
        # 与其让用户看到一个 403，不如按「你自己的数据也能做聚合」处理。
        logger.info("对客请求被判成 analysis，降级到 data（analysis 只对内部员工开放）")
        route, reason = "data", "analysis 仅对内部开放，对客降级为 data"

    return {"route": route, "certainty": certainty, "reason": reason, "fell_back": fell_back}


async def router_node(state: GraphState, *, llm: JsonLLM | None = None) -> dict[str, Any]:
    """图节点：分流 + 注入权限上下文，返回要写进 state 的那几个键。

    输出（见 `7.agents_module.md` §4 第 1 条要求的结构化结果）：
        route / route_confidence / route_reason：给第 8 步的条件边用；
        permissions / is_customer / role / user_id：给后续所有节点消费的身份上下文。

    ⚠️ **没有用户消息时的处置**：路由到 `final`（什么都不查）。
    这比抛异常合理：`final` 的语义就是「不需要任何数据」，而空输入恰恰不需要数据。
    """
    text = last_user_message(state)
    role = role_of(state)
    if not text.strip():
        logger.info("state 里没有 user 消息，router 直接落到 final（无需检索）")
        return {
            "route": "final",
            "route_confidence": "low",
            "route_reason": "没有用户消息，无需检索",
            **inject_identity(int(state.get("user_id") or 0), role),
        }

    result = await classify(text, role=role, llm=llm)
    confidence = "high" if result["certainty"] >= LOW_CONFIDENCE else "low"
    if confidence == "low":
        logger.info(
            "router 置信度低（certainty=%s，路线=%s）：保留路线但标记 low，便于第 11/16 步追查错分样本",
            result["certainty"],
            result["route"],
        )

    return {
        "route": result["route"],
        "route_confidence": confidence,
        "route_reason": result["reason"],
        **inject_identity(int(state.get("user_id") or 0), role),
    }


__all__ = [
    "FALLBACK_ROUTE",
    "LOW_CONFIDENCE",
    "ROUTER_EXAMPLES",
    "ROUTER_SYSTEM",
    "build_router_messages",
    "classify",
    "router_node",
]
