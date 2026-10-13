"""MCP 四个工具的行为用例：上限夹取、结果形状、JSON 化、契约错误。

这一层测的是「薄壳自己那点逻辑」——它与第 6 步的工具体测试**不重复**：
工具层的用例证明「截断/幂等/只读角色是对的」，这里的用例证明
「MCP 这一层没有把这些能力放大或改写」。

最值钱的一组是**上限夹取**（`_capped`）。MCP 的调用方是一个可能被提示注入影响的模型，
所以「客户端要 100 万行」必须被安静的收住，而收住的证据要能看见：

| 客户端请求 | 服务端上限 | 结果 |
|---|---|---|
| `max_rows=10000` | 100 | 查询按 100 行执行，`meta.rows_cap == 100` |
| `top_k=500` | 10 | 检索按 10 条执行 |
| `max_points=10000` | 50 | 图表最多 50 点 |

⚠️ 三条都**不报错**（请求本身是合理的，只是超了配额）—— 这与「参数写错」要分开：
写错该 400 式拒绝，超配额该夹住并继续（否则模型会因为一个无关紧要的数字而彻底失败）。
"""

from __future__ import annotations

import pytest

from app.mcp import sync_calls
from app.mcp import tools as tool_impl
from app.mcp.settings import McpSettings
from tests.mcp.conftest import tool_json

SQL_OK = "SELECT order_id, status, total_amount FROM orders WHERE user_id = :user_id ORDER BY order_id LIMIT 5"


def _fake_executor(calls: list[dict]):
    """假执行器：记录被夹取后的上限，返回一行带 Decimal 的"真实形状"数据。"""

    def fake(sql, params, *, max_rows, caller):
        calls.append({"sql": sql, "params": dict(params or {}), "max_rows": max_rows, "caller": caller})
        return (
            [{"order_id": 3, "status": "pending", "total_amount": __import__("decimal").Decimal("2745.98")}],
            {
                "audit_id": "tc-fake",
                "row_count": 1,
                "truncated": False,
                "columns": ["order_id", "status", "total_amount"],
                "tables": ["orders"],
                "declared_limit": max_rows,
                "limit_clamped": False,
                "bound_limit_clamped": False,
                "rows_cap": max_rows,
                "duration_ms": 3,
            },
        )

    return fake


# ── 一、上限夹取（MCP 这一层唯一的"判断"）──────────────────────────────────────


