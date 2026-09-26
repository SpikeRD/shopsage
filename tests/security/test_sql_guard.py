"""SQL 守卫的攻击用例集（`4.security.md` §7「SQL 安全（D1/C1）」）。

**不需要数据库**：本模块是纯静态校验，所以这些用例在没有依赖栈的机器上也必须全绿 ——
这也正是「用 AST 校验」相对「用正则 + 真实库试跑」的价值：拦 SQL 这件事本身可以离线单测。

用例分三块：
1. 攻击集（必须被拦），每条都断言**具体规则名**（`rule`），不是笼统的「抛异常就算过」——
   规则名一改，测试立刻提醒「拦截语义变了」；
2. 合法集（必须放行），专门覆盖「拦得太狠」的误杀：正常对客查询、聚合、CTE、参数化 LIMIT；
3. 判据的边界（豁免清单、顺序、规则名稳定性）。
"""

from __future__ import annotations

import pytest

from app.security.policy import MAX_ROWS
from app.security.sql_guard import SQLSecurityError, validate_sql

# ── ① 黑名单：非只读语句一律拒 ───────────────────────────────────────────────
_WRITE_STATEMENTS = [
    "DROP TABLE users;",
    "DELETE FROM orders;",
    "DELETE FROM orders WHERE order_id = 1",
    "UPDATE orders SET status = 'paid' WHERE order_id = 1",
    "INSERT INTO users (user_id, name) VALUES (999, 'x')",
    "TRUNCATE TABLE orders",
    "ALTER TABLE orders ADD COLUMN x int",
    "GRANT ALL ON orders TO public",
    "CREATE TABLE evil (id int)",
    "VACUUM FULL orders",
    "COPY orders TO '/tmp/orders.csv'",
]

# ── ② 多语句与读写混用（最容易被静默放行的一类）────────────────────────────
_SMUGGLED = [
    # 前半句干净、后半句是攻击 —— `sqlglot.parse_one` 只返回第一条，**静默丢掉**后半句
    ("SELECT 1; DROP TABLE users", "multiple_statements"),
    ("SELECT order_id FROM orders WHERE user_id = 1 LIMIT 1; DELETE FROM orders", "multiple_statements"),
    # 单条语句里的读写混用（不是靠分号，而是靠语句结构）
    ("WITH x AS (DELETE FROM orders RETURNING order_id) SELECT * FROM x", "write_in_read"),
    ("WITH x AS (UPDATE orders SET status = 'x' RETURNING order_id) SELECT * FROM x", "write_in_read"),
    # `SELECT ... INTO <表>`：postgres 方言下这本身就是语法错误（写路径不属于 SELECT），
    # 因此在 PG 方言里由 parse_error 拦住；`app/tools/sql_executor.py` 会在 DML 里再挡一层。
    ("SELECT order_id FROM orders WHERE user_id = 1 INTO evil", "parse_error"),
]

# ── ③ 无 WHERE / 恒真条件（全表扫描 + 越权拖库）─────────────────────────────
_NO_FILTER = [
    "SELECT * FROM orders",
    "SELECT order_id, status FROM orders",
    "SELECT * FROM orders LIMIT 10",  # 有 LIMIT 也不能代替 WHERE：行级过滤是另一回事
    "SELECT * FROM orders WHERE 1=1",
    "SELECT * FROM orders WHERE true",
    "SELECT order_id FROM orders WHERE 1 = 1 LIMIT 10",
    "SELECT order_id FROM orders WHERE 2 > 1 LIMIT 10",  # 恒真不一定写成 =
    "SELECT order_id FROM orders WHERE 1=1 OR user_id = 5 LIMIT 10",  # 恒真分支让整条条件失效
    "SELECT order_id FROM orders WHERE user_id = 5 OR TRUE LIMIT 10",
    "SELECT order_id FROM orders WHERE user_id = 5 AND 1=1 LIMIT 10",
    "SELECT order_id FROM orders WHERE 'a' = 'a' LIMIT 10",
    "SELECT order_id FROM orders LIMIT 10 UNION SELECT order_id FROM orders",  # 分支漏过滤
    "SELECT * FROM (SELECT * FROM orders) AS t LIMIT 10",  # 子查询漏过滤
]

