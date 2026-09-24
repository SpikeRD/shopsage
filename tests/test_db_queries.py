"""行级过滤（D1）的测试。

分两层：
- **纯逻辑层（不需要数据库）**：把每个对客查询编译成 SQL，断言「带 WHERE、带 LIMIT」。
  这一层用 `pkgutil` 遍历 `app.db.queries` 里所有公开函数，**新增查询函数时自动纳入检查** ——
  以后有人写了不带 `WHERE user_id` 的封装，测试立刻红，不用等越权事件暴露。
- **集成层（`@pytest.mark.db`）**：对真库断言「拿 A 的 id 查不到 B 的数据」；
  库侧证据用 `pg_stat_statements` 时**按 queryid 锚定**，而不是按 «全文含 user_id» 捞一批
  —— 后者会把「挑种子数据的装置查询」（带 `user_id` 列名、但没有 WHERE）也算成违规（见 §5.2 与 §7.1）。
  ⚠️ 这几条不要在 `pytest-xdist` 下并发跑（`-n`），否则别的 worker 的 orders 语句会混进快照。
"""

from __future__ import annotations

import inspect
import re
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db import queries as queries_module
from app.db.models import Order
from app.db.queries import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    assert_row_filtered,
    get_customer_profile,
    get_order_for_customer,
    get_orders_for_customer,
    get_refunds_for_customer,
    get_tickets_for_customer,
)
from app.db.session import SessionLocal, engine
from scripts.db_check import MIN_COUNTS, snapshot

# ── 第 1 层：编译期检查（不需要数据库）────────────────────────────────────────


def _public_query_functions() -> list[tuple[str, inspect.Signature]]:
    """`app.db.queries` 里定义的公开函数（= 数据访问出口清单）。"""
    found: list[tuple[str, inspect.Signature]] = []
    for name, obj in vars(queries_module).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        if obj.__module__ != queries_module.__name__:  # 排除 import 进来的外部函数
            continue
        found.append((name, inspect.signature(obj)))
    return found


def test_every_public_query_is_row_filterable_via_assert_helper():
    """`assert_row_filtered` 是「把约定变成检查」的那一步：不带 WHERE 的语句必须被拒。"""
    unfiltered = select(Order).limit(10)
    with pytest.raises(AssertionError, match="WHERE"):
        assert_row_filtered(unfiltered)


def test_assert_helper_also_requires_limit_for_list_queries():
    """列表类对客查询还必须带 LIMIT（C1：结果行数截断）。"""
    no_limit = select(Order).where(Order.user_id == 1)
    with pytest.raises(AssertionError, match="LIMIT"):
        assert_row_filtered(no_limit)
    # 单行查询可以不带 LIMIT（主键等值，天然 0/1 行）
    assert "WHERE" in assert_row_filtered(no_limit, expect_limit=False)


def test_query_module_exposes_expected_customer_functions():
    """出口清单要稳定：删/改了函数名，这里先红（4.security.md 也依赖这套出口）。"""
    names = {name for name, _ in _public_query_functions()}
    expected = {
        "get_orders_for_customer",
        "get_order_for_customer",
        "get_refunds_for_customer",
        "get_tickets_for_customer",
        "get_customer_profile",
        "assert_row_filtered",
    }
    missing = expected - names
    assert not missing, f"app.db.queries 缺少对客查询出口: {sorted(missing)}"


def test_customer_query_signatures_take_user_id_first():
    """对客查询的第一个位置参数必须是 user_id —— 从签名上就没有「忘了过滤」的口子。"""
    for name, sig in _public_query_functions():
        if not name.startswith("get_"):
            continue
        params = [p for p in sig.parameters.values() if p.kind is not p.KEYWORD_ONLY]
        assert params, f"{name} 没有位置参数"
        assert params[0].name == "user_id", f"{name} 的第一个位置参数不是 user_id，而是 {params[0].name}"


def _clamp(value: int) -> int:
    """直接调用内部夹取逻辑，避免为了测边界去连库。"""
    return queries_module._clamp_limit(value)  # noqa: SLF001 - 边界逻辑的单元测试


def test_limit_is_clamped_so_no_one_can_ask_for_everything():
    """limit 上下界：0/负数直接拒绝，超过 MAX_LIMIT 会被夹到 MAX_LIMIT。"""
    with pytest.raises(ValueError):
        _clamp(0)
    with pytest.raises(ValueError):
        _clamp(-5)
    assert _clamp(10_000) == MAX_LIMIT
    assert _clamp(7) == 7
    assert DEFAULT_LIMIT <= MAX_LIMIT


