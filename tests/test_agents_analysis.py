"""Analysis Agent 的用例：权限闸门、图表取舍、结论结构（`7.agents_module.md` §6 第二批第 6 条）。

本文件全部离线（不碰库）：`analysis_node` 消费的是 `state["rows"]`，
而那正是 data 节点的产物 —— 用构造的行来测它，比连库测更准确地锁住**本节点**的责任。
真库端到端（data → 真查询 → analysis 出图）在 `tests/test_agents_data.py` 与本书 §4.4 的手工验证里。
"""

from __future__ import annotations

import logging

from app.agents.analysis import (
    MAX_ANALYSIS_ROWS,
    MIN_CHART_POINTS,
    analysis_node,
    build_analysis_messages,
)
from tests.agents_helpers import FakeLLM, internal_state, rows_of, state_of

SALES_ROWS = [
    {"month": "2026-07", "amount": 100},
    {"month": "2026-08", "amount": 120},
    {"month": "2026-09", "amount": 90},
    {"month": "2026-10", "amount": 150},
]

REPLY = {
    "headline": "10 月销售额 150 元，为四个月最高",
    "bullets": ["7→8 月 +20%", "9 月回落至 90 元", "10 月回到 150 元"],
    "caveats": ["仅 4 个月样本"],
}


def _state_with_rows(rows: list[dict], **extra):  # noqa: ANN202
    state = internal_state("最近几个月的销售额趋势", **extra)
    state["rows"] = rows
    return state


async def test_analysis_refuses_customer_path() -> None:
    """对客请求**不许**走 analysis（它要读全站数据）：落 refusals 而不是 500。"""
    state = state_of("我的订单趋势", user_id=1)
    state["rows"] = SALES_ROWS

    out = await analysis_node(state, llm=FakeLLM(REPLY))

    assert out["analysis"] == {} and out["chart_spec"] is None
    assert "read:all_orders" in out["refusals"][0]
    assert "仅对内部角色开放" in out["refusals"][0]


async def test_analysis_requires_rows() -> None:
    """没有数据行时明确拒绝（**不自己生成 SQL** —— 那会出现第二处 SQL 生成逻辑）。"""
    out = await analysis_node(internal_state("销售额趋势"), llm=FakeLLM(REPLY))
    assert out["analysis"] == {} and out["chart_spec"] is None
    assert "没有可分析的数据行" in out["refusals"][0]
    assert "data 节点" in out["refusals"][0]


async def test_analysis_builds_valid_chart_spec() -> None:
    """有「分类列 + 数值列」时自动出图，且 spec 是图表工具的正式契约。"""
    out = await analysis_node(_state_with_rows(SALES_ROWS), llm=FakeLLM(REPLY))

    spec = out["chart_spec"]
    assert spec is not None
    assert spec["schema"] == "shopsage.chart/v1"
    assert spec["type"] == "bar"
    assert spec["x"]["field"] == "month"
    assert spec["series"][0]["field"] == "amount"
    assert spec["meta"]["points"] == len(SALES_ROWS)
    assert spec["meta"]["caller"] == "agent:analysis"


async def test_analysis_returns_structured_conclusions() -> None:
    out = await analysis_node(_state_with_rows(SALES_ROWS), llm=FakeLLM(REPLY))
    analysis = out["analysis"]

    assert analysis["headline"] == REPLY["headline"]
    assert analysis["bullets"] == REPLY["bullets"]
    assert analysis["row_count"] == len(SALES_ROWS)
    assert analysis["chart"] == "bar"
    assert "仅 4 个月样本" in analysis["caveats"]


async def test_analysis_skips_chart_when_shape_is_unsuitable() -> None:
    """数据形状不合适时**不出图**并把原因记进 caveats，而不是出错。"""
    rows = rows_of(
        [("a", "x"), ("b", "y"), ("c", "z")],  # 全是文本列：没有数值列可供画
        ("name", "note"),
    )
    out = await analysis_node(_state_with_rows(rows), llm=FakeLLM(REPLY))

    assert out["chart_spec"] is None
    assert any("不适合出图" in caveat for caveat in out["analysis"]["caveats"])