# ── ④ 白名单（表 / 高敏列 / 元数据表）───────────────────────────────────────
_OUT_OF_SCOPE = [
    "SELECT * FROM secret_table WHERE id = 1 LIMIT 1",
    "SELECT * FROM pg_shadow WHERE usename = 'shopuser' LIMIT 1",
    "SELECT * FROM information_schema.tables WHERE table_name = 'users' LIMIT 1",
    "SELECT phone FROM users WHERE user_id = 1",
    "SELECT users.address FROM users WHERE user_id = 1",
    "SELECT id_card FROM users WHERE user_id = 1",
    "SELECT * FROM users WHERE user_id = 1",  # SELECT * 在含高敏列的表上 = 绕过列白名单
    "SELECT u.* FROM users AS u WHERE u.user_id = 1",
    "SELECT operator_id FROM stock_movements WHERE movement_id = 1",
    # 不带表名的高敏列：少写限定名不能成为绕过手段（JOIN 场景下最容易发生）
    "SELECT o.order_id FROM orders AS o JOIN users AS u ON u.user_id = o.user_id "
    "WHERE o.user_id = :uid AND phone = '13800000000' LIMIT 10",
    "SELECT * FROM secret_table AS t WHERE t.id = 1 LIMIT 1",
]

# ── ⑤ 危险函数与资源放大 ────────────────────────────────────────────────────
_DANGEROUS_FUNCTIONS = [
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_ls_dir('/')",
    "SELECT pg_sleep(60)",
    "SELECT dblink('host=other', 'select 1')",
    "SELECT pg_read_binary_file('/etc/shadow')",
    "SELECT pg_terminate_backend(1)",
]

# ── ⑥ 行数上限（C1）────────────────────────────────────────────────────────
_LIMIT_VIOLATIONS = [
    f"SELECT * FROM orders WHERE user_id = 1 LIMIT {MAX_ROWS + 1}",
    "SELECT * FROM orders WHERE user_id = 1 LIMIT 999999",
    "SELECT * FROM orders WHERE user_id = 1 LIMIT 2147483647",
    # 把多行塞进一行的聚合：结果虽只一行，内容却是多行数据 —— 不给「单行」豁免
    "SELECT string_agg(status, ',') FROM orders WHERE user_id = 1",
    "SELECT array_agg(order_id) FROM orders WHERE user_id = 1",
]

# ── ⑦ 绕过变形（注释 / 大小写 / 换行 / 反引号 / 全角）──────────────────────
_OBFUSCATED = [
    "SELECT * FROM orders /* WHERE user_id = 1 */ LIMIT 10",
    "SELECT * FROM orders -- WHERE user_id = 1\nLIMIT 10",
    "select * from orders where 1=1 limit 10",
    "SELECT * FROM Orders WHERE 1=1 LIMIT 10",
    "SELECT order_id FROM orders;",
    "SELECT * FROM orders WHERE user_id IN (1) LIMIT 10 UNION ALL SELECT * FROM orders",
    "SELECT * FROM orders WHERE user_id = 1 LIMIT 10; -- 注释尾巴里塞第二条",
    "SeLeCt * FrOm orders LiMiT 999999",
]

_ATTACKS = (
    [(s, "not_read_only") for s in _WRITE_STATEMENTS]
    + list(_SMUGGLED)
    + [(s, "missing_where") for s in _NO_FILTER[:3]]
    + [(s, "trivial_where") for s in _NO_FILTER[3:11]]
    + [(s, "table_not_allowed") for s in _OUT_OF_SCOPE[:3]]
    + [(s, "column_not_allowed") for s in _OUT_OF_SCOPE[3:6]]
    + [(s, "star_on_protected_table") for s in _OUT_OF_SCOPE[6:8]]
    + [(s, "column_not_allowed") for s in _OUT_OF_SCOPE[8:10]]
    + [(s, "table_not_allowed") for s in _OUT_OF_SCOPE[10:]]
    # 危险函数在「没有 FROM」时也必须先按 forbidden_function 拦下（不能只剩 missing_limit）
    + [(s, "forbidden_function") for s in _DANGEROUS_FUNCTIONS]
    + [(s, "limit_too_large") for s in _LIMIT_VIOLATIONS[:3]]
    + [(s, "missing_limit") for s in _LIMIT_VIOLATIONS[3:]]
)


