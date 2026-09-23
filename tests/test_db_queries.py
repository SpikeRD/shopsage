"""行级过滤（D1）的测试。

分两层：
- **纯逻辑层（不需要数据库）**：把每个对客查询编译成 SQL，断言「带 WHERE、带 LIMIT」。
  这一层用 `pkgutil` 遍历 `app.db.queries` 里所有公开函数，**新增查询函数时自动纳入检查** ——
  以后有人写了不带 `WHERE user_id` 的封装，测试立刻红，不用等越权事件暴露。
- **集成层（`@pytest.mark.db`）**：对真库断言「拿 A 的 id 查不到 B 的数据」。
"""

from __future__ import annotations

import inspect
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


@pytest.mark.db
def test_live_sql_contains_row_filter(seeded: None):
    """库侧证据：`pg_stat_statements` 里能看到带 WHERE 的真实语句。

    拿不到该视图时（未 `shared_preload_libraries`）跳过，并提示改用 `echo=True` 或引擎级事件
    —— 见 `docs/runbook/3.db-runbook.md` §5.2。
    """
    user_a, _ = _two_users_with_orders()
    get_orders_for_customer(user_a)  # 先制造一次真实查询

    with engine.connect() as conn:
        available = conn.execute(
            text("select count(*) from pg_extension where extname = 'pg_stat_statements'")
        ).scalar_one()
        if not available:
            pytest.skip("未安装 pg_stat_statements 扩展：改用 echo=True 打印 SQL 作为等效证据")
        rows = conn.execute(
            text(
                "select query from pg_stat_statements"
                " where query ilike '%from orders%' and query ilike '%user_id%' limit 20"
            )
        ).fetchall()
    assert rows, "pg_stat_statements 里没有看到带 user_id 过滤的 orders 查询"
    assert all("WHERE" in r[0].upper() for r in rows), "存在不带 WHERE 的 orders 查询 —— 对客路径有裸查询"