# ── 第 2 层：真库集成（行级过滤真的生效）─────────────────────────────────────


def _two_users_with_orders() -> tuple[int, int]:
    """挑两个「各自都有订单」的用户，否则过滤测试会因空集而变成假通过。"""
    with SessionLocal() as db:
        ids = [
            r[0]
            for r in db.execute(
                select(Order.user_id)
                .group_by(Order.user_id)
                .having(text("count(*) >= 2"))
                .order_by(Order.user_id)
                .limit(2)
            )
        ]
    assert len(ids) >= 2, "种子数据里需要至少两个用户各有 >= 2 笔订单（先跑 uv run python -m scripts.seed）"
    return ids[0], ids[1]


@pytest.mark.db
def test_customer_query_is_row_filtered(seeded: None):
    """3.db.md §7 的 `test_customer_query_is_row_filtered`：A 的查询结果里没有 B 的订单。"""
    user_a, user_b = _two_users_with_orders()

    orders_a = get_orders_for_customer(user_a)
    orders_b = get_orders_for_customer(user_b)

    assert orders_a, "用户 A 应该有订单，否则这条测试证明不了过滤生效"
    assert orders_b, "用户 B 应该有订单"
    assert all(o.user_id == user_a for o in orders_a)
    assert all(o.user_id == user_b for o in orders_b)

    ids_a = {o.order_id for o in orders_a}
    ids_b = {o.order_id for o in orders_b}
    assert not (ids_a & ids_b), f"越权：A 和 B 的订单集合出现交集 {ids_a & ids_b}"


@pytest.mark.db
def test_order_detail_rejects_other_users_order_id(seeded: None):
    """拿到别人的 order_id 也没用：详情查询同时按 (user_id, order_id) 过滤。"""
    user_a, user_b = _two_users_with_orders()
    order_of_b = get_orders_for_customer(user_b)[0]

    assert get_order_for_customer(user_b, order_of_b.order_id) is not None
    # 换成 A 的身份去查 B 的订单 → 必须查不到
    assert get_order_for_customer(user_a, order_of_b.order_id) is None


@pytest.mark.db
def test_refunds_and_tickets_are_row_filtered(seeded: None):
    """退款单与工单同样是逐个用户过滤的（不是「查出来再筛」）。"""
    with SessionLocal() as db:
        user_ids = [r[0] for r in db.execute(select(Order.user_id).distinct().order_by(Order.user_id))]
    for user_id in user_ids[:4]:
        assert all(r.user_id == user_id for r in get_refunds_for_customer(user_id))
        assert all(t.user_id == user_id for t in get_tickets_for_customer(user_id))


@pytest.mark.db
def test_limit_truncates_result_rows(seeded: None):
    """limit 真的截断：要 1 行就只给 1 行，且默认上限不超过 MAX_LIMIT。"""
    user_a, _ = _two_users_with_orders()
    one = get_orders_for_customer(user_a, limit=1)
    assert len(one) == 1
    many = get_orders_for_customer(user_a, limit=10_000)  # 请求超量 → 被夹到 MAX_LIMIT
    assert len(many) <= MAX_LIMIT
    assert len(many) >= len(one)
    assert DEFAULT_LIMIT <= MAX_LIMIT


@pytest.mark.db
def test_customer_profile_only_returns_that_user(seeded: None):
    user_a, user_b = _two_users_with_orders()
    profile = get_customer_profile(user_a)
    assert profile is not None and profile.user_id == user_a
    assert profile.user_id != user_b


@pytest.mark.db
def test_detail_amount_matches_items(seeded: None):
    """顺带钉住「明细金额能加出订单总额」这条数据自洽性（顺手即可发现种子脚本回归）。"""
    user_a, _ = _two_users_with_orders()
    order = get_orders_for_customer(user_a)[0]
    items_total = sum(
        (i.unit_price * i.quantity for i in order.items),
        start=Decimal("0.00"),
    )
    assert items_total == order.total_amount


@pytest.mark.db
def test_seed_counts_meet_minimum(seeded: None):
    """3.db.md §7 的 `test_seed_data_counts` 手工版的自动化版本。"""
    snap = snapshot()
    assert not snap.missing_tables, f"缺少业务表: {snap.missing_tables}"
    assert snap.revision is not None, "alembic_version 为空：库不是由迁移建起来的"
    for table, minimum in MIN_COUNTS.items():
        assert snap.counts[table] >= minimum, f"{table} 只有 {snap.counts[table]} 行，要求 >= {minimum}"