@pytest.mark.parametrize(("sql", "rule"), _ATTACKS, ids=lambda v: v[:48] if isinstance(v, str) else v)
def test_reject_dangerous_sql(sql: str, rule: str):
    """攻击集：必须被拦，且**拦它的规则**要符合预期。"""
    with pytest.raises(SQLSecurityError) as exc:
        validate_sql(sql)
    assert exc.value.rule == rule, f"拦截规则不符：期望 {rule}，实际 {exc.value.rule}（{exc.value}）"


@pytest.mark.parametrize(
    "sql",
    [
        # 无 WHERE 的分支（UNION / 子查询）与恒真条件在改写后仍要拦
        "SELECT order_id FROM orders WHERE user_id = 1 LIMIT 10 UNION SELECT order_id FROM orders",
        "SELECT * FROM (SELECT * FROM orders) AS t LIMIT 10",
        "SELECT * FROM orders WHERE user_id = 1 AND EXISTS (SELECT 1 FROM users)",
        # 注释/大小写/换行变形也必须拦（这是「用 AST 而不是正则」的直接收益）
        "SELECT * FROM orders WHERE 1=1 LIMIT 10",
        "SELECT * FROM orders WHERE 1 /* 混淆 */ = 1 LIMIT 10",
        "SELECT * FROM orders\nWHERE\n1\n=\n1\nLIMIT 10",
    ],
)
def test_reject_obfuscated_bypasses(sql: str):
    """变形/注释/换行都不能绕过：判据是 AST 结构，不受文本形态影响。"""
    with pytest.raises(SQLSecurityError):
        validate_sql(sql)


@pytest.mark.parametrize("sql", _OBFUSCATED)
def test_obfuscated_variants_are_still_blocked(sql: str):
    """⑦ 里的每一条都要被拦（规则名不限，拦不住才是问题）。"""
    with pytest.raises(SQLSecurityError):
        validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # 对客查询：行级过滤 + LIMIT
        "SELECT order_id, status FROM orders WHERE user_id = :uid LIMIT 20",
        # 主键等值查询：天然 0/1 行，不强制 LIMIT（与 app/db/queries.py 同口径）
        "SELECT order_id, status FROM orders WHERE order_id = 1",
        # 聚合：结果一行
        "SELECT count(*) FROM orders WHERE user_id = 1",
        "SELECT sum(total_amount) AS total, count(*) AS n FROM orders WHERE user_id = 1",
        # 不带高敏列的表可以 SELECT *
        "SELECT * FROM orders WHERE user_id = 1 LIMIT 10",
        "SELECT o.order_id, o.status FROM orders AS o WHERE o.user_id = :uid AND o.status = :st LIMIT 20",
        # JOIN 白名单表
        "SELECT o.order_id, p.name FROM orders AS o JOIN order_items AS i ON i.order_id = o.order_id "
        "JOIN products AS p ON p.product_id = i.product_id WHERE o.user_id = :uid LIMIT 20",
        # 时间范围 + LIMIT
        "SELECT * FROM orders WHERE created_at > now() - interval '7 day' LIMIT 50",
        # CTE 里带过滤
        "WITH recent AS (SELECT order_id, user_id, status FROM orders WHERE user_id = :uid LIMIT 50) "
        "SELECT status, count(*) FROM recent WHERE recent.user_id = :uid GROUP BY status LIMIT 10",
        # 参数化 LIMIT：值在运行期定，守卫收紧到 MAX_ROWS
        "SELECT order_id FROM orders WHERE user_id = :uid LIMIT :n",
        # 不含表的常量查询
        "SELECT 1",
    ],
)
def test_allow_safe_query(sql: str):
    """合法集：不能误杀。日常对客查询、聚合、CTE、参数化 LIMIT 都要放行。"""
    result = validate_sql(sql)
    assert result.sql
    if "FROM" in sql.upper():  # 审计信息里要能看到表名；`SELECT 1` 本来就没有表
        assert result.tables


