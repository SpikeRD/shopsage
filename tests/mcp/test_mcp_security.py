"""安全用例（D1：MCP 入口与内部调用**同一套**约束）。

`15.mcp.md` §7 第一条写的是 `test_mcp_sql_passes_guard`：
「MCP 调 sql_query 传入 DELETE → 被 validate_sql 拦截（D1）」。
本文件把这一条展开成**一个矩阵**，因为「被拦」不是一件事：

| 用例 | 拦在哪一层 | 规则名（客户端据此纠正） |
|---|---|---|
| 写语句 / 多语句 / 危险函数 | `security.sql_guard`（静态守卫） | `not_read_only` / `multiple_statements` / … |
| 无 WHERE 的全表查询 | `security.sql_guard` | `missing_where` / `trivial_where` |
| 白名单外的表 | `security.sql_guard` | `table_not_allowed` |
| **缺行级过滤** | `app/mcp/sync_calls`（本步新增） | `mcp_missing_row_filter` |
| **过滤写成字面量** | 同上 | `mcp_row_filter_needs_binding` |
| **绑定值指向别人** | 同上 | `mcp_row_filter_mismatch` |
| 连接本身不能写 | 只读数据库角色（第 4 步） | 库层权限错误 |

⚠️ 顺序是**先守卫、再行级过滤**，这一条本身就是个踩过的坑（runbook §10.1 坑 1）：
`DELETE FROM orders` 若先撞「缺行级过滤」，客户端会以为「加个 WHERE 就行」，
而它真正的问题是写语句。规则名是给模型自我纠正用的，必须先答「这是什么形态」。
"""

from __future__ import annotations

import pytest

from app.mcp import sync_calls
from app.mcp import tools as tool_impl
from app.mcp.settings import McpSettings
from tests.mcp.conftest import mcp_error_text, tool_json

# ── 一、静态守卫在这一层同样生效（不允许「MCP 有后门」）─────────────────────────

GUARD_CASES = [
    ("DELETE FROM orders", "not_read_only"),
    ("UPDATE orders SET status = 'x' WHERE user_id = :user_id", "not_read_only"),
    ("INSERT INTO tickets (user_id) VALUES (:user_id)", "not_read_only"),
    ("SELECT 1; DROP TABLE orders", "multiple_statements"),
    ("SELECT order_id FROM orders", "missing_where"),
    ("SELECT order_id FROM orders WHERE 1=1", "trivial_where"),
    # 受保护列：`users.email` 是高敏列（要脱敏出口，不能自由 SQL 直查）
    ("SELECT email FROM users WHERE user_id = :user_id", "column_not_allowed"),
    # 白名单外的表：默认拒绝
    ("SELECT order_id FROM secret_table WHERE user_id = :user_id", "table_not_allowed"),
    ("SELECT pg_read_file('/etc/passwd') FROM orders WHERE user_id = :user_id", "forbidden_function"),
]


@pytest.mark.parametrize(("sql", "rule"), GUARD_CASES)
async def test_mcp_sql_query_passes_through_the_guard(sql, rule, customer_settings):
    """★`15.mcp.md` §7 第一条：MCP 入口不走后门，写语句/越权形态照旧被守卫拦。"""
    with pytest.raises(Exception) as caught:
        await tool_impl.sql_query(sql, {"user_id": 1}, None, settings=customer_settings)
    message = str(caught.value)
    assert f"[{rule}]" in message, f"期望规则 {rule}，实际消息：{message}"


async def test_write_statement_is_reported_as_not_read_only_not_as_missing_filter(customer_settings):
    """顺序用例：写语句的报错必须是「只允许 SELECT」，而不是「缺行级过滤」。

    这正是上面那条矩阵之外要单独钉的一件事 —— 两条规则都能拦住它，
    但只有一条是对的答案。客户端按错误的规则名去改，会改出一个「更合规的写语句」。
    """
    with pytest.raises(Exception) as caught:
        await tool_impl.sql_query("DELETE FROM orders", None, None, settings=customer_settings)
    message = str(caught.value)
    assert "[not_read_only]" in message
    assert "mcp_missing_row_filter" not in message


# ── 二、行级过滤（本步新增的那一层）────────────────────────────────────────────


async def test_missing_row_filter_is_rejected(customer_settings):
    with pytest.raises(Exception) as caught:
        await tool_impl.sql_query(
            "SELECT order_id FROM orders WHERE status = :s LIMIT 5",
            {"s": "paid"},
            None,
            settings=customer_settings,
        )
    assert "[mcp_missing_row_filter]" in str(caught.value)


async def test_literal_row_filter_is_rejected(customer_settings):
    """字面量过滤没有「可校验的对象」—— 必须用绑定参数。"""
    with pytest.raises(Exception) as caught:
        await tool_impl.sql_query(
            "SELECT order_id FROM orders WHERE user_id = 1 LIMIT 5",
            {"user_id": 1},
            None,
            settings=customer_settings,
        )
    assert "[mcp_row_filter_needs_binding]" in str(caught.value)


async def test_binding_must_equal_the_server_identity(customer_settings):
    """★越权核心用例：绑定参数指向别人 → 拦，且消息里点明「身份来自服务端配置」。"""
    with pytest.raises(Exception) as caught:
        await tool_impl.sql_query(
            "SELECT order_id FROM orders WHERE user_id = :user_id LIMIT 5",
            {"user_id": 2},
            None,
            settings=customer_settings,
        )
    message = str(caught.value)
    assert "[mcp_row_filter_mismatch]" in message
    assert "MCP_ACTOR_USER_ID" in message