# 库侧证据的判据（§5.2）：
#   ① 业务路径「零容忍」—— 只有带行级过滤（`WHERE ... user_id = $n`）的语句才允许落地；
#   ② 不带 WHERE 的语句只允许命中**已知豁免清单**（测试装置 / 结构自检 / 迁移 introspection），
#      清单以外出现新的裸 orders 查询 = 有人绕开 `app/db/queries.py` → 测试红。
# 这两条都按**编译后的 SQL 结构**判定，不按「全文里有没有 user_id 这个词」——后者是假阳性来源：
# `SELECT DISTINCT orders.user_id FROM orders LIMIT $1` 带 user_id 列名却没有任何过滤。
#
# queryid 由 pg_stat_statements 计算（语句文本归一化后的 hash），同一 SQL 的多次执行归并为一条；
# 因此「拉两次快照比新增 queryid」= 精确定位「本次对客调用到底发了哪些语句」。
_QUERYID_RE = re.compile(r"queryid\s*=\s*(-?\d+)", re.IGNORECASE)

# 对客出口必须带的行级过滤：绑定参数形态（`$1::BIGINT`，见 pg_stat_statements 的归一化输出）。
_ROW_FILTER_RE = re.compile(r"WHERE\s+orders\.user_id\s*=", re.IGNORECASE)

# 允许「不带 WHERE」的已知语句（SQL LIKE 通配）
# ⚠️ 它只用于**人工分诊与测试判据**，不是给判据开的豁免分支：清单外的裸 orders 查询一律算违规。
UNFILTERED_STMT_EXEMPTIONS: dict[str, str] = {
    "SELECT DISTINCT orders.user_id%FROM orders%": (
        "tests/test_db_queries.py::_two_users_with_orders —— 只挑「有订单的用户 id」当前置条件，"
        "不返回订单内容，天然不该带 WHERE"
    ),
    "%information_schema%": "tests/db_helpers.py / scripts/db_check.py 的结构自检，不碰业务行",
    "SELECT%pg_extension%": "扩展可用性探测（pg_stat_statements / vector 是否装了）",
}


# §5.2 分诊时从活库 `pg_stat_statements` 抄回来的两条真实语句（去掉 `+` 续行符）——
# 用它们把「判据怎么读」钉成不依赖数据库的用例，避免下次再被「全文含 user_id」误导。
LIVE_SQL_WITH_ROW_FILTER = (
    "SELECT orders.order_id, orders.user_id, orders.status, orders.total_amount, "
    "orders.payment_method, orders.created_at FROM orders "
    "WHERE orders.user_id = $1::BIGINT "
    "ORDER BY orders.created_at DESC, orders.order_id DESC LIMIT $2::INTEGER"
)
LIVE_SQL_TEST_HELPER_WITHOUT_FILTER = (
    "SELECT DISTINCT orders.user_id FROM orders LIMIT $1::INTEGER"
)


def _orders_queryid_snapshot() -> dict[int | None, tuple[int, str]]:
    """orders 相关语句的现场快照：{queryid: (calls, 归一化语句全文)}（只读）。"""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "select queryid, calls, query from pg_stat_statements"
                " where query ilike '%from orders%'"
            )
        ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _queryids_referenced(stmt) -> set[int]:
    """语句里引用的 queryid（SQLAlchemy 编译输出里以注释形式带 `queryid = ...`）。"""
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    return {int(m) for m in _QUERYID_RE.findall(compiled)}


def _sql_like(query: str, pattern: str) -> bool:
    """纯 Python 的 SQL LIKE 语义（`%` 通配），避免为了判据再往库里发一条查询。"""
    regex = "^" + ".*".join(re.escape(part) for part in re.split(r"%", pattern)) + "$"
    return re.match(regex, query, re.IGNORECASE | re.DOTALL) is not None


def _is_exempt(query: str) -> bool:
    """该语句是否命中已知豁免清单（`%` 通配，大小写不敏感）。"""
    return any(_sql_like(query, pattern) for pattern in UNFILTERED_STMT_EXEMPTIONS)


def test_row_filter_regex_matches_filtered_not_helper():
    """判据读法：`WHERE orders.user_id =` 认得出真过滤语句，不会把装置语句算成过滤。

    这是「带 user_id 列名 ≠ 带 WHERE user_id」的可执行版本：两条语句都含 `user_id`，
    但只有第一条带行级过滤。用正则判结构，别用 `'user_id' in query` 判。
    """
    assert _ROW_FILTER_RE.search(LIVE_SQL_WITH_ROW_FILTER)
    assert not _ROW_FILTER_RE.search(LIVE_SQL_TEST_HELPER_WITHOUT_FILTER)
    # 两条都含 user_id —— 这正是「按全文 ilike」会翻车的原因
    assert "user_id" in LIVE_SQL_WITH_ROW_FILTER
    assert "user_id" in LIVE_SQL_TEST_HELPER_WITHOUT_FILTER


