"""`sql_executor` 的测试：**拦得住、截得断、走得对引擎、留得下审计**。

分两层（与 `test_db_queries.py` 同口径）：

- **离线层**：所有被拒绝的路径都不需要数据库 —— 它们在 `validate_sql` 就失败了。
  这一层是安全判据的主场：**每一条拒绝都要断言「按哪条规则拒的」**，
  否则「被别的规则顺带拦下」会被误读成「这条规则有效」（`4.security-runbook` §7.1 坑 2 的教训）。
- **真库层（`@pytest.mark.db`）**：截断、只读角色、绑定参数夹取 ——
  这些必须真的打到 PostgreSQL 才能证明（mock 出来的「截断」只能证明我们自己写了个 if）。

⚠️ 用例名与 `6.tools.md` 第 7 节列的两条对齐（`test_sql_executor_rejects_write` /
`test_sql_executor_truncates`），便于逐条核对文档与实现的对应关系。
但**实现口径与该文档示例有一处不同**：文档写的是 `SELECT * FROM orders`，
而这条 SQL 在守卫里会被 `missing_where` 拦下（无 WHERE 的整表查询一律拒绝），
所以截断用例带了 WHERE —— 见 `docs/runbook/6.tools-runbook.md` §10.1 坑 1。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.session import engine, get_read_engine
from app.security.sql_guard import SQLSecurityError
from app.tools.audit import audit_records
from app.tools.errors import ToolContractError
from app.tools.sql_executor import (
    ABSOLUTE_MAX_ROWS,
    DEFAULT_MAX_ROWS,
    clamp_bound_limit,
    execute_readonly_query,
    limit_placeholder_name,
)

CALLER = "test"


# ── 离线层：拒绝（不需要数据库）───────────────────────────────────────────────


def test_sql_executor_rejects_write():
    """`6.tools.md` §7 第 1 条：写语句必须在校验层就被拒（**原样抛 SQLSecurityError**）。"""
    with pytest.raises(SQLSecurityError) as excinfo:
        execute_readonly_query("DELETE FROM orders", caller=CALLER)
    assert excinfo.value.rule == "not_read_only"
    assert excinfo.value.sql == "DELETE FROM orders"  # 原始 SQL 留在异常上，便于日志取证


def test_sql_executor_rejects_multiple_statements():
    """多语句：`parse_one` 会**静默丢掉**后半句，所以这条必须按规则名断言。"""
    with pytest.raises(SQLSecurityError) as excinfo:
        execute_readonly_query("SELECT order_id FROM orders WHERE user_id = 1 LIMIT 1; DROP TABLE orders", caller=CALLER)
    assert excinfo.value.rule == "multiple_statements"


def test_sql_executor_rejects_whereless_scan():
    """无 WHERE 的整表查询（`6.tools.md` §7 的示例 SQL 就长这样，实际会被拦）。"""
    with pytest.raises(SQLSecurityError) as excinfo:
        execute_readonly_query("SELECT order_id FROM orders LIMIT 10", caller=CALLER)
    assert excinfo.value.rule == "missing_where"


def test_sql_executor_rejects_over_limit_literal():
    """字面量超上限：`LIMIT 999999` 被 `limit_too_large` 拦下（C1 的第一道）。"""
    with pytest.raises(SQLSecurityError) as excinfo:
        execute_readonly_query(
            f"SELECT order_id FROM orders WHERE user_id = 1 LIMIT {ABSOLUTE_MAX_ROWS + 1}", caller=CALLER
        )
    assert excinfo.value.rule == "limit_too_large"


def test_sql_executor_never_touches_database_on_rejection():
    """被拒的调用**一次数据库连接都不该建**（否则拒绝路径本身就是资源放大器）。

    手法：把 `get_read_engine` 换成「一被调用就炸」的假引擎 ——
    它在拒绝路径上不该被碰到。这比「断言抛了异常」强：后者不排除「先查了库再拒绝」。
    """
    import app.tools.sql_executor as sql_executor

    def boom():  # pragma: no cover - 只会在实现退化时被调用
        raise AssertionError("校验未通过却已经去连数据库了：拒绝路径必须先于取连接")

    original = sql_executor.get_read_engine
    sql_executor.get_read_engine = boom
    try:
        with pytest.raises(SQLSecurityError):
            execute_readonly_query("DROP TABLE orders", caller=CALLER)
    finally:
        sql_executor.get_read_engine = original


def test_rows_cap_can_only_tighten():
    """`max_rows` 只能**调低**：调高会被夹回 `ABSOLUTE_MAX_ROWS`（C1 的上限不是建议值）。"""
    from app.tools.sql_executor import _resolve_rows_cap

    assert _resolve_rows_cap(None) == DEFAULT_MAX_ROWS
    assert _resolve_rows_cap(5) == 5
    assert _resolve_rows_cap(99_999) == ABSOLUTE_MAX_ROWS  # 想放宽？夹回去
    with pytest.raises(ToolContractError):
        _resolve_rows_cap(0)
    with pytest.raises(ToolContractError):
        _resolve_rows_cap(True)  # bool 是 int 的子类，必须显式挡住（否则 True 会被当成上限 1）


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT a FROM orders WHERE user_id = :uid LIMIT :n", "n"),
        ("SELECT a FROM orders WHERE user_id = %(uid)s LIMIT %(n)s", "n"),
        ("SELECT a FROM orders WHERE user_id = 1 LIMIT 20", None),
        ("SELECT a FROM orders WHERE user_id = 1", None),
    ],
)
def test_limit_placeholder_name_shapes(sql, expected):
    """占位符 LIMIT 的识别（两种写法都要认；字面量/无 LIMIT 返回 None）。"""
    assert limit_placeholder_name(sql) == expected


def test_clamp_bound_limit_only_tightens_placeholder():
    """绑定参数夹取：只夹**占位符 LIMIT**，且只在超上限时动手。"""
    sql = "SELECT a FROM orders WHERE user_id = :uid LIMIT :n"

    bound, clamped = clamp_bound_limit(sql, {"uid": 1, "n": 10**9}, 201)
    assert clamped is True and bound["n"] == 201  # 夹到「上限 + 1」：多的一行给 truncated 探针

    bound, clamped = clamp_bound_limit(sql, {"uid": 1, "n": 50}, 201)
    assert clamped is False and bound["n"] == 50  # 没超就不动它（保持调用方意图）

    bound, clamped = clamp_bound_limit(sql, {"uid": 1}, 201)
    assert clamped is False  # 参数没给 → 交给数据库报错（那是不可恢复错误）

    # 字面量 LIMIT：没有占位符可夹，参数原样透传
    bound, clamped = clamp_bound_limit(
        "SELECT a FROM orders WHERE user_id = 1 LIMIT 20", {"n": 10**9}, 201
    )
    assert clamped is False and bound["n"] == 10**9

    # 非整数绑定值：不猜、不转，交给数据库按类型错误拒绝
    bound, clamped = clamp_bound_limit(sql, {"uid": 1, "n": "many"}, 201)
    assert clamped is False and bound["n"] == "many"


def test_rejected_call_is_audited(caplog):
    """**被拦下的调用同样进审计**（谁在什么时候试图执行写操作）—— 只记成功的审计等于没有审计。"""
    with caplog.at_level("WARNING", logger="app.tools.audit"):
        with pytest.raises(SQLSecurityError):
            execute_readonly_query("UPDATE orders SET status = 'paid' WHERE order_id = 1", caller="agent:data")

    records = audit_records(caplog.records)
    assert len(records) == 1
    record = records[0]
    assert record["tool"] == "sql_query"
    assert record["caller"] == "agent:data"
    assert record["outcome"] == "error"
    assert record["error_type"] == "SQLSecurityError"
    assert "UPDATE orders" in record["args"]["sql"]  # 原始 SQL 留在审计里（取证用）
    assert record["rows"] is None


def test_caller_is_required():
    """没有 caller 的调用在**源头**被拒（审计字段不可选）。"""
    with pytest.raises(ToolContractError, match="caller"):
        execute_readonly_query("SELECT order_id FROM orders WHERE user_id = 1 LIMIT 1", caller="  ")


# ── 真库层：截断、只读引擎、审计字段 ──────────────────────────────────────────


@pytest.mark.db
def test_sql_executor_truncates(seeded):
    """`6.tools.md` §7 第 2 条：超过上限即截断，且 `meta["truncated"]` 为 True。

    手法：`LIMIT :n` 绑定一个超大值（校验层看不穿运行期才定下来的值），
    执行层把它夹到「上限 + 1」并多取一行做探针 → 拿到 `上限 + 1` 行即判定截断。
    """
    order = _any_order()
    rows, meta = execute_readonly_query(
        "SELECT order_id, user_id FROM orders WHERE user_id = :uid ORDER BY order_id LIMIT :n",
        {"uid": order[0], "n": 10**9},
        max_rows=2,
        caller=CALLER,
    )
    assert meta["truncated"] is True
    assert len(rows) == 2  # 上限是硬上限：多取的那一行只用于判定，永不返回
    assert meta["row_count"] == len(rows) <= ABSOLUTE_MAX_ROWS
    assert meta["bound_limit_clamped"] is True


@pytest.mark.db
def test_sql_executor_does_not_truncate_when_data_fits(seeded):
    """数据不够上限时 `truncated` 必须是 False —— 否则调用方会把「数据就这么多」误读成「被截了」。"""
    order = _any_order()
    rows, meta = execute_readonly_query(
        "SELECT order_id FROM orders WHERE user_id = :uid ORDER BY order_id LIMIT 200",
        {"uid": order[0]},
        caller=CALLER,
    )
    assert meta["truncated"] is False
    assert meta["row_count"] == len(rows) == order[1]


@pytest.mark.db
def test_sql_executor_runs_on_read_only_engine(seeded):
    """**只读引擎的证据**：工具查到的 `current_user` 就是第 4 步建的只读角色。

    这条比「代码里写了 get_read_engine()」强：它证明那条连接**真的**落在只读角色上。
    没配 `DATABASE_READ_URL` 的环境（读写同库）会跳过该断言并说明原因，而不是假绿。
    """
    rows, meta = execute_readonly_query("SELECT current_user AS db_user", caller=CALLER)
    assert meta["columns"] == ["db_user"]
    actual = rows[0]["db_user"]
    if get_read_engine() is engine:
        pytest.skip(
            "未配置 DATABASE_READ_URL：读路径回落到写引擎，无法断言只读角色"
            "（见 docs/runbook/4.security-runbook.md §3.2）"
        )
    assert actual == "shopsage_readonly", (
        f"读路径落到了 {actual}，而不是只读角色：检查 DATABASE_READ_URL 与 get_read_engine()"
    )


@pytest.mark.db
def test_successful_call_records_audit_fields(seeded, caplog):
    """成功调用的审计字段：调用者/时间/参数/结果行数四样齐（`6.tools.md` §4 第 2 条）。"""
    order = _any_order()
    with caplog.at_level("INFO", logger="app.tools.audit"):
        rows, meta = execute_readonly_query(
            "SELECT order_id FROM orders WHERE user_id = :uid ORDER BY order_id LIMIT 50",
            {"uid": order[0]},
            caller="mcp:sql_query",
        )

    record = audit_records(caplog.records)[-1]
    assert record["audit_id"] == meta["audit_id"]  # 日志与返回值靠这个 id 串起来
    assert record["caller"] == "mcp:sql_query"
    assert record["outcome"] == "ok"
    assert record["rows"] == len(rows)
    assert record["started_at"].endswith("+00:00")
    assert isinstance(record["duration_ms"], int)
    assert record["args"]["params"]["uid"] == order[0]
    assert record["details"]["tables"] == ["orders"]


@pytest.mark.db
def test_executor_returns_raw_driver_values(seeded):
    """值保持驱动原样（`Decimal`/`datetime`），**不在这一层做 JSON 化**：

    金额要参与比较、时间要参与计算；在这里 `float()`/`str()` 一把梭，
    下游就会拿到「看起来一样、算起来不一样」的数据。
    """
    order = _any_order()
    rows, _ = execute_readonly_query(
        "SELECT total_amount, created_at FROM orders WHERE user_id = :uid ORDER BY order_id LIMIT 1",
        {"uid": order[0]},
        caller=CALLER,
    )
    from datetime import datetime
    from decimal import Decimal

    assert isinstance(rows[0]["total_amount"], Decimal)
    assert isinstance(rows[0]["created_at"], datetime)


@pytest.mark.db
def test_binding_parameters_never_interpolates(seeded):
    """参数化查询：SQL 文本里带引号的输入不会变成注入（绑定值原样传给驱动）。"""
    rows, _ = execute_readonly_query(
        "SELECT order_id FROM orders WHERE status = :status ORDER BY order_id LIMIT 5",
        {"status": "paid' OR TRUE --"},
        caller=CALLER,
    )
    assert rows == []  # 整串被当成一个字符串值去比较，匹配不到任何状态


def _any_order() -> tuple[int, int]:
    """取一个「订单数最多」的用户与其订单数（种子数据的具体值见 `scripts/seed.py`）。"""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "select user_id, count(*) from orders group by user_id"
                " having count(*) >= 2 order by count(*) desc limit 1"
            )
        ).one()
    return int(row[0]), int(row[1])
