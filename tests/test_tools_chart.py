"""`chart` 的测试：**纯函数、零依赖、输出一定可 JSON 化**。

图表工具的价值全在「解耦」上（后端不出图、前端自己渲染），所以它的契约特别值得钉死：

1. 输出必须能 `json.dumps`（数据库给的 `Decimal`/`datetime` 就是在这条路上炸的）；
2. 载荷必须**最小**（只带声明的列，别人家的 PII 不许混进图里）；
3. 「参数写错」要抛错、「没有数据」要给空图 + 警告 —— 两者混淆会让 agent 无从判断该说什么。
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from app.tools.audit import audit_records
from app.tools.chart import (
    ABSOLUTE_MAX_POINTS,
    CHART_SCHEMA,
    CHART_TYPES,
    DEFAULT_MAX_POINTS,
    build_chart_spec,
)
from app.tools.errors import ToolContractError

CALLER = "test"


def _rows() -> list[dict]:
    """典型查询结果：分类 + 金额（`Decimal`）+ 时间（`datetime`）+ 一列 PII。"""
    return [
        {"category": "手机数码", "revenue": Decimal("1299.00"), "at": dt.datetime(2026, 9, 1, 12, 0), "phone": "13812341234"},
        {"category": "家用电器", "revenue": Decimal("899.50"), "at": dt.datetime(2026, 9, 2, 12, 0), "phone": "13900001111"},
        {"category": "图书文娱", "revenue": Decimal("59.90"), "at": dt.datetime(2026, 9, 3, 12, 0), "phone": "13700002222"},
    ]


def test_spec_is_json_serializable_with_db_types():
    """`Decimal` / `datetime` 必须被转成 JSON 原生类型（实测：直接 dumps 会 TypeError）。

    ⚠️ 两个转换只在**被声明的列**上发生：`at` 这一列要显式画进图里才会被 ISO 化
    （载荷最小化是另一条规矩，见 `test_payload_carries_only_declared_columns`）。
    """
    spec = build_chart_spec(_rows(), x="at", y="revenue", chart_type="line", caller=CALLER)
    payload = json.dumps(spec, ensure_ascii=False)  # 不抛错即通过
    assert "2026-09-01T12:00:00" in payload  # 时间转 ISO-8601
    assert spec["data"][0]["revenue"] == 1299.0  # Decimal → float（图表只要比例关系）


def test_spec_shape_is_stable_and_versioned():
    """spec 的字段名是前端契约：`schema` 版本号 + type/x/series/data/meta 一样都不能少。"""
    spec = build_chart_spec(_rows(), x="category", y="revenue", caller=CALLER)
    assert spec["schema"] == CHART_SCHEMA
    assert spec["type"] == "bar"
    assert spec["x"] == {"field": "category", "label": "category"}
    assert spec["series"] == [{"name": "revenue", "field": "revenue", "type": "bar"}]
    assert [point["category"] for point in spec["data"]] == ["手机数码", "家用电器", "图书文娱"]
    assert spec["meta"]["points"] == 3 and spec["meta"]["truncated_points"] is False


def test_payload_carries_only_declared_columns():
    """载荷最小化：没被声明要画的列（这里是 `phone`）不进 spec —— 图表也是数据出口。"""
    spec = build_chart_spec(_rows(), x="category", y="revenue", caller=CALLER)
    assert all(set(point) == {"category", "revenue"} for point in spec["data"])
    assert "13812341234" not in json.dumps(spec)


def test_unknown_chart_type_is_rejected():
    """类型白名单：前端渲染分支有限，未知类型必须早炸（而不是前端画不出来）。"""
    with pytest.raises(ToolContractError, match="图表类型"):
        build_chart_spec(_rows(), x="category", y="revenue", chart_type="donut3d", caller=CALLER)


def test_unknown_column_lists_available_columns():
    """列名写错：报错里要带**可用列**，agent 才能自己纠正（第 7 步的报错回喂靠这个）。"""
    with pytest.raises(ToolContractError) as excinfo:
        build_chart_spec(_rows(), x="categoy", y="revenue", caller=CALLER)
    assert "categoy" in str(excinfo.value)
    assert "category" in str(excinfo.value)  # 可用列列表


def test_y_and_series_are_mutually_exclusive():
    """`y`（单系列）与 `series`（多系列）二选一：都传或都不传都是调用方的错。"""
    with pytest.raises(ToolContractError, match="只能传一个"):
        build_chart_spec(_rows(), x="category", y="revenue", series=["revenue"], caller=CALLER)
    with pytest.raises(ToolContractError, match="必须指定"):
        build_chart_spec(_rows(), x="category", caller=CALLER)


def test_multi_series():
    """多系列：一个 X、多列数值，系列顺序与入参一致（前端按顺序配色）。"""
    rows = [
        {"day": "2026-09-01", "paid": 3, "refunded": 1},
        {"day": "2026-09-02", "paid": 5, "refunded": 0},
    ]
    spec = build_chart_spec(rows, x="day", series=["paid", "refunded"], chart_type="line", caller=CALLER)
    assert [s["field"] for s in spec["series"]] == ["paid", "refunded"]
    assert spec["data"][1] == {"day": "2026-09-02", "paid": 5.0, "refunded": 0.0}


def test_pie_requires_exactly_one_series():
    """饼图只能有一个系列：两个系列的饼图没有公认语义，宁可直接拒绝。"""
    rows = [{"k": "a", "v1": 1, "v2": 2}]
    with pytest.raises(ToolContractError, match="饼图"):
        build_chart_spec(rows, x="k", series=["v1", "v2"], chart_type="pie", caller=CALLER)
    spec = build_chart_spec(rows, x="k", y="v1", chart_type="pie", caller=CALLER)
    assert spec["type"] == "pie"


def test_non_numeric_series_value_is_rejected():
    """非数值的系列列直接拒绝：把 `'N/A'` 悄悄当 0 画出一条假曲线，比报错危险得多。"""
    rows = [{"k": "a", "v": "12"}, {"k": "b", "v": "N/A"}]
    with pytest.raises(ToolContractError, match="必须是数值"):
        build_chart_spec(rows, x="k", y="v", caller=CALLER)
    assert build_chart_spec(rows[:1], x="k", y="v", caller=CALLER)["data"][0]["v"] == 12.0


def test_null_values_are_kept_and_counted():
    """NULL 是数据事实，不是错误：原样保留并计入 `null_values` + 警告（不做 0 填充）。"""
    rows = [{"k": "a", "v": 3}, {"k": "b", "v": None}]
    spec = build_chart_spec(rows, x="k", y="v", caller=CALLER)
    assert spec["data"][1]["v"] is None
    assert spec["meta"]["null_values"] == 1
    assert any("空值" in w for w in spec["meta"]["warnings"])


def test_empty_rows_return_empty_spec_with_warning():
    """**没有数据 ≠ 参数写错**：空数据返回空图 + 警告，调用方据此回答「没有可展示的数据」。"""
    spec = build_chart_spec([], x="category", y="revenue", caller=CALLER)
    assert spec["data"] == [] and spec["meta"]["points"] == 0
    assert any("没有数据行" in w for w in spec["meta"]["warnings"])


def test_max_points_is_capped_and_reported():
    """点数上限：既不能超过绝对上限，超出的行也必须**说出来**（不要静默丢数据）。"""
    rows = [{"k": f"k{i}", "v": i} for i in range(300)]
    spec = build_chart_spec(rows, x="k", y="v", max_points=10_000, caller=CALLER)
    assert spec["meta"]["points"] == ABSOLUTE_MAX_POINTS
    assert spec["meta"]["truncated_points"] is True
    assert any("超过单图上限" in w for w in spec["meta"]["warnings"])

    default_spec = build_chart_spec(rows, x="k", y="v", caller=CALLER)
    assert default_spec["meta"]["points"] == DEFAULT_MAX_POINTS
    with pytest.raises(ToolContractError):
        build_chart_spec(rows, x="k", y="v", max_points=0, caller=CALLER)


def test_sort_by_supports_top_n():
    """排序：`sort_by='v'` + `descending=True` 就是「Top N」的写法（画图前先把顺序定好）。"""
    rows = [{"k": "a", "v": 1}, {"k": "c", "v": 9}, {"k": "b", "v": 5}]
    spec = build_chart_spec(rows, x="k", y="v", sort_by="v", descending=True, caller=CALLER)
    assert [point["k"] for point in spec["data"]] == ["c", "b", "a"]
    assert spec["meta"]["sorted_by"] == "v" and spec["meta"]["descending"] is True

    with pytest.raises(ToolContractError, match="sort_by"):
        build_chart_spec(rows, x="k", y="v", sort_by="nope", caller=CALLER)


def test_title_is_defaulted_sanitized_and_bounded():
    """标题来自 LLM：默认值要合理，且必须去掉换行、限长（不能把整段 prompt 塞进标题）。"""
    spec = build_chart_spec(_rows(), x="category", y="revenue", caller=CALLER)
    assert spec["title"] == "revenue · category"

    long_title = "很长的标题\n" * 100
    spec = build_chart_spec(_rows(), x="category", y="revenue", title=long_title, caller=CALLER)
    assert "\n" not in spec["title"] and len(spec["title"]) <= 121


def test_rows_accepts_single_row_and_rejects_non_mappings():
    """入参宽容但明确：单行 `dict` 可以，`list[str]` 不行（报错要指出是第几行的问题）。"""
    assert build_chart_spec({"k": "a", "v": 1}, x="k", y="v", caller=CALLER)["meta"]["points"] == 1
    with pytest.raises(ToolContractError, match=r"rows\[1\]"):
        build_chart_spec([{"k": "a", "v": 1}, "oops"], x="k", y="v", caller=CALLER)


def test_chart_calls_are_audited(caplog):
    """图表调用同样进审计（B1 埋点之前，审计是唯一能回答「谁在什么数据上画了什么」的地方）。"""
    import logging

    with caplog.at_level(logging.INFO, logger="app.tools.audit"):
        spec = build_chart_spec(_rows(), x="category", y="revenue", caller="agent:analysis")

    record = audit_records(caplog.records)[-1]
    assert record["tool"] == "build_chart"
    assert record["caller"] == "agent:analysis"
    assert record["rows"] == 3
    assert record["details"]["chart_type"] == "bar"
    assert spec["meta"]["audit_id"] == record["audit_id"]


def test_all_documented_chart_types_are_supported():
    """文档里承诺的类型必须都能出 spec（防止白名单与实现漂移）。"""
    assert set(CHART_TYPES) == {"bar", "line", "pie", "scatter", "table"}
    for chart_type in CHART_TYPES:
        spec = build_chart_spec(_rows(), x="category", y="revenue", chart_type=chart_type, caller=CALLER)
        assert spec["type"] == chart_type
