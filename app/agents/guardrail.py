"""Guardrail Agent：**输入/输出双向合规审查**（`7.agents_module.md` §4 第 2 条、D2）。

它是「横切」节点而不是顺序节点：router **之后**挂一次（输入侧），
synthesis **之后**再挂一次（输出侧）。同一份代码、两个方向，靠 `stage` 参数区分。

为什么输入侧不需要 LLM 判定，而输出侧需要
----------------------------------------

| | 输入侧 | 输出侧 |
|---|---|---|
| 判据 | 关键词/正则（`security.guardrail`，第 4 步已交付） | 泄漏与越权叙述**没有稳定关键词** |
| 成本 | 零 token，微秒级 | 一次额外 LLM 调用 |
| 位置 | 花任何钱（检索/SQL/模型）之前 | 出网之前 |

输入侧的关键词检测是**减速带**（`app/security/guardrail.py` 开篇就写明了）：
它便宜、早、可回归；真正的兜底是 SQL 守卫与只读角色。
输出侧则相反 —— 「把内部口径说给客户」不会命中任何固定词表，
所以那里加一层 LLM 判定（**同时保留确定性检查**，见下）。

两层输出的确定性检查（先跑，且**不花 token**）
---------------------------------------------

1. **阻断词表**：回答里出现 SQL/表名/审计 id/权限点名/节点名 → 直接拦。
   这条对**对客**路径严格生效；内部路径只记为告警（内部同事看到 `orders 表` 是正常工作需要）。
2. **PII 形态**：回答里出现未脱敏手机号/身份证/银行卡 → 拦（不区分角色：
   出口不该出现明文 PII，`security/masking.py` 已经负责把它变成 `138****1234`，
   这里出现明文形态说明有人绕过了脱敏出口）。

为什么内部路径不查阻断词，却仍要跑 LLM 判定
-------------------------------------------

内部回答里提表名、提 SQL 是正常协作方式（`7.agents_module.md` §5 第四条：
「对内专业、详细、可含数据表格」）。但对内同样有合规红线：
把 A 客户的明细回答给 B 客户的会话、把「还没批准的退款」说成「已退款」这类**事实性越界**，
关键词查不出来 —— 那是 LLM 判定该做的事。

⚠️ 本模块**不做**的事：不重写回答（拦下来就是拦下来，改写的后果更难预测）、
不记录被拦的原文进正文日志（只记规则名与长度，否则日志本身成了泄漏通道）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from app.agents.agent_llm import JsonLLM, build_system_prompt, generate_json
from app.agents.state import GraphState, is_customer_path, last_user_message
from app.security.guardrail import PromptInjectionError, assert_no_injection

logger = logging.getLogger(__name__)

#: 两个阶段（与 `7.agents_module.md` §4 第 2 条的「router 之后、synthesis 之后」一一对应）
INPUT_STAGE: Final[str] = "input"
OUTPUT_STAGE: Final[str] = "output"

#: 被拦时给用户的落地话术。**固定文本**而不是让模型现编：
#: 被拦的那一刻正是「不能让模型自由措辞」的时刻，一句不透露判定依据的礼貌话最安全。
BLOCKED_CUSTOMER_MESSAGE: Final[str] = (
    "抱歉，这个请求我无法处理。如果你要查询自己的订单、退款或政策，可以直接换个说法问我。"
)
BLOCKED_INTERNAL_MESSAGE: Final[str] = (
    "该请求被合规护栏拦下，未继续处理。若是正常的内部查询被误拦，请联系管理员并附上时间点。"
)

#: 输出侧的阻断词：**实现细节**层面的内部口径（对客路径上出现即拦）。
#:
#: ⚠️ 刻意**不收**业务词（订单/退款/工单/金额）：那些正是对客回答必须出现的词，
#: 收进来等于把正常回答全部拦掉。这里守的是「孩子不该看到的实现」——
#: 列名、表名、SQL、审计 id、权限点、节点名、引擎名。
#: 业务层面的越界（把别人的订单说给这个客户）**没有固定词表**，
#: 由后面的 LLM 判定负责（见模块 docstring 的分工表）。
INTERNAL_TERMS: Final[tuple[str, ...]] = (
    "user_id",
    "order_id",
    "refund_id",
    "ticket_id",
    "audit_id",
    "audit id",
    "审计 id",
    "idempotency_key",
    "select ",
    "insert ",
    "update ",
    "delete from",
    "orders",
    "order_items",
    "refunds",
    "tickets",
    "stock_movements",
    "read:all_orders",
    "read:finance",
    "write:refund",
    "write:ticket",
    "read:own",
    "permission",
    "rbac",
    "guardrail",
    "router_node",
    "data_node",
    "synthesis_node",
    "shopsage_readonly",
    "litellm",
    "prompt",
)

#: PII 形态（未脱敏）：与 `security.masking._VALUE_PATTERNS` 同口径，
#: 但这里只做「发现」不做「替换」——出口出现明文 PII 是缺陷，要修上游而不是悄悄糊掉。
#:
#: ⚠️ **刻意不收「16~19 位纯数字」（银行卡）**：主键 id 在很多系统里就是长数字，
#: 而本仓的订单号/工单号是自增 bigint —— 收这条会把「订单 100000000000000001」判成银行卡，
#: 于是正常回答被拦（误杀）。银行卡明文只可能来自用户粘贴的文本，
#: 它由 `masking` 的值形态兜底在**上游**处理；出口这一层守住「手机号 + 身份证」两条即可。
_PII_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
)

#: 掩码后的手机号（`138****1234`）**不算**泄漏 —— 它是脱敏出口的正常产物。
_MASKED_PHONE: Final[re.Pattern[str]] = re.compile(r"1[3-9]\d\*{4}\d{4}")

OUTPUT_JUDGE_SYSTEM: Final[str] = build_system_prompt(
    "你是回答合规审查员。你只做一件事：判断这段**已经生成好的回答**能不能发给该用户。\n\n"
    "以下情况判 allowed=false（其余一律 true）：\n"
    "1. 泄漏了别人的数据（别人的订单号/金额/手机号，或跨用户的汇总）；\n"
    "2. 把「申请中/未批准」的状态说成已完成（如把 requested 说成已退款）；\n"
    "3. 声称执行了系统并未执行的动作（如「已帮你删除订单」「已改价」）；\n"
    "4. 编造材料里没有的数据（回答里出现的数字在材料里找不到）；\n"
    "5. 给用户操作指令去绕过系统限制（如「你可以直接改数据库」）。\n\n"
    "判断要克制：语气、格式、篇幅都不是合规问题；材料里有的信息照实复述允许。\n"
    "只输出 JSON。",
    output_schema='{"allowed": true|false, "reason": "不超过 40 字；allowed=true 时写 ok"}',
)


def scan_internal_terms(text: str) -> list[str]:
    """扫出回答里出现的内部口径词（返回命中的词，供日志与测试断言）。"""
    lowered = text.lower()
    return [term for term in INTERNAL_TERMS if term in lowered]


def scan_pii(text: str) -> list[str]:
    """扫出回答里**未脱敏**的 PII 形态。

    ⚠️ 先剔除已脱敏的形态再扫：`138****1234` 会被 `\\d{16,19}` 之外的规则放过，
    但「脱敏后的手机号被当成明文」是最容易误杀的一类（脱敏出口天天产出它）。
    """
    stripped = _MASKED_PHONE.sub("", text)
    hits: list[str] = []
    for name, pattern in _PII_PATTERNS:
        if pattern.search(stripped):
            hits.append(name)
    return hits


def output_violations(text: str, *, is_customer: bool) -> list[str]:
    """输出侧的确定性检查：返回违规项（空列表 = 通过）。"""
    violations: list[str] = []
    if is_customer:
        terms = scan_internal_terms(text)
        if terms:
            violations.append(f"internal_terms:{','.join(terms)}")
    pii = scan_pii(text)
    if pii:
        violations.append(f"unmasked_pii:{','.join(pii)}")
    return violations


def _blocked(state: GraphState, *, stage: str, rules: list[str], detail: str) -> dict[str, Any]:
    """统一的「拦下」返回：写 `blocked` + 规则 + 落地话术。"""
    is_customer = is_customer_path(state)
    message = BLOCKED_CUSTOMER_MESSAGE if is_customer else BLOCKED_INTERNAL_MESSAGE
    logger.warning(
        "guardrail 拦下请求：stage=%s rules=%s detail=%s",
        stage,
        rules,
        detail,
    )
    return {
        "blocked": True,
        "guardrail_stage": stage,
        "guardrail_rules": rules,
        "guardrail_message": message,
        "answer": message if stage == OUTPUT_STAGE else state.get("answer", ""),
    }


async def guardrail_node(
    state: GraphState,
    *,
    stage: str = INPUT_STAGE,
    llm: JsonLLM | None = None,
) -> dict[str, Any]:
    """图节点：`stage="input"` 查用户输入，`stage="output"` 查最终回答。

    传入：`stage` 与 state（`messages` 取问题；`answer` 取待审回答）。
    返回：通过时 `{"blocked": False, "guardrail_stage": stage, "guardrail_rules": []}`；
          拦下时额外带 `guardrail_message`（输出侧还会覆盖 `answer`）。

    为什么**通过时也要返回**（而不是空 dict）：第 8 步的条件边要靠 `blocked` 判断分支，
    「键不存在」与「键为 False」在条件边里都要处理的话，图里就多了一处隐式约定。
    显式写 False 更啰嗦，但它是可断言的。
    """
    if stage == INPUT_STAGE:
        text = last_user_message(state)
        try:
            scan = assert_no_injection(text)
        except PromptInjectionError as exc:
            rules = list(exc.rules) or ["prompt_injection"]
            return _blocked(
                state,
                stage=stage,
                rules=rules,
                detail=f"命中规则 {exc.rules}；片段长度 {len(exc.snippet)}",
            )
        # 命中但未达拒绝线的场景（如超长）在上面已抛；这里只记「扫过了」的证据。
        logger.info("guardrail 输入侧通过：规则命中 %s", list(scan.matched_rules))
        return {"blocked": False, "guardrail_stage": stage, "guardrail_rules": []}

    if stage != OUTPUT_STAGE:
        raise ValueError(
            f"未知的护栏阶段 {stage!r}：只能是 {INPUT_STAGE!r} 或 {OUTPUT_STAGE!r}"
            "（输入侧挂在 router 之前，输出侧挂在 synthesis 之后）"
        )

    answer = str(state.get("answer") or "")
    if not answer.strip():
        # 没有回答可查：这不是「通过」，是接线漏了（synthesis 没写 answer）。
        # 报错而不是放行 —— 放行会让一个空回答直接出网。
        raise ValueError(
            "guardrail 输出侧收到空 answer：请确认 synthesis 节点已执行并写入了 answer"
        )

    violations = output_violations(answer, is_customer=is_customer_path(state))
    if violations:
        return _blocked(
            state, stage=OUTPUT_STAGE, rules=violations, detail=f"回答长度 {len(answer)}"
        )

    verdict = await _judge_with_llm(state, answer, llm=llm)
    if not verdict["allowed"]:
        return _blocked(
            state,
            stage=OUTPUT_STAGE,
            rules=[f"llm_judge:{verdict['reason'][:60]}"],
            detail="LLM 判定不合规",
        )

    logger.info("guardrail 输出侧通过：LLM 判定 reason=%s", verdict["reason"])
    return {"blocked": False, "guardrail_stage": OUTPUT_STAGE, "guardrail_rules": []}


async def _judge_with_llm(
    state: GraphState, answer: str, *, llm: JsonLLM | None
) -> dict[str, Any]:
    """让 LLM 判一次「这段话能不能发」；判不出来时**默认放行但告警**。

    为什么失败方向是「放行」而不是「拦下」：这里已经是最后一环，
    判不出来时拦下等于「模型一抖动用户就什么都问不到」；而确定性检查
    （阻断词 + PII）已经在它之前跑过，最关键的那两类泄漏不会因为放行而漏出去。
    ⚠️ 这是**有意的取舍**，写在 docstring 里而不是留在代码里让人猜。
    """
    question = last_user_message(state).strip()
    materials = _materials_digest(state)
    messages = [
        {"role": "system", "content": OUTPUT_JUDGE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"用户问题：{question}\n\n"
                f"材料摘要（判断「有没有编造/越界」的依据）：\n{materials}\n\n"
                f"待审回答：\n{answer}"
            ),
        },
    ]
    try:
        payload = await generate_json(
            messages, llm=llm, agent="guardrail", temperature=0.0, max_tokens=200
        )
    except Exception as exc:  # noqa: BLE001 - 审查环节的失败不该把用户请求打死
        logger.warning("guardrail 的 LLM 判定失败，按放行处置（确定性检查已先行）：%s", exc)
        return {"allowed": True, "reason": f"judge_unavailable:{type(exc).__name__}"}

    allowed = payload.get("allowed")
    if not isinstance(allowed, bool):
        logger.warning("guardrail 的 LLM 判定没有给出布尔 allowed，按放行处置：%r", allowed)
        return {"allowed": True, "reason": "judge_ambiguous"}
    return {"allowed": allowed, "reason": str(payload.get("reason", ""))[:80]}


def _materials_digest(state: GraphState) -> str:
    """把「本次真的拿到了什么」浓缩成几行，供判定者核对有没有编造。

    只给**计数与答案本身的来源**，不给原始数据：审查者不需要看到客户的完整地址
    才能判断有没有编造，而把原始数据再喂一次等于把泄漏面扩大一倍。
    """
    rows = list(state.get("rows") or [])
    docs = list(state.get("docs") or [])
    analysis = state.get("analysis") or {}
    lines = [
        f"- 业务数据：{len(rows)} 行",
        f"- 知识库片段：{len(docs)} 条（出处：{', '.join(str(getattr(d, 'source', '')) for d in docs[:5]) or '无'}）",
        f"- 分析结论：{'有' if analysis.get('headline') else '无'}",
        f"- 未拿到的材料：{'；'.join(str(r) for r in (state.get('refusals') or [])) or '无'}",
    ]
    return "\n".join(lines)


__all__ = [
    "BLOCKED_CUSTOMER_MESSAGE",
    "BLOCKED_INTERNAL_MESSAGE",
    "INPUT_STAGE",
    "INTERNAL_TERMS",
    "OUTPUT_JUDGE_SYSTEM",
    "OUTPUT_STAGE",
    "guardrail_node",
    "output_violations",
    "scan_internal_terms",
    "scan_pii",
]