async def test_analysis_skips_chart_for_too_few_points() -> None:
    """只有 1~2 行时不出图：那种图会让读者以为「只有这一项」。"""
    rows = SALES_ROWS[: MIN_CHART_POINTS - 1]
    out = await analysis_node(_state_with_rows(rows), llm=FakeLLM(REPLY))
    assert out["chart_spec"] is None
    assert "不适合出图" in " ".join(out["analysis"]["caveats"])


async def test_analysis_uses_llm_numbers_only_from_rows() -> None:
    """prompt 里必须带上真实的行数据 —— 否则「解读」就只能靠模型编。"""
    llm = FakeLLM(REPLY)
    await analysis_node(_state_with_rows(SALES_ROWS), llm=llm)

    prompt = llm.last_prompt()
    assert "2026-07" in prompt and "150" in prompt
    assert "内部问题：最近几个月的销售额趋势" in prompt


async def test_analysis_mentions_chart_to_the_model() -> None:
    """出图后要告诉模型「已经有一张图」，否则它不知道可以引用。"""
    llm = FakeLLM(REPLY)
    await analysis_node(_state_with_rows(SALES_ROWS), llm=llm)
    assert "已生成的图表：bar 图" in llm.last_prompt()


async def test_analysis_truncates_rows_beyond_cap(caplog) -> None:
    """超过 `MAX_ANALYSIS_ROWS` 时截断并告警（上限来自工具的绝对上限，不是自创数字）。"""
    rows = [{"month": f"m{i}", "amount": i} for i in range(MAX_ANALYSIS_ROWS + 5)]
    with caplog.at_level(logging.WARNING, logger="app.agents.analysis"):
        out = await analysis_node(_state_with_rows(rows), llm=FakeLLM(REPLY))

    assert out["analysis"]["row_count"] == MAX_ANALYSIS_ROWS
    assert any("超过上限" in r.getMessage() for r in caplog.records)


async def test_analysis_decimal_columns_are_chartable() -> None:
    """金额列是 `Decimal`（驱动原样返回）—— 它也要能当数值列画图。"""
    from decimal import Decimal

    rows = [
        {"status": "paid", "total": Decimal("10.00")},
        {"status": "shipped", "total": Decimal("20.00")},
        {"status": "delivered", "total": Decimal("30.00")},
    ]
    out = await analysis_node(_state_with_rows(rows), llm=FakeLLM(REPLY))
    assert out["chart_spec"] is not None
    assert out["chart_spec"]["series"][0]["field"] == "total"


async def test_analysis_bullets_are_normalized() -> None:
    """模型给的要点形态可能不规范（单个字符串/含非字符串项）→ 归一成字符串列表。"""
    out = await analysis_node(
        _state_with_rows(SALES_ROWS), llm=FakeLLM({"headline": "h", "bullets": "只有一条", "caveats": None})
    )
    assert out["analysis"]["bullets"] == ["只有一条"]
    assert out["analysis"]["caveats"] == []


def test_build_analysis_messages_includes_row_count() -> None:
    messages = build_analysis_messages("问题", SALES_ROWS)
    assert messages[0]["role"] == "system"
    assert "查询结果（4 行" in messages[1]["content"]
    assert '"headline"' in messages[0]["content"]


async def test_analysis_marks_boolean_as_non_numeric() -> None:
    """布尔列不算数值列：把它画成 0/1 的条形图是一条**看起来合理**的假数据。"""
    rows = [
        {"name": "a", "flag": True},
        {"name": "b", "flag": False},
        {"name": "c", "flag": True},
    ]
    out = await analysis_node(_state_with_rows(rows), llm=FakeLLM(REPLY))
    assert out["chart_spec"] is None


def test_analysis_module_has_no_write_or_sql_generation_path() -> None:
    """分析节点只读、只消费 state：模块里不该出现写路径与 SQL 文本生成。"""
    import inspect

    from app.agents import analysis as analysis_mod

    source = inspect.getsource(analysis_mod)
    for forbidden in ("create_ticket", "write_session", "SessionLocal", "INSERT INTO"):
        assert forbidden not in source, f"analysis 模块里不该出现 {forbidden}"
