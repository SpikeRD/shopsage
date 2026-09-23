"""种子数据与数据自洽性的测试（`3.db.md` §7 的 `test_seed_data_counts` 及其扩展）。

种子数据的价值不只是「有数据能演示」：它是**唯一**能覆盖「金额能对账、库存能推回」
这类跨表不变量的地方。这里把每条不变量都写成断言，种子脚本一改坏就立刻红。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.db.models import Employee, Order, OrderItem, Product, Refund, StockMovement, Ticket, User
from app.db.session import SessionLocal
from scripts.db_check import MIN_COUNTS, snapshot
from scripts.seed import seed

pytestmark = pytest.mark.db


def test_seed_data_counts(seeded: None):
    """3.db.md §7：各表行数 >= 最小演示量。"""
    snap = snapshot()
    assert not snap.missing_tables, f"缺少业务表: {snap.missing_tables}"
    below = {t: (snap.counts[t], m) for t, m in MIN_COUNTS.items() if snap.counts[t] < m}
    assert not below, f"这些表低于最小演示量（实际, 要求）: {below}"


def test_order_total_equals_sum_of_items(seeded: None):
    """`orders.total_amount` 必须等于明细之和（按 unit_price 算，不是商品现价）。"""
    with SessionLocal() as db:
        mismatched = db.execute(
            text(
                "select count(*) from orders o"
                " join (select order_id, sum(quantity * unit_price) s from order_items group by 1) i"
                "   on i.order_id = o.order_id"
                " where o.total_amount <> i.s"
            )
        ).scalar_one()
    assert mismatched == 0, f"有 {mismatched} 笔订单的总额与明细之和对不上"


def test_every_order_has_items(seeded: None):
    """没有明细的订单是脏数据（会让「按明细算的业务指标」静默少算）。"""
    with SessionLocal() as db:
        orphan = db.execute(
            text("select count(*) from orders o where not exists (select 1 from order_items i where i.order_id = o.order_id)")
        ).scalar_one()
    assert orphan == 0


def test_refund_amount_never_exceeds_order_total(seeded: None):
    """退款金额不能超过订单金额，且退款单的 user_id 必须与订单一致（否则行级过滤会对错人）。"""
    with SessionLocal() as db:
        overpaid = db.execute(
            text(
                "select count(*) from refunds r join orders o using (order_id)"
                " where r.amount > o.total_amount"
            )
        ).scalar_one()
        if overpaid:
            pytest.fail(f"有 {overpaid} 条退款金额超过订单金额")
        # 注意：`join orders r using (order_id)` 里 r.user_id 与 o.user_id 必须相等
        mismatched_user = db.execute(
            text("select count(*) from refunds r join orders o using (order_id) where r.user_id <> o.user_id")
        ).scalar_one()
        other_user = db.execute(
            text(
                "select count(*) from refunds r"
                " where not exists (select 1 from orders o where o.order_id = r.order_id and o.user_id = r.user_id)"
            )
        ).scalar_one()
    assert mismatched_user == 0 and other_user == 0


def test_stock_reconciles_with_movements(seeded: None):
    """`products.stock` == 该商品全部流水之和（库存能被流水推回来）。"""
    with SessionLocal() as db:
        inconsistent = db.execute(
            text(
                "select count(*) from products p"
                " join (select product_id, sum(change_qty) d from stock_movements group by 1) m"
                "   on m.product_id = p.product_id"
                " where p.stock <> m.d"
            )
        ).scalar_one()
        negative = db.execute(text("select count(*) from products where stock < 0")).scalar_one()
        idle = db.execute(
            text("select count(*) from products p where not exists (select 1 from stock_movements m where m.product_id = p.product_id)")
        ).scalar_one()
    assert inconsistent == 0, f"有 {inconsistent} 个商品的库存与流水对不上"
    assert negative == 0, "出现负库存"
    assert idle == 0, "有商品完全没有库存流水（库存变更必须留痕）"


def test_cancelled_orders_do_not_consume_stock(seeded: None):
    """已取消的订单不应产生出库流水：流水里的出库量必须 <= 未取消订单的订购量。"""
    with SessionLocal() as db:
        rows = db.execute(
            text(
                """
                with ordered as (
                    select i.product_id, sum(i.quantity) q
                    from order_items i join orders o using (order_id)
                    where o.status <> 'cancelled'
                    group by 1
                ), shipped as (
                    select product_id, -sum(change_qty) q
                    from stock_movements where type = 'out' group by 1
                )
                select count(*) from ordered
                left join shipped using (product_id)
                where coalesce(shipped.q, 0) > ordered.q
                """
            )
        ).scalar_one()
    assert rows == 0, "出库流水超过了未取消订单的订购量（取消订单被算进库存了）"


def test_seed_is_idempotent(seeded: None):
    """重复灌同一个 seed：行数与关键字段完全一致（TRUNCATE ... RESTART IDENTITY 的效果）。"""
    def fingerprint() -> tuple[dict[str, int], list[tuple[int, str, str]]]:
        with SessionLocal() as db:
            counts = {
                m.__tablename__: db.execute(select(func.count()).select_from(m)).scalar_one()
                for m in (User, Product, Order, OrderItem, Refund, StockMovement, Ticket, Employee)
            }
            users = db.execute(select(User.user_id, User.name, User.phone).order_by(User.user_id)).all()
            return counts, [tuple(r) for r in users]

    first_counts, first_users = fingerprint()
    seed()  # 再灌一遍（默认 truncate）
    second_counts, second_users = fingerprint()
    assert first_counts == second_counts, f"重复执行后行数变了: {first_counts} -> {second_counts}"
    assert first_users == second_users, "重复执行后用户数据变了（说明不幂等）"


def test_seed_respects_check_constraints(seeded: None):
    """合法性由数据库 CHECK 约束兜底：这里确认库里的值都在白名单内。"""
    with SessionLocal() as db:
        bad_orders = db.execute(
            text("select count(*) from orders where status not in ('pending','paid','shipped','delivered','cancelled','refunded')")
        ).scalar_one()
        bad_moves = db.execute(
            text("select count(*) from stock_movements where type not in ('in','out','adjust','return')")
        ).scalar_one()
        bad_refunds = db.execute(text("select count(*) from refunds where amount <= 0")).scalar_one()
    assert (bad_orders, bad_moves, bad_refunds) == (0, 0, 0)


def test_total_amount_is_numeric_not_float(seeded: None):
    """金额必须是精确十进制：累加 100 次也不该出现浮点误差。"""
    with SessionLocal() as db:
        total = db.execute(select(func.sum(Order.total_amount))).scalar_one()
    assert isinstance(total, Decimal), f"总金额类型是 {type(total)}，应为 Decimal（NUMERIC 列）"


def test_every_table_reachable_through_orm(seeded: None):
    """8 张表都能用 ORM 查（模型与库结构没有对不上）。"""
    with SessionLocal() as db:
        for model in (User, Product, Order, OrderItem, Refund, StockMovement, Ticket, Employee):
            assert db.execute(select(func.count()).select_from(model)).scalar_one() > 0, model.__tablename__
