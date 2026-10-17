"""Synthesis Agent：**按角色合成最终回答**（`7.agents_module.md` §4 第 1 条、§5 第四条）。

一句话职责：把前面节点产出的**结构化产物**（rows / docs / analysis / refusals）
翻译成一段**给这个人看**的话。它自己**不取数、不检索、不判断权限**。

合成 Agent 的四个真实风险（本模块的每一处代码都在对付其中之一）
------------------------------------------------------------

1. **编造**：上下文里没有的数据被「顺着答下去」。
   对策不是「在 prompt 里请求它别编」，而是**把材料边界写进 prompt**：
   查询 0 行就写「查询结果是 0 行」、检索为空就写「知识库没有返回任何片段」、
   有 refusals 就原样列出来。模型编造往往不是因为它想撒谎，
   而是因为**没人告诉它「这里没有材料」**。
2. **越权叙述**：把内部口径（权限、审计 id、SQL、engine 名）说给客户。
   对策是**两套 system prompt + 材料过滤**：对客模板里根本不给 SQL 与内部字段。
3. **引用漂移**：回答里复述文档名时会改写（「退货政策.md」→「退货政策文档」）。
   对策是让模型只写**编号**（`[1]`），由程序映射回 `RetrievedDoc.citation()`。
4. **对客话术过长**：客户要的是结论，不是分析报告。
   对策是模板里给出硬性的篇幅与口吻约束（并且被测试钉住，见 `test_synthesis_*`）。

⚠️ `7.agents_module.md` §4 第 1 条说「每个 Agent 输出结构化 JSON，而非自由文本」。
本节点的产物**是**一段自然语言（回答本来就该是自然语言），但它的输出仍然走结构化
JSON：`{"answer": ..., "cited": [1,3]}`。区别在于「模型被约束在一个字段里写字」，
而不是「它想说什么就说什么」——引用编号能被程序核对，这就是结构化的价值。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.agents.agent_llm import JsonLLM, build_system_prompt, format_rows, generate_json
from app.agents.rag import docs_to_prompt_block, remember_answer
from app.agents.state import GraphState, last_user_message, permissions_of, role_of
from app.security.rbac import (
    PERM_READ_ALL_ORDERS,
    PERM_READ_FINANCE,
    permissions_for,
)

logger = logging.getLogger(__name__)

#: 对客回答的字数上限（`7.agents_module.md` §5：「对客礼貌、简短、口语」）。
#: 定成**可断言的数字**而不是「尽量简短」：第 16 步评测要能量化「换了 prompt 之后是不是更啰嗦」。
CUSTOMER_MAX_CHARS: Final[int] = 300

#: 送进合成 prompt 的结果行数上限（E2）
PROMPT_ROWS: Final[int] = 20

_CUSTOMER_SYSTEM: Final[str] = build_system_prompt(
    "你是电商平台的**客服助手**，正在直接回复客户。\n\n"
    "口吻与篇幅（硬要求）：\n"
    f"1. 口语、礼貌、直接给结论，**总长度不超过 {CUSTOMER_MAX_CHARS} 字**；\n"
    "2. 不复述内部术语：不提权限、审计 id、SQL、表名、行数上限、模型名、英文字段名；\n"
    "3. 只讲这个客户自己的事：材料里出现别人的信息时一律不提；\n"
    "4. 材料里没有的信息，直说「我这边看不到」，**不许推测**；\n"
    "5. 不要罗列数据表的原始行，改写成客户能看懂的一两句话。\n\n"
    "退款相关（材料里出现「已受理的退款申请」时必须遵守）：\n"
    "6. 那是一笔**已经提交、等待审核**的申请 —— 明确说「已提交，等待审核」，"
    "**不许**说「已经退给您了」「钱已到账」（钱还没动）；\n"
    "7. 要给出退款单号与金额，让客户能拿去核对；\n"
    "8. 材料说「没有提交任何退款」时，**不要**说「已为您提交」，只问清是哪一笔订单。\n\n"
    "格式：先给结论，需要时可以给 1~3 条短要点；用 `[1]` 这样的编号引用知识库出处。",
    output_schema='{"answer": "给客户的回答", "cited": [引用的出处编号，整数数组，可为空]}',
)

_INTERNAL_SYSTEM: Final[str] = build_system_prompt(
    "你是电商企业的**内部业务助手**，面向内部员工（客服/运营/主管）回答，"
    "他们需要能核对的细节。\n\n"
    "口吻与篇幅（硬要求）：\n"
    "1. 专业、简洁，允许用表格/要点罗列关键字段；\n"
    "2. 结论先行，数字要带口径（时间范围、样本行数、是否被截断）；\n"
    "3. 材料不足时明确说明缺什么，不推测、不补数据；\n"
    "4. 引用知识库出处时用 `[1]` 这样的编号。",
    output_schema='{"answer": "给内部同事的回答", "cited": [引用的出处编号，整数数组，可为空]}',
)

_FINANCE_SYSTEM: Final[str] = build_system_prompt(
    "你是电商企业的**财务口径助手**，面向财务审计角色回答。\n\n"
    "口吻与篇幅（硬要求）：\n"
    "1. 金额一律带币种与两位小数（材料里的金额是精确十进制值，不要改写成「约」「大概」）；\n"
    "2. 每个金额都要标清口径（订单金额 / 已申请退款 / 剩余可退）；\n"
    "3. 结论先行，随后给可核对的明细要点；数字与材料必须逐一对应；\n"
    "4. 材料不足时明确说明缺什么，不推测；引用出处用 `[1]` 这样的编号。",
    output_schema='{"answer": "给审计角色的回答", "cited": [引用的出处编号，整数数组，可为空]}',
)


def role_template(role: str, permissions: frozenset[str] | None = None) -> tuple[str, str]:
    """选出 (模板名, system prompt)。

    **三级优先**（顺序即口径）：

    1. `read:finance` → 财务口径（金额与口径要求最严，财务审计角色即便同时是内部员工也走这条）；
    2. `read:all_orders` → 内部口径；
    3. 其余 → 对客口径（**默认最保守**：未登记角色、缺权限的调用方都只会看到对客话术）。

    为什么按权限点而不是角色名分支：与 `analysis` 节点同一条理由 ——
    `4.security.md` 已经把「能力」定义成权限点，再写一遍角色白名单就是第二处真相。

    `permissions=None` 时**按角色现查**（`permissions_for`），而不是当成空集：
    否则「只传角色」的调用方（测试、脚本、第 16 步的评测）会把内部员工判成对客，
    症状是「内部功能用了对客话术」—— 一种不会报错、只会让人困惑的降级。
    """
    scope = permissions_for(role) if permissions is None else permissions
    if PERM_READ_FINANCE in scope:
        return "finance", _FINANCE_SYSTEM
    if PERM_READ_ALL_ORDERS in scope:
        return "internal", _INTERNAL_SYSTEM
    return "customer", _CUSTOMER_SYSTEM


def _material_block(
    state: GraphState, *, include_internal_details: bool
) -> tuple[str, list[dict[str, Any]]]:
    """把 state 里的产物渲染成 prompt 的材料块；同时返回「知识库出处编号 → 出处串」表。

    `include_internal_details=False`（对客）时**不渲染 SQL 与执行元信息**：
    这是「两套模板」之外的第二层保护 —— 台词不同还不够，
    材料里根本不该出现内部字段，否则模型一旦顺口复述就泄漏了。
    """
    lines: list[str] = []

    docs = list(state.get("docs") or [])
    if docs:
        lines.append(f"知识库片段（共 {len(docs)} 条，全部带出处）：")
        lines.append(docs_to_prompt_block(docs))
    else:
        lines.append("知识库片段：无（本次没有检索，或检索没有命中/后端暂不可用）。")

    rows = list(state.get("rows") or [])
    meta = state.get("row_meta") or {}
    lines.append("")
    if rows:
        lines.append(f"业务数据（本次查询结果 {len(rows)} 行）：")
        lines.append(format_rows(rows, limit=PROMPT_ROWS))
        if meta.get("truncated"):
            lines.append(
                f"⚠️ 结果**被截断**（上限 {meta.get('rows_cap')} 行）："
                "回答里必须说明「这只是部分数据」，不要当成全部。"
            )
    else:
        lines.append("业务数据：无（本次没有查询，或查询结果为 0 行）。")

    analysis = state.get("analysis") or {}
    if analysis:
        lines.append("")
        lines.append("已完成的内部解读（事实来源同上，可直接引用其数字）：")
        if analysis.get("headline"):
            lines.append(f"结论：{analysis['headline']}")
        for bullet in analysis.get("bullets") or []:
            lines.append(f"- {bullet}")
        for caveat in analysis.get("caveats") or []:
            lines.append(f"（口径提醒：{caveat}）")

    refusals = list(state.get("refusals") or [])
    if refusals:
        lines.append("")
        lines.append("本次**没有拿到**的材料（回答时如实说明，不许编造替代内容）：")
        for reason in refusals:
            lines.append(f"- {reason}")

    # ── 退款申请的结果（变更 003）─────────────────────────────────────────────
    # ⚠️ 用**中文标签**，绝不用 `refund_id` / `refunds` 这类英文字段名：
    # `guardrail.INTERNAL_TERMS` 把它们列为对客禁词（那是「实现细节不许出网」的口径），
    # 模型一旦把字段名照抄进回答，输出侧护栏会**拦下整句回答** ——
    # 表现是「退款明明成功了，客户看到的却是一句拦截话术」。
    # 所以这里给的是「退款单号：12」这种形态，模型照抄也不会撞词表。
    refund = state.get("refund") or {}
    if refund:
        lines.append("")
        status = str(refund.get("status") or "")
        if status == "submitted":
            lines.append(
                "本次**已经受理**的退款申请（这是已发生的事实，必须如实告知客户）："
            )
            lines.append(f"- 退款单号：{refund.get('refund_id')}")
            lines.append(f"- 订单号：{refund.get('order_id')}")
            lines.append(f"- 退款金额：{refund.get('amount')} 元")
            lines.append(
                "- 当前处理状态：已提交，**等待平台审核**（还没到账，不要说「已经退给您了」）"
            )
            if str(refund.get("remaining_after") or "") not in ("", "0.00"):
                lines.append(f"- 该订单剩余可退额度：{refund.get('remaining_after')} 元")
        elif status == "needs_order_id":
            candidates = "、".join(str(item) for item in (refund.get("candidates") or []))
            lines.append("")
            lines.append(
                "客户有多笔可退订单，**本次没有提交任何退款**（需要他先确认是哪一笔）："
            )
            lines.append(f"- 可退订单号：{candidates}")
            lines.append(f"- 处理要求：{refund.get('hint')}")
        else:
            # `refused` 或任何将来新增的取值：把 hint 原样交给模型转述。
            lines.append("")
            lines.append("退款申请没有提交成功，原因如下（如实告知客户，不要粉饰）：")
            lines.append(f"- {refund.get('hint') or '未能提交退款申请'}")

    if include_internal_details:
        if state.get("sql"):
            lines.append("")
            lines.append(f"（内部参考，可简要提及查询方式：执行的 SQL 是 {state['sql']}）")
        if meta:
            lines.append(
                f"（内部参考：执行耗时 {meta.get('duration_ms')} ms，审计 id {meta.get('audit_id')}）"
            )

    index: list[dict[str, Any]] = []
    for number, doc in enumerate(docs, start=1):
        citation = getattr(doc, "citation", None)
        index.append(
            {
                "number": number,
                "citation": str(citation()) if callable(citation) else str(getattr(doc, "source", "")),
            }
        )
    return "\n".join(lines), index


def build_synthesis_messages(state: GraphState) -> list[dict[str, Any]]:
    """拼合成 prompt（纯函数：材料边界与模板选择都能被单独断言）。"""
    role = role_of(state)
    permissions = permissions_of(state)
    template, system = role_template(role, permissions)
    is_customer = template == "customer"
    material, _ = _material_block(state, include_internal_details=not is_customer)
    question = last_user_message(state).strip() or "（未提供原始问题）"

    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"用户问题：{question}\n\n"
                f"可用材料（**这是你唯一的信息来源**）：\n{material}\n\n"
                "要求：只用上面的材料回答；材料里没有的内容不要推测；"
                "引用知识库时写 `[编号]`，编号必须是上面出现过的。"
            ),
        },
    ]


def resolve_citations(cited: Any, index: Sequence[Mapping[str, Any]]) -> list[str]:
    """把模型给的编号映射回出处串（越界/非法编号一律丢弃）。"""

    def _as_int(value: Any) -> int | None:
        # ⚠️ `True` 会隐式转成 1：模型偶尔把「引用了」写成布尔值，
        # 那会静默变成「引用了第 1 条」。所以先显式排掉 bool。
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    numbers = cited if isinstance(cited, (list, tuple)) else []
    found: list[str] = []
    for item in numbers:
        number = _as_int(item)
        if number is None or not (1 <= number <= len(index)):
            continue
        citation = str(index[number - 1]["citation"])
        if citation and citation not in found:
            found.append(citation)
    return found


async def synthesis_node(
    state: GraphState,
    *,
    llm: JsonLLM | None = None,
    cache: Any = None,
) -> dict[str, Any]:
    """图节点：合成最终回答，写 `answer` + `citations`。

    ## 第 13 步：命中缓存时**不打模型**

    `state["cached_answer"]` 非空（`rag_node` 命中语义缓存时写入）就直接返回它。
    这一步是 E1（成本下降）的真正落点：命中时省掉的是
    「embedding + 向量检索 + 关键词检索 + rerank + 一次上千 token 的生成」。

    ⚠️ 这里**只跳过模型调用，不跳过护栏**：输出侧护栏（`guardrail_output`）
    是图上的下一个节点，它对缓存答案一视同仁 —— 否则「缓存成了一个绕过合规审查的后门」
    （而它看起来完全正常：缓存里的答案当初是通过审查的）。
    也就是说：缓存答案要过**两遍**审查（写入那次 + 命中那次），这是刻意的。

    ⚠️ **不在这里塞「抱歉我没答上」的模板兜底**：那属于第 8 步的图级兜底
    （节点异常/被护栏拦下时的出口）。若这里也写一份，就会出现两处「拒答话术」，
    改一处忘一处；更糟的是本节点若在材料为空时自顾自编一段，`refusals` 就白记了。
    """
    cached = str(state.get("cached_answer") or "")
    if cached:
        citations = [str(item) for item in (state.get("citations") or [])]
        logger.info(
            "synthesis 用缓存答案（跳过模型调用）：chars=%d citations=%d outcome=%s",
            len(cached),
            len(citations),
            (state.get("cache_info") or {}).get("outcome"),
        )
        return {
            "answer": cached,
            "citations": citations,
            "cache_info": {**(state.get("cache_info") or {}), "cached_answer_used": True},
        }

    role = role_of(state)
    permissions = permissions_of(state)
    template, _ = role_template(role, permissions)
    _, index = _material_block(state, include_internal_details=template != "customer")

    messages = build_synthesis_messages(state)
    payload = await generate_json(
        messages, llm=llm, agent="synthesis", temperature=0.3, max_tokens=700
    )

    answer = str(payload.get("answer", "")).strip()
    if not answer:
        # 空回答是**契约失败**而不是「没什么可说」：交给上层（第 8 步）走兜底出口，
        # 而不是返回一句空白让用户以为系统卡住了。
        raise ValueError(
            "synthesis 的 LLM 返回了空 answer：请检查 prompt 或 max_tokens"
            "（推理模型会把 token 先花在 reasoning 上，见 runbook §9 坑 4）"
        )

    citations = resolve_citations(payload.get("cited"), index)
    logger.info(
        "synthesis 完成：template=%s chars=%d citations=%d",
        template,
        len(answer),
        len(citations),
    )
    out: dict[str, Any] = {"answer": answer, "citations": citations}
    if cache is not None:
        # 生成之后写缓存（**只有真的从模型拿到回答才写** —— 空回答由上面的 raise 拦住）。
        # ⚠️ 写用的上下文来自 `state["cache_context"]` —— 那是 `rag_node` 在读缓存那一刻
        # 留下的**可序列化字段**（问题串 / scope / 指纹 / 版本 / 分档 / TTL）。
        # 用「当前的 messages」去写是不行的：图会在本节点之后把这一轮追加进历史。
        #
        # ── 「空结果不写缓存」（issue 010 的第 1 条修法，产品已确认）─────────
        # 判据：**这一轮走了知识库分支**（`cache_context` 存在 ⟺ `rag_node` 跑过）
        #       **且一条出处都没有**（`citations == []`）。
        #
        # 为什么不缓存：那种答案的内容是「我没查到」，而它最常见的成因是**瞬时的**
        # —— `rag_chunks` 刚被全量回归清空（runbook §10.1 坑 6）、检索后端抖了一下、
        # 或问法恰好没命中。把它按政策类 TTL 缓存 **7 天**，等于把一次故障固化成答案：
        # 语料后来灌好了，用户问同一句仍然拿到「没有找到相关知识库片段」。
        # 而它**省不下任何成本**（本来就没检索到东西、也没多少 token 可省），
        # 收益近乎为零、代价是一周的错答案 —— 这笔交换不该做。
        #
        # ⚠️ 边界（别扩大）：判据必须带 `cache_context`。退款（`refund`）与寒暄
        # （`final`）这两条路径**不经过 `rag_node`**，`cache_context` 为空，
        # 它们的答案是「系统刚写入的真实事实」或「不需要材料的寒暄」，
        # 虽然同样 `citations == []`，却**必须**照常缓存 —— 只按 `citations`
        # 判空会把它们一起误伤，而症状是「退款结果不再进缓存」这种没人会怀疑缓存的问题。
        writes_cache = bool(state.get("cache_context")) and bool(citations)
        if not writes_cache:
            logger.info(
                "不写缓存：本轮走了知识库但有 0 条出处（或根本没走知识库）"
                "—— 空结果按瞬态处理，见 issue 010。answer_chars=%d",
                len(answer),
            )
            out["cache_info"] = {**(state.get("cache_info") or {}), "write": "skipped:empty_citations"}
        else:
            write = await remember_answer(state, answer, cache=cache, citations=citations)
            if write is not None:
                out["cache_info"] = {**(state.get("cache_info") or {}), "write": write.summary()}
    return out


__all__ = [
    "CUSTOMER_MAX_CHARS",
    "PROMPT_ROWS",
    "build_synthesis_messages",
    "resolve_citations",
    "role_template",
    "synthesis_node",
]