def test_safe_query_returns_limit_and_audit_info():
    """放行时要给出可执行的 SQL 与审计信息（表名/列名/行数上限）。"""
    result = validate_sql("SELECT order_id, status FROM orders WHERE user_id = :uid LIMIT 20")
    assert result.limit == 20
    assert result.limit_clamped is False
    assert "orders" in result.tables
    assert {"order_id", "status", "user_id"} <= set(result.columns)
    assert result.notes


def test_parameterized_limit_is_clamped_to_max_rows():
    """`LIMIT :n` 的值运行期才定 → 守卫把生效上限收到 MAX_ROWS（C1）。"""
    result = validate_sql("SELECT order_id FROM orders WHERE user_id = :uid LIMIT :n")
    assert result.limit == MAX_ROWS
    assert result.limit_clamped is True


def test_limit_at_the_boundary_is_allowed():
    """恰好等于上限要放行（边界不能差一）。"""
    result = validate_sql(f"SELECT order_id FROM orders WHERE user_id = 1 LIMIT {MAX_ROWS}")
    assert result.limit == MAX_ROWS
    assert result.limit_clamped is False


def test_empty_sql_is_rejected():
    for empty in ("", "   ", "\n"):
        with pytest.raises(SQLSecurityError) as exc:
            validate_sql(empty)
        assert exc.value.rule == "empty_sql"


def test_parse_error_is_rejected_not_ignored():
    """**解析不了的一律拒绝**：语法错误不能当成「大概没问题」放行。"""
    with pytest.raises(SQLSecurityError) as exc:
        validate_sql("SELECT FROM WHERE ;;; )(")
    assert exc.value.rule in {"parse_error", "not_read_only", "empty_sql"}


def test_error_carries_rule_and_sql_for_audit():
    """异常要带规则名与原始 SQL：拦截率统计与事后审计都靠它。"""
    with pytest.raises(SQLSecurityError) as exc:
        validate_sql("DROP TABLE users;")
    assert exc.value.rule == "not_read_only"
    assert "DROP TABLE" in exc.value.sql


def test_allow_tables_can_be_narrowed_by_caller():
    """调用方能把白名单收得更窄（默认拒绝，而不是「默认全开」）。"""
    with pytest.raises(SQLSecurityError) as exc:
        validate_sql("SELECT order_id FROM orders WHERE user_id = 1 LIMIT 1", allow_tables={"users"})
    assert exc.value.rule == "table_not_allowed"

    result = validate_sql("SELECT order_id FROM orders WHERE user_id = 1 LIMIT 1", allow_tables={"orders"})
    assert result.tables == ("orders",)


def test_protected_columns_follow_policy_table():
    """高敏列清单来自 policy（唯一真相），可由调用方覆盖。"""
    from app.security import policy

    assert "phone" in policy.PROTECTED_COLUMNS["users"]
    # 覆盖成空表 → 同一句 SQL 放行，说明「高敏列」确实是这份清单在起作用
    result = validate_sql(
        "SELECT phone FROM users WHERE user_id = 1",
        protected_columns={},
    )
    assert "phone" in result.columns


def test_comment_only_where_is_still_missing_where():
    """注释里的 WHERE 不算 WHERE（正则实现最容易在这里翻车）。"""
    with pytest.raises(SQLSecurityError) as exc:
        validate_sql("SELECT * FROM orders /* WHERE user_id = 1 */ LIMIT 10")
    assert exc.value.rule == "missing_where"