async def test_filter_binding_must_be_present_in_params(customer_settings):
    """SQL 里写了 `:user_id` 但参数表里没有 → 明确报「缺绑定值」，而不是让它去撞数据库。"""
    with pytest.raises(Exception) as caught:
        await tool_impl.sql_query(
            "SELECT order_id FROM orders WHERE user_id = :user_id LIMIT 5",
            {},
            None,
            settings=customer_settings,
        )
    assert "[mcp_missing_row_filter]" in str(caught.value)


@pytest.mark.parametrize("value", ["abc", None, 1.5])
async def test_filter_binding_must_be_an_integer(value, customer_settings):
    with pytest.raises(Exception) as caught:
        await tool_impl.sql_query(
            "SELECT order_id FROM orders WHERE user_id = :user_id LIMIT 5",
            {"user_id": value},
            None,
            settings=customer_settings,
        )
    assert "[mcp_invalid_row_filter]" in str(caught.value)


# ── 三、内部策略：放宽的是"行范围"，不是"守卫"────────────────────────────────


async def test_internal_policy_allows_cross_user_queries(internal_settings):
    """★反向用例：内部策略下**不再**强制行级过滤（这是有意的放宽，见 runbook §4.3）。

    用假执行器换掉真查询：这条用例量的是「MCP 侧的边界是否放行」，
    不是「数据库能不能查」（后者由 live 用例负责）。
    """
    calls: list[dict] = []

    def fake(sql, params, *, max_rows, caller=None):
        calls.append({"sql": sql, "max_rows": max_rows, "caller": caller})
        return [], {
            "audit_id": "tc-fake",
            "row_count": 0,
            "truncated": False,
            "columns": [],
            "tables": ["orders"],
            "declared_limit": None,
            "limit_clamped": False,
            "bound_limit_clamped": False,
            "rows_cap": max_rows,
            "duration_ms": 0,
        }

    original = sync_calls.execute_readonly_query
    sync_calls.execute_readonly_query = fake
    try:
        await tool_impl.sql_query(
            "SELECT count(*) AS n FROM orders WHERE status = :s", {"s": "paid"}, None,
            settings=internal_settings,
        )
    finally:
        sync_calls.execute_readonly_query = original
    assert calls, "内部策略下这条（无行级过滤的）查询应当被放行到执行器"
    assert calls[0]["max_rows"] == internal_settings.max_rows


async def test_internal_policy_still_blocks_write_statements(internal_settings):
    """内部策略**不是**「关掉安全检查」：写语句照旧被守卫拦。"""
    with pytest.raises(Exception) as caught:
        await tool_impl.sql_query("DELETE FROM orders", None, None, settings=internal_settings)
    assert "[not_read_only]" in str(caught.value)


# ── 四、写路径：对客只能建自己的工单 ──────────────────────────────────────────


async def test_customer_cannot_create_a_ticket_for_someone_else(customer_settings):
    with pytest.raises(Exception) as caught:
        await tool_impl.create_ticket(
            "logistics", "test-mcp-cross", user_id=2, settings=customer_settings
        )
    message = str(caught.value)
    assert "[mcp_row_filter_mismatch]" in message
    assert "只能建自己的工单" in message


async def test_idempotency_key_is_required_from_the_client(customer_settings):
    """服务端**不替客户端生成**幂等键：生成了就等于每次都新建（幂等形同虚设）。"""
    for empty in ("", "   ", None):
        with pytest.raises(Exception) as caught:
            await tool_impl.create_ticket("logistics", empty, settings=customer_settings)
        assert "[tool_contract]" in str(caught.value)


async def test_role_is_taken_from_settings_not_from_the_client(customer_settings):
    """★D1：没有 `role` 这个名字的入参 —— 客户端无法把自己说成客服/管理员。

    用具名参数去撞签名：Python 会直接抛 `TypeError`（连到不了 RBAC 判定），
    这比"传了被忽略"更好 —— 它保证**不可能**有人以为这个入口支持提权。
    """
    with pytest.raises(TypeError):
        await tool_impl.create_ticket(
            "logistics", "test-mcp-role", role="admin", settings=customer_settings  # type: ignore[call-arg]
        )


async def test_customer_role_never_gets_the_refund_tool(internal_settings):
    """退款不对外：即使内部策略也只暴露四个工具（`15.mcp.md` §4 第 3 条）。"""
    with pytest.raises(Exception) as caught:
        await tool_impl.dispatch("create_refund_request", {"user_id": 1}, settings=internal_settings)
    assert "[unknown_tool]" in str(caught.value)


def test_error_messages_do_not_leak_infrastructure_details():
    """错误消息是给不可信客户端看的：不许出现连接串、SQLSTATE、堆栈。"""
    from app.security.sql_guard import SQLSecurityError

    exc = SQLSecurityError("拒绝写语句", rule="not_read_only", sql="DELETE FROM orders")
    message = str(tool_impl.as_tool_error(exc))
    assert "[not_read_only]" in message
    for leak in ("postgresql://", "Traceback", "psycopg", "shopsage_readonly"):
        assert leak not in message
