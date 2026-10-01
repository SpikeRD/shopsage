"""Synthesis Agent 的用例：三套模板、材料边界、引用映射（`7.agents_module.md` §7 第五条）。

对应文档用例：
    test_synthesis_customer_tone()   # 对客回答短、无内部术语
"""

from __future__ import annotations

import pytest

from app.agents.synthesis import (
    CUSTOMER_MAX_CHARS,
    build_synthesis_messages,
    resolve_citations,
    role_template,
    synthesis_node,
)
from app.security.rbac import PERM_READ_FINANCE
from app.tools.retriever import RetrievedDoc
from tests.agents_helpers import FakeLLM, internal_state, rows_of, state_of

ANSWER = "您最近一笔订单已发货，预计明天送达。"


def _docs() -> list[RetrievedDoc]:
    return [
        RetrievedDoc(text="七天无理由退货，自签收次日起算。", source="退货政策.md", chunk_id="c12", page=2, score=0.88),
        RetrievedDoc(text="运费由买家承担。", source="FAQ.md", chunk_id="c3", score=0.42),
    ]


# ── 模板选择 ─────────────────────────────────────────────────────────────────


def test_role_template_priorities() -> None:
    """`read:finance` > `read:all_orders` > 对客；未知角色落最保守的对客模板。"""
    assert role_template("customer")[0] == "customer"
    assert role_template("internal_employee")[0] == "internal"
    assert role_template("finance_auditor")[0] == "finance"
    assert role_template("finance_auditor", frozenset({PERM_READ_FINANCE}))[0] == "finance"
    # 未登记角色：既不是内部也不是财务 → 对客模板（默认最保守）
    assert role_template("who-knows")[0] == "customer"


def test_customer_template_has_length_and_tone_constraints() -> None:
    """对客模板的篇幅约束是**可断言的数字**，不是「尽量简短」。"""
    _, system = role_template("customer")
    assert str(CUSTOMER_MAX_CHARS) in system
    for forbidden in ("内部术语", "SQL", "权限"):
        assert forbidden in system  # 模板里明确列出「不许提」的东西
    assert CUSTOMER_MAX_CHARS == 300


# ── 材料边界（防编造的机制，而不是请求它别编）────────────────────────────────


def test_prompt_tells_model_when_there_is_no_material() -> None:
    """材料为空时 prompt 必须**明说**「无」，否则模型会顺着问题编下去。"""
    state = state_of("我的订单到哪了")
    prompt = build_synthesis_messages(state)[-1]["content"]

    assert "业务数据：无" in prompt
    assert "知识库片段：无" in prompt
    assert "这是你唯一的信息来源" in prompt
    assert "材料里没有的内容不要推测" in prompt


def test_prompt_lists_refusals_as_first_class_material() -> None:
    """`refusals` 要作为「没拿到的材料」原样进 prompt（不许编造替代内容）。"""
    state = state_of("我的订单到哪了", refusals=["知识库检索暂不可用：app.rag.retriever 不存在"])
    prompt = build_synthesis_messages(state)[-1]["content"]

    assert "本次**没有拿到**的材料" in prompt
    assert "app.rag.retriever 不存在" in prompt
    assert "不许编造替代内容" in prompt


def test_prompt_marks_truncated_results() -> None:
    """结果被截断必须在材料里写出来（否则模型会把部分数据当全量讲）。"""
    state = internal_state("本月销售额")
    state["rows"] = rows_of([(1, 10)], ("order_id", "amount"))
    state["row_meta"] = {"truncated": True, "rows_cap": 1}
    prompt = build_synthesis_messages(state)[-1]["content"]

    assert "**被截断**" in prompt
    assert "这只是部分数据" in prompt


