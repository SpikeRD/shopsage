"""种子数据：灌入电商演示数据（第 3 步 `3.db.md` 第 6 条）。

用法（仓库根，必须模块形式）：
    uv run python -m scripts.seed              # 清空业务表后重新灌（幂等）
    uv run python -m scripts.seed --no-truncate  # 保留现有数据，只做缺失补齐（不推荐）
    uv run python -m scripts.seed --seed 42     # 换一组确定性随机数据

三条硬要求（`docs/runbook/3.db-runbook.md` §4）：
1. **幂等**：重复执行不炸、不重复灌 —— 默认先 `TRUNCATE ... RESTART IDENTITY CASCADE`。
   这也是 A2「断点重放不重复下单」的前置训练。
2. **自洽**：`orders.total_amount` = 明细之和；`refunds.amount` <= 订单金额；
   `products.stock` 与 `stock_movements` 流水对得上。数据自相矛盾时，Text-to-SQL 演示里
   「模型算错了」和「数据本身就错」根本分不清。
3. **量级够小**：几十行量级，够演示即可；百万行那是第 6 步压测（C1）的事。

⚠️ `TRUNCATE` 会删数据，只允许在开发/演示库上跑；生产库不要执行本脚本。
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.db.models import (
    CUSTOMER_LEVELS,
    ORDER_STATUSES,
    PAYMENT_METHODS,
    REFUND_STATUSES,
    Employee,
    Order,
    OrderItem,
    Product,
    Refund,
    StockMovement,
    Ticket,
    User,
)
from app.db.session import SessionLocal
from scripts.db_check import BUSINESS_TABLES, MIN_COUNTS, snapshot

# 固定随机种子：同一版本代码 + 同一 seed ⇒ 同一批数据（可复现，diff 友好）
DEFAULT_SEED = 20260923
TZ = ZoneInfo("Asia/Shanghai")

SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨"
GIVEN = "伟芳娜敏静秀丽强磊洋艳勇军杰娟涛明超霞平刚桂英"
CATEGORIES = {
    "手机数码": (1299, 8999),
    "家用电器": (299, 5999),
    "服饰鞋包": (59, 1299),
    "食品生鲜": (9, 299),
    "图书文娱": (19, 199),
    "运动户外": (99, 1999),
}
ADJECTIVES = ("轻享", "臻选", "悦动", "云感", "智联", "经典", "城市", "极简")
REFUND_REASONS = ("七天无理由", "商品破损", "发错货", "不想要了", "质量问题", "物流太慢")
TICKET_TOPICS = ("订单状态咨询", "退款进度", "商品参数", "物流异常", "发票问题")
EMPLOYEES = (
    ("张明", "客服主管", "客户服务部", "L3"),
    ("李静", "客服专员", "客户服务部", "L1"),
    ("王强", "仓储主管", "仓储物流部", "L3"),
    ("赵敏", "数据分析师", "数据运营部", "L2"),
    ("陈磊", "运维工程师", "技术部", "L2"),
)


def _phone(rng: random.Random) -> str:
    return "1" + rng.choice("3578") + "".join(rng.choice("0123456789") for _ in range(9))


def _pick_status(rng: random.Random, index: int) -> str:
    """让状态分布可预期：最近几单还在流程中，早期订单已完成/取消。"""
    if index < 3:
        return rng.choice(("pending", "paid"))
    if index < 6:
        return "shipped"
    return rng.choices(
        ("delivered", "cancelled", "refunded"),
        weights=(80, 12, 8),
        k=1,
    )[0]


def seed(seed_value: int = DEFAULT_SEED, *, truncate: bool = True) -> dict[str, int]:
    """灌数据并返回各表行数。`truncate=True` 时先清空业务表（保证重复执行结果一致）。"""
    rng = random.Random(seed_value)
    now = dt.datetime.now(tz=TZ).replace(microsecond=0)

    with SessionLocal() as db:
        if truncate:
            _truncate_business_tables(db)

        employees = _seed_employees(db)
        users = _seed_users(db, rng, now)
        products, base_stock = _seed_products(db, rng)
        orders, order_items, stock_out = _seed_orders(db, rng, users, products, now)
        refunds = _seed_refunds(db, rng, orders, now)
        movements = _seed_stock_movements(db, rng, products, employees, base_stock, stock_out, now)
        tickets = _seed_tickets(db, rng, users, employees, now, orders)

        db.flush()
        assert len(employees) and len(users) and len(products)
        assert len(orders) == len({o.order_id for o in orders})
        assert len(refunds) and len(movements) and len(tickets)

        db.commit()
        return _count_rows(db)


def _truncate_business_tables(db: Session) -> None:
    """清空 8 张业务表并重置自增 id。**刻意不动向量库**（见下）。

    用一条 `TRUNCATE ... RESTART IDENTITY CASCADE` 而不是逐表 DELETE：
    · CASCADE 自动处理外键顺序（不用手工排 `delete()` 的先后）；
    · RESTART IDENTITY 让 id 从 1 开始 —— 重复执行后的数据**完全一致**，便于 diff；
    · 比逐表 delete 少 8 条语句，几十万行时差别明显。

    ⚠️ **第 10 步（rag）实测踩到的连带事故**：`CASCADE` 会顺着外键**删掉向量库**。
    `rag_chunks.doc_id → rag_documents.doc_id ON DELETE CASCADE` —— 而
    `TRUNCATE rag_documents CASCADE` 会把 `rag_chunks` 一起清空。也就是说：
    「跑一次 `scripts.seed.py`（或跑一次带 `seeded` 装置的全量测试）」→
    **知识库全部消失**，随后 `tests/rag/test_rag_live.py` 会以「向量库是空的」而 skip，
    而现象看起来完全像「检索坏了」。

    所以本函数显式**只 TRUNCATE 业务表**，把向量表排除在外：
    向量库的内容来自 `docs/kb`（可重新灌，但要花 embedding 的钱与时间），
    而它与种子数据**没有任何依赖关系**（业务表里没有 chunk_id 的外键，反过来也一样）。
    `tests/rag/test_rag_seed_isolation.py` 钉住这条 —— 否则下一个人会顺手把它"统一"回去。
    """
    tables = ", ".join(f'"{t}"' for t in BUSINESS_TABLES)
    db.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


def _seed_employees(db: Session) -> list[Employee]:
    rows = [Employee(name=n, role=r, dept=d, level=lv) for n, r, d, lv in EMPLOYEES]
    db.add_all(rows)
    db.flush()
    return rows


def _seed_users(db: Session, rng: random.Random, now: dt.datetime) -> list[User]:
    rows: list[User] = []
    for i in range(8):
        name = rng.choice(SURNAMES) + "".join(rng.choice(GIVEN) for _ in range(rng.randint(1, 2)))
        rows.append(
            User(
                name=name,
                phone=_phone(rng),
                email=f"user{i + 1}@example.com",
                address=f"上海市浦东新区示例路 {rng.randint(1, 999)} 号 {rng.randint(1, 30)} 室",
                level=rng.choice(CUSTOMER_LEVELS),
                created_at=now - dt.timedelta(days=rng.randint(120, 900)),
            )
        )
    db.add_all(rows)
    db.flush()
    return rows


def _seed_products(db: Session, rng: random.Random) -> tuple[list[Product], dict[int, int]]:
    """商品 + 建品时的初始入库量（后面用来生成流水）。"""
    rows: list[Product] = []
    base_qty: dict[int, int] = {}
    category_pool = list(CATEGORIES.items())
    for i in range(20):
        category, (low, high) = category_pool[i % len(category_pool)]
        price = Decimal(rng.randint(low, high)) + Decimal(rng.choice(("0.00", "0.90", "0.99")))
        rows.append(
            Product(
                name=f"{rng.choice(ADJECTIVES)}{category[:2]}{rng.randint(1, 99):02d}",
                category=category,
                price=price,
                stock=0,  # 先占位，稍后由流水推导出最终库存
                description=f"{category} 演示商品，型号 {i + 1:03d}",
            )
        )
    db.add_all(rows)
    db.flush()
    for product in rows:
        base_qty[product.product_id] = rng.randint(50, 400)
    return rows, base_qty


def _seed_orders(
    db: Session,
    rng: random.Random,
    users: list[User],
    products: list[Product],
    now: dt.datetime,
) -> tuple[list[Order], list[OrderItem], dict[int, int]]:
    """订单 + 明细。**总金额严格等于明细之和**（自洽要求）。"""
    orders: list[Order] = []
    items: list[OrderItem] = []
    stock_out: dict[int, int] = {p.product_id: 0 for p in products}

    for i in range(30):
        user = rng.choice(users)
        created = now - dt.timedelta(days=rng.randint(0, 120), hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
        status = _pick_status(rng, i)
        order = Order(
            user_id=user.user_id,
            status=status,
            total_amount=Decimal("0.00"),  # 先占位，按明细回填
            payment_method=rng.choice(PAYMENT_METHODS) if status != "pending" else None,
            created_at=created,
        )
        db.add(order)
        db.flush()

        total = Decimal("0.00")
        for product in rng.sample(products, rng.randint(1, 3)):
            qty = rng.randint(1, 4)
            # 偶尔给个折扣价，制造「下单时的单价 != 商品现价」的真实感
            # （对账必须按 unit_price 算，不能拿 products.price 现算 —— 这正是订单明细要存单价的原因）
            unit_price = (
                product.price
                if rng.random() > 0.2
                else (product.price * Decimal("0.90")).quantize(Decimal("0.01"))
            )
            item = OrderItem(
                order_id=order.order_id,
                product_id=product.product_id,
                quantity=qty,
                unit_price=unit_price,
            )
            db.add(item)
            items.append(item)
            total += unit_price * qty
            if status != "cancelled":  # 取消的订单不扣库存
                stock_out[product.product_id] += qty
        order.total_amount = total.quantize(Decimal("0.01"))
        orders.append(order)

    db.flush()
    return orders, items, stock_out


def _seed_refunds(
    db: Session, rng: random.Random, orders: list[Order], now: dt.datetime
) -> list[Refund]:
    """退款单：金额不超过订单金额；退款中的订单状态会与 order.status 对齐。"""
    refundable = [o for o in orders if o.status in ("delivered", "shipped", "refunded")]
    rows: list[Refund] = []
    for order in refundable[:8]:
        status = "refunded" if order.status == "refunded" else rng.choice(REFUND_STATUSES)
        if order.status == "refunded":
            amount = order.total_amount
        else:
            amount = (order.total_amount * Decimal(rng.choice(("0.30", "0.50", "1.00")))).quantize(Decimal("0.01"))
        rows.append(
            Refund(
                order_id=order.order_id,
                user_id=order.user_id,
                amount=max(amount, Decimal("0.01")),
                status=status,
                reason=rng.choice(REFUND_REASONS),
                created_at=order.created_at + dt.timedelta(days=rng.randint(1, 10)),
            )
        )
    db.add_all(rows)
    db.flush()
    return rows


def _seed_stock_movements(
    db: Session,
    rng: random.Random,
    products: list[Product],
    employees: list[Employee],
    base_qty: dict[int, int],
    stock_out: dict[int, int],
    now: dt.datetime,
) -> list[StockMovement]:
    """库存流水 + 回填 products.stock，使 `stock == 初始入库 - 出库 + 调整` 可对账。"""
    rows: list[StockMovement] = []
    for product in products:
        initial = base_qty[product.product_id]
        rows.append(
            StockMovement(
                product_id=product.product_id,
                change_qty=initial,
                type="in",
                operator_id=rng.choice(employees).emp_id,
                created_at=now - dt.timedelta(days=rng.randint(150, 300)),
            )
        )
        out_qty = stock_out.get(product.product_id, 0)
        if out_qty:
            rows.append(
                StockMovement(
                    product_id=product.product_id,
                    change_qty=-out_qty,
                    type="out",
                    operator_id=None,  # 出库由订单触发，没有人工操作者
                    created_at=now - dt.timedelta(days=rng.randint(0, 120)),
                )
            )
        adjust = rng.choice((0, 0, 0, 5, -3))  # 偶发盘点
        if adjust:
            rows.append(
                StockMovement(
                    product_id=product.product_id,
                    change_qty=adjust,
                    type="adjust",
                    operator_id=rng.choice(employees).emp_id,
                    created_at=now - dt.timedelta(days=rng.randint(0, 60)),
                )
            )
        stock = initial - out_qty + adjust
        product.stock = max(stock, 0)  # CHECK(stock >= 0)
    db.add_all(rows)
    db.flush()
    return rows


def _seed_tickets(
    db: Session,
    rng: random.Random,
    users: list[User],
    employees: list[Employee],
    now: dt.datetime,
    orders: list[Order],
) -> list[Ticket]:
    rows: list[Ticket] = []
    for i in range(8):
        user = rng.choice(users)
        topic = rng.choice(TICKET_TOPICS)
        rows.append(
            Ticket(
                user_id=user.user_id,
                type={
                    "订单状态咨询": "order",
                    "退款进度": "refund",
                    "商品参数": "product",
                    "物流异常": "logistics",
                    "发票问题": "other",
                }[topic],
                status=rng.choice(("open", "processing", "resolved", "closed")),
                assignee_id=rng.choice(employees).emp_id if i % 4 else None,  # 部分未分配
                created_at=now - dt.timedelta(days=rng.randint(0, 90), hours=rng.randint(0, 23)),
            )
        )
    db.add_all(rows)
    db.flush()
    return rows


def _count_rows(db: Session) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in (User, Product, Order, OrderItem, Refund, StockMovement, Ticket, Employee):
        counts[model.__tablename__] = db.execute(select(func.count()).select_from(model)).scalar_one()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="灌入电商演示种子数据（幂等）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"随机种子（默认 {DEFAULT_SEED}）")
    parser.add_argument(
        "--no-truncate",
        action="store_true",
        help="不清空现有数据（可能造成重复数据，仅排障用）",
    )
    args = parser.parse_args(argv)

    existing = snapshot()
    if existing.missing_tables:
        print(f"[FAIL] 缺少业务表: {', '.join(existing.missing_tables)}")
        print("       先建表：uv run alembic upgrade head")
        return 1

    counts = seed(args.seed, truncate=not args.no_truncate)
    print(f"种子数据已灌入（seed={args.seed}, truncate={not args.no_truncate}）：")
    for table, n in counts.items():
        flag = "" if n >= MIN_COUNTS[table] else f"  <-- 低于最小演示量 {MIN_COUNTS[table]}"
        print(f"  {table:<18}{n:>6}{flag}")

    below = {t: n for t, n in counts.items() if n < MIN_COUNTS[t]}
    if below:
        print(f"\n[WARN] 有表低于最小演示量: {below}")
        return 1
    print("\n[OK] 各表行数均达到最小演示量。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