async def test_rows_cap_is_clamped_to_the_server_limit(customer_settings, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(sync_calls, "execute_readonly_query", _fake_executor(calls))
    result = await tool_impl.sql_query(SQL_OK, {"user_id": 1}, 10_000, settings=customer_settings)
    assert calls[0]["max_rows"] == customer_settings.max_rows == 100
    assert result["meta"]["rows_cap"] == 100
    assert result["meta"]["row_count"] == 1


async def test_rows_cap_defaults_to_the_server_limit(customer_settings, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(sync_calls, "execute_readonly_query", _fake_executor(calls))
    await tool_impl.sql_query(SQL_OK, {"user_id": 1}, None, settings=customer_settings)
    assert calls[0]["max_rows"] == 100


async def test_rows_cap_can_be_tightened_by_the_client(customer_settings, monkeypatch):
    """只允许变严：客户端要 5 行就拿 5 行（画个小图时这是正常需求）。"""
    calls: list[dict] = []
    monkeypatch.setattr(sync_calls, "execute_readonly_query", _fake_executor(calls))
    await tool_impl.sql_query(SQL_OK, {"user_id": 1}, 5, settings=customer_settings)
    assert calls[0]["max_rows"] == 5


async def test_top_k_is_clamped_and_can_be_tightened(customer_settings, monkeypatch):
    seen: list[int] = []

    def fake_query(query, top_k, *, caller):
        seen.append(top_k)
        return []

    monkeypatch.setattr(sync_calls, "retrieve_docs", fake_query)
    await tool_impl.retrieve_knowledge("退货要多久", 500, settings=customer_settings)
    await tool_impl.retrieve_knowledge("退货要多久", 2, settings=customer_settings)
    assert seen == [10, 2], "500 条要夹到服务端上限 10；2 条要原样放行"


async def test_chart_points_are_clamped(customer_settings):
    rows = [{"k": f"k{i}", "v": i} for i in range(120)]
    spec = await tool_impl.build_chart(rows, "k", y="v", max_points=10_000, settings=customer_settings)
    assert spec["meta"]["points"] == 50  # 服务端上限
    assert spec["meta"]["truncated_points"] is True
    assert spec["meta"]["warnings"], "截断必须说出来（静默截断比报错危险）"


@pytest.mark.parametrize("bad", [0, -1, True, "5", 2.5])
async def test_non_positive_limits_are_contract_errors(bad, customer_settings):
    with pytest.raises(Exception) as caught:
        await tool_impl.sql_query(SQL_OK, {"user_id": 1}, bad, settings=customer_settings)
    assert "[tool_contract]" in str(caught.value)


# ── 二、结果形状与 JSON 化（跨进程出口的唯一真相）──────────────────────────────


async def test_decimal_is_serialised_as_a_string_not_a_float(customer_settings, monkeypatch):
    """★金额过 MCP 边界**不许变 float**：那是精度丢失，而调用方拿它做金额比对。

    实测形状：工具层返回 `Decimal('2745.98')`；MCP 出口必须是 `"2745.98"` 字符串。
    若这里图省事写成 `float()`，`141.10 + 0.20` 那类比较会在客户端上出现
    「看起来一样、算起来不一样」的结果 —— 与第 6 步 chart 的注释是同一条道理。
    """
    monkeypatch.setattr(sync_calls, "execute_readonly_query", _fake_executor([]))
    result = await tool_impl.sql_query(SQL_OK, {"user_id": 1}, None, settings=customer_settings)
    assert result["rows"][0]["total_amount"] == "2745.98"
    assert isinstance(result["rows"][0]["total_amount"], str)
    # 结果整体必须是可 JSON 序列化的（MCP 是跨进程协议）
    import json

    json.dumps(result)


async def test_sql_query_meta_carries_diagnostic_keys_only(customer_settings, monkeypatch):
    """返回的 meta 是**精选**的：行数/截断/上限/落表/耗时/审计 id。

    刻意**不含** `columns`（表结构）与 `declared_limit`（内部口径）：
    协议里每多一个字段就多一个要长期兼容的承诺，而这两个对客户端没有诊断价值。
    """
    monkeypatch.setattr(sync_calls, "execute_readonly_query", _fake_executor([]))
    result = await tool_impl.sql_query(SQL_OK, {"user_id": 1}, None, settings=customer_settings)
    assert set(result["meta"]) == {
        "row_count",
        "truncated",
        "rows_cap",
        "tables",
        "duration_ms",
        "audit_id",
    }
    assert set(result) == {"rows", "meta"}


async def test_retrieve_knowledge_shape_is_citation_ready(customer_settings, monkeypatch):
    from app.tools.retriever import RetrievedDoc

    def fake_query(query, top_k, *, caller):
        return [
            RetrievedDoc(text="第 2 条：运费由买家承担。", source="docs/kb/退货政策.md", chunk_id="c1", score=0.73),
            RetrievedDoc(text="质量问题由本店承担。", source="docs/kb/退货政策.md", page=3, score=0.5),
        ]

    monkeypatch.setattr(sync_calls, "retrieve_docs", fake_query)
    result = await tool_impl.retrieve_knowledge("运费", 5, settings=customer_settings)
    assert result["meta"]["returned"] == 2
    first = result["results"][0]
    assert first["source"] == "docs/kb/退货政策.md"
    assert first["citation"] == "docs/kb/退货政策.md #c1"
    assert result["results"][1]["citation"] == "docs/kb/退货政策.md p.3"


async def test_empty_retrieval_is_not_an_error(customer_settings, monkeypatch):
    """检索 0 条是**数据状况**，不是错误：客户端应当能如实回答「知识库里没有这条」。"""
    monkeypatch.setattr(sync_calls, "retrieve_docs", lambda query, top_k, *, caller: [])
    result = await tool_impl.retrieve_knowledge("股价多少", 5, settings=customer_settings)
    assert result["results"] == []
    assert result["meta"]["returned"] == 0


# ── 三、图表：空数据、写错列名、载荷最小化（复用第 6 步的口径，这一层不许改写）────


async def test_chart_empty_rows_returns_an_empty_chart_with_a_warning(customer_settings):
    spec = await tool_impl.build_chart([], "status", y="n", settings=customer_settings)
    assert spec["data"] == []
    assert spec["meta"]["warnings"], "空图必须带警告（否则客户端会以为查询被吞了）"


async def test_chart_wrong_column_is_a_contract_error_with_available_columns(customer_settings):
    with pytest.raises(Exception) as caught:
        await tool_impl.build_chart(
            [{"status": "paid", "n": 3}], "stauts", y="n", settings=customer_settings
        )
    message = str(caught.value)
    assert "[tool_contract]" in message
    assert "status" in message and "n" in message, "报错必须列出可用列，客户端才能自我纠正"


async def test_chart_payload_is_minimised(customer_settings):
    """载荷最小化：没声明画的列不进 spec（图表数据会被缓存/贴进工单/打日志）。"""
    rows = [{"status": "paid", "n": 3, "phone": "13812341234", "address": "某小区 1 号"}]
    spec = await tool_impl.build_chart(rows, "status", y="n", settings=customer_settings)
    assert set(spec["data"][0]) == {"status", "n"}
    assert "13812341234" not in sync_calls.dumps(spec)


async def test_chart_rows_must_be_a_list(customer_settings):
    with pytest.raises(Exception) as caught:
        await tool_impl.build_chart("not rows", "x", y="y", settings=customer_settings)  # type: ignore[arg-type]
    assert "[tool_contract]" in str(caught.value)


# ── 四、dispatch：按名字分发的边界 ─────────────────────────────────────────────


async def test_dispatch_rejects_tools_outside_the_exposure_list(customer_settings):
    with pytest.raises(Exception) as caught:
        await tool_impl.dispatch("execute_sql", {"sql": "SELECT 1"}, settings=customer_settings)
    message = str(caught.value)
    assert "[unknown_tool]" in message
    assert "sql_query" in message, "报错要告诉客户端「能用的有哪些」"


async def test_dispatch_routes_to_each_tool(customer_settings, monkeypatch):
    monkeypatch.setattr(sync_calls, "execute_readonly_query", _fake_executor([]))
    monkeypatch.setattr(sync_calls, "retrieve_docs", lambda query, top_k, *, caller: [])
    payload = await tool_impl.dispatch(
        "build_chart", {"rows": [{"k": "a", "v": 1}], "x": "k", "y": "v"}, settings=customer_settings
    )
    assert payload["schema"] == "shopsage.chart/v1"