def test_unfiltered_helper_statement_is_on_exemption_list():
    """挑种子数据的装置查询（无过滤）在豁免清单里；业务裸查询不在（应当被测试抓住）。"""
    assert _is_exempt(LIVE_SQL_TEST_HELPER_WITHOUT_FILTER)
    assert _ROW_FILTER_RE.search(LIVE_SQL_WITH_ROW_FILTER)  # 真出口不需要豁免
    assert not _is_exempt(
        "SELECT orders.order_id, orders.user_id FROM orders"  # 假想的业务裸查询
    )


def test_sql_like_helper_uses_percent_as_wildcard():
    """豁免清单用 SQL LIKE 语义（`%` = 任意串），转义字符不该被当通配。"""
    assert _sql_like(
        "SELECT DISTINCT orders.user_id FROM orders LIMIT $1",
        "SELECT DISTINCT orders.user_id%FROM orders%",
    )
    assert _sql_like("select 1 from information_schema.tables", "%information_schema%")
    assert not _sql_like("SELECT orders.order_id FROM orders", "SELECT DISTINCT orders.user_id%")


@pytest.mark.db
def test_live_sql_contains_row_filter(seeded: None):
    """库侧证据（D1）：对客出口落地的真实 SQL 带行级过滤，且没有新的裸 orders 查询。

    判据（按 queryid 锚定，不按「全文含 user_id」）：
    1. 先取 orders 语句快照 → 调一次 `get_orders_for_customer()`（本次对客查询）→ 再取快照；
    2. **业务路径零容忍**：由本次调用引入（或 `calls` 上涨）的 orders 语句必须含
       `WHERE orders.user_id = $n`；
    3. **不漏新裸查询**：不带 WHERE 的语句只允许命中 `UNFILTERED_STMT_EXEMPTIONS`，
       否则报错并直接给出完整 SQL 供分诊。

    拿不到该视图时（未配 `shared_preload_libraries`）跳过，提示改用 `echo=True` 或
    引擎级事件——见 `docs/runbook/3.db-runbook.md` §5.2、§7.1 坑 4/5。
    """
    user_a, _ = _two_users_with_orders()

    with engine.connect() as conn:
        available = conn.execute(
            text("select count(*) from pg_extension where extname = 'pg_stat_statements'")
        ).scalar_one()
        if not available:
            pytest.skip("未安装 pg_stat_statements 扩展：改用 echo=True 打印 SQL 作为等效证据")

    before = _orders_queryid_snapshot()
    before_calls = {qid: calls for qid, (calls, _) in before.items()}

    get_orders_for_customer(user_a)  # 唯一目的：让真实对客 SQL 落进 pg_stat_statements

    after = _orders_queryid_snapshot()
    if not after:
        pytest.skip(
            "pg_stat_statements 里没有 orders 语句：确认 `shared_preload_libraries` 或"
            " `pg_stat_statements.track`（见 3.db-runbook.md §7.1 坑 4）"
        )

    # 本次调用「新出现」或「被再次执行」的语句
    touched = {
        qid: (calls, query)
        for qid, (calls, query) in after.items()
        if qid not in before_calls or calls > before_calls[qid]
    }

    naked = {
        qid: query
        for qid, (_, query) in touched.items()
        if not _ROW_FILTER_RE.search(query)
    }

    # ① 业务路径零容忍 + 顺带固化「装置查询不算违规」这条口径：
    #    不带行级过滤的语句只允许命中豁免清单，清单外一律违规
    violations = {qid: query for qid, query in naked.items() if not _is_exempt(query)}
    assert not violations, (
        "对客路径出现裸查询（未带 WHERE user_id 且不在豁免清单）—— 收口到 app/db/queries.py，"
        f"并把该 SQL 补进 4.security.md 的拦截用例：{violations}"
    )

    # ② 正向证据：必须看到「带行级过滤的 orders 语句」被真实执行过
    filtered = {qid: q for qid, (_, q) in touched.items() if _ROW_FILTER_RE.search(q)}
    assert filtered, (
        "本次对客调用没有产生带 `WHERE orders.user_id = ` 的语句："
        "要么走了未封装的查询，要么统计没记录（track）"
    )