def test_customer_prompt_hides_internal_details() -> None:
    """对客材料里**不出现** SQL 与执行元信息（两套模板之外的第二层保护）。"""
    shared = {
        "sql": "SELECT order_id FROM orders WHERE user_id = :uid",
        "row_meta": {"audit_id": "tc-deadbeef", "duration_ms": 42},
        "rows": rows_of([(1, "paid")], ("order_id", "status")),
    }
    customer_prompt = build_synthesis_messages(state_of("我的订单", user_id=1, **shared))[-1]["content"]
    assert "SELECT order_id" not in customer_prompt
    assert "tc-deadbeef" not in customer_prompt

    internal_prompt = build_synthesis_messages(internal_state("订单情况", **shared))[-1]["content"]
    assert "SELECT order_id" in internal_prompt  # 内部可以看到查询方式
    assert "tc-deadbeef" in internal_prompt


def test_prompt_numbers_docs_for_citation() -> None:
    state = state_of("退货政策是什么")
    state["docs"] = _docs()
    prompt = build_synthesis_messages(state)[-1]["content"]

    assert "[1]（出处：退货政策.md p.2 #c12）" in prompt
    assert "[2]（出处：FAQ.md #c3）" in prompt
    assert "写 `[编号]`" in prompt


# ── 引用映射 ─────────────────────────────────────────────────────────────────


def test_resolve_citations_maps_numbers_to_sources() -> None:
    index = [{"number": 1, "citation": "退货政策.md p.2 #c12"}, {"number": 2, "citation": "FAQ.md #c3"}]
    assert resolve_citations([1, 2], index) == ["退货政策.md p.2 #c12", "FAQ.md #c3"]
    assert resolve_citations([2, 1, 2], index) == ["FAQ.md #c3", "退货政策.md p.2 #c12"]  # 去重


def test_resolve_citations_drops_out_of_range_and_junk() -> None:
    """越界/非法/布尔编号一律丢弃：`True` 不能静默变成「引用了第 1 条」。"""
    index = [{"number": 1, "citation": "A.md"}]
    assert resolve_citations([0, 5, -1, "x", None, True, 1], index) == ["A.md"]


# ── 节点行为 ─────────────────────────────────────────────────────────────────


async def test_synthesis_customer_tone() -> None:
    """文档用例：对客回答短、带出处引用，且**没有**内部术语。

    注意这条用例断的不是「模型说了什么」（那是假 LLM 定的），
    而是**返回的东西满足出口要求**：长度、引用映射、模板选择。
    """
    state = state_of("退货政策是什么？", user_id=1)
    state["docs"] = _docs()
    llm = FakeLLM({"answer": ANSWER, "cited": [1]})

    out = await synthesis_node(state, llm=llm)

    assert out["answer"] == ANSWER
    assert len(out["answer"]) <= CUSTOMER_MAX_CHARS
    assert out["citations"] == ["退货政策.md p.2 #c12"]
    # 模板选择正确：system 里带对客的篇幅约束
    assert str(CUSTOMER_MAX_CHARS) in llm.calls[0][0]["content"]


async def test_synthesis_internal_template_and_details() -> None:
    state = internal_state("近三个月销售趋势", user_id=9)
    state["rows"] = rows_of([("2026-07", 100), ("2026-08", 120)], ("month", "amount"))
    llm = FakeLLM({"answer": "7 月 100 元、8 月 120 元，环比 +20%。", "cited": []})

    out = await synthesis_node(state, llm=llm)

    assert out["citations"] == []
    assert "内部" in llm.calls[0][0]["content"]
    assert "2026-07" in llm.last_prompt()


async def test_synthesis_empty_answer_raises() -> None:
    """空回答是契约失败：报错（由第 8 步走兜底出口），而不是返回空白页。"""
    with pytest.raises(ValueError, match="空 answer"):
        await synthesis_node(state_of("我的订单"), llm=FakeLLM({"answer": "   "}))


async def test_synthesis_citations_empty_when_model_cites_nothing() -> None:
    state = state_of("我的订单", user_id=1)
    state["rows"] = rows_of([(1, "paid")], ("order_id", "status"))
    out = await synthesis_node(state, llm=FakeLLM({"answer": "你有一笔已支付订单。", "cited": []}))
    assert out["citations"] == []
    assert out["answer"] == "你有一笔已支付订单。"
