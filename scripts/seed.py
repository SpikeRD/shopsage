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
import os
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
    Account,
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

        # 账号（变更 001）：必须在业务主体建好之后 —— `create_account` 会校验
        # `subject_id` 在 `users` / `employees` 里真的存在（`accounts` 没有外键，
        # 补偿点只有应用层那一条，见 `app/security/accounts._assert_subject_exists`）。
        account_rows = _seed_accounts(db) if _accounts_seed_enabled() else []

        db.commit()
        counts = _count_rows(db)
        counts["accounts"] = len(account_rows)
        return counts


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


# ── 演示账号（变更 001 / T12）────────────────────────────────────────────────
#
# 为什么需要：变更 001 之前「登录」是 `scripts/dev_token.py` 现场签发令牌，
# 系统里根本没有账号这个概念。改成真登录之后，**第一次搭环境时必须有账号能登进去**，
# 否则你面对的是一个「有个登录页、但没有任何凭据」的系统，而唯一的出路是先去
# 写一段脚本手动建管理员 —— 那正是「演示启动成本高」的经典形态。
#
# ## 三条口径
#
# 1. **口令写在这里，但只是演示口令**：本脚本的 docstring 已经写着「只允许在开发/演示库上跑」。
#    真实部署（`ENV=prod`）请用 `ACCOUNT_SEED_ENABLED=false` 关掉，或者建完号立刻改口令。
# 2. **口令不打印**：`main()` 只打印「已灌 N 个演示账号」与标识/角色，**不打印口令**。
#    理由与 `app/security/accounts.reset_password` 的 docstring 同源：口令一旦落到
#    终端输出/日志/截图里，它就等价于泄露了。要口令就去看本文件（它是源码，不是运行日志）。
# 3. **先删后建，保证幂等**：账号指向 `users.user_id` / `employees.emp_id`，而业务表是
#    `TRUNCATE ... RESTART IDENTITY` 重建的 —— id 会被复用。若不先清账号，
#    第二次执行必然撞 `uq_accounts_subject_type_subject_id`（表现为「种子脚本第二次跑就炸」）。
#    刻意**只删账号、不动业务表**（`_truncate_business_tables` 也不动账号表）：两者的
#    生命周期分开管，谁都不许顺手清对方。

#: 演示账号：(标识, 口令, 角色, 主体类别, 主体 id)。客户 2 / 3 与员工 1 / 2 是种子数据里必然存在的。
DEMO_ACCOUNTS: tuple[tuple[str, str, str, str, int], ...] = (
    # 管理员：员工表里 role='admin' 的那一行（变更单决策 9 = A：不引入第三种身份）
    ("admin", "admin-demo-pass", "admin", "employee", 1),
    # 演示客户：`api_demo.py` / live 用例一直用 user_id=2，这里对齐它
    ("13800000002", "customer-demo-pass", "customer", "customer", 2),
    # 第二个客户：**限流场景需要两个不同 user_id** 才能演示「各有一份额度」
    # （桶键里带 user_id，见 `app/api/ratelimit.py`）。少一个这条演示就退化成自说自话。
    ("13800000003", "customer-demo-pass", "customer", "customer", 3),
    # 演示内部员工：客户服务部主管，能进对内入口（可读全站订单）
    ("employee", "employee-demo-pass", "internal_employee", "employee", 2),
)

#: 环境变量：是否灌演示账号（默认开 —— 本脚本本来就只在 dev 跑；prod 部署时显式关掉）
ACCOUNT_SEED_ENV = "ACCOUNT_SEED_ENABLED"


def _accounts_seed_enabled() -> bool:
    """读 `.env` / 环境变量里的开关。默认 `true`（见 `ACCOUNT_SEED_ENV` 的注释）。"""
    value = str(os.getenv(ACCOUNT_SEED_ENV, "true") or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _seed_accounts(db: Session) -> list[Account]:
    """清掉旧账号再灌演示账号。**调用方负责 commit。**

    ⚠️ 全量 `delete(Account)` 而不是「只删演示那几个标识」：业务表已经被 TRUNCATE 过，
    旧的 `subject_id` 指向的客户/员工已经不存在了 —— 留着它们就是**孤儿账号**
    （能登录、但按一个不存在的 user_id 过滤，永远查不到东西，而报错只会说「你没有订单」）。
    这也是 `create_account` 会在应用层校验主体存在的原因（`accounts.subject_id` 没有外键）。
    """
    db.execute(delete(Account))
    db.flush()
    return sync_demo_accounts(db)


def sync_demo_accounts(db: Session) -> list[Account]:
    """确保 `DEMO_ACCOUNTS` 里的账号**都存在且口令/角色正确**（幂等，不删别的账号）。

    为什么需要「幂等补齐」这个模式，而不是只有全量重建（`_seed_accounts`）：

    - `scripts/seed.py` 会 `TRUNCATE ... RESTART IDENTITY` 业务表，`subject_id` 全部重来，
      所以它必须先清空账号再重建（否则旧账号指向不存在的客户）；
    - **测试装置**（`tests/api/conftest.py`）则需要「补一个演示账号，但别动库里其它东西」——
      它跑在一次全量测试的中间，把账号表清空会影响其它用例（尤其是那些临时账号）。

    两者共用同一份 `DEMO_ACCOUNTS` 口径，所以「演示账号是什么」只有一处真相。
    口令**每次都重设**（不判断「已存在就跳过」）：跑过一轮「改口令」的用例之后，
    演示账号的口令可能已经被改掉，而下一个用例仍期望它可用 —— 那会变成顺序依赖。
    """
    from app.security.accounts import (
        AccountError,
        create_account,
        get_account_by_identifier,
        reset_password,
        update_account,
    )

    rows: list[Account] = []
    for identifier, password, role, subject_type, subject_id in DEMO_ACCOUNTS:
        existing = get_account_by_identifier(db, identifier)
        if existing is None:
            try:
                view = create_account(
                    db,
                    identifier=identifier,
                    password=password,
                    role=role,
                    subject_type=subject_type,
                    subject_id=subject_id,
                )
            except AccountError:
                # 该主体已经被别的账号占了（例如某个用例刚造了一个临时账号）→ 跳过这一条。
                # 这是**补齐**语义：目标是「演示账号可用」，不是「账号表里只有这几行」。
                continue
            view_id = view.account_id
        else:
            view_id = existing.account_id

        row = db.get(Account, view_id)
        if row is not None:
            # 口令可能被别的用例改过 → 重设（同时会自增 token_version，那正是我们想要的：
            # 任何在途令牌都不该在「刚灌完种子」之后还能用）
            reset_password(db, view_id, password)
            update_account(
                db,
                view_id,
                # 这里借 0 当操作者：自我保护的判据是「是不是最后一个管理员」，
                # 而它只在**会减少**管理员时才触发。演示账号的补齐不该被那条规则挡住，
                # 所以显式传一个不等于任何账号的 actor（0）—— 见 update_account 的 docstring。
                actor_account_id=0,
                role=role,
                status="active",
            )
            db.flush()
            rows.append(db.get(Account, view_id))
    db.flush()
    return [row for row in rows if row is not None]




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
        # ⚠️ 账号表**不是业务表**（它在 `db_check.INFRA_TABLES` 里），`MIN_COUNTS` 查不到它。
        # 第一版直接 `MIN_COUNTS[table]`，于是加了账号种子之后本脚本以
        # `KeyError: 'accounts'` 结束 —— 而现象看起来像「种子脚本坏了」。
        if table not in MIN_COUNTS:
            print(f"  (基础设施) {table:<18}{n:>6}")
            continue
        flag = "" if n >= MIN_COUNTS[table] else f"  <-- 低于最小演示量 {MIN_COUNTS[table]}"
        print(f"  {table:<18}{n:>6}{flag}")

    if counts.get("accounts"):
        # ⚠️ **不打印口令**（见 `_seed_accounts` 上方那段注释）：口令一旦落到终端输出，
        # 就等价于泄露了（它会进日志、截图、聊天记录）。要看口令请直接读本文件的
        # `DEMO_ACCOUNTS`——它是源码，不是运行日志。
        print("\n演示账号（口令见 scripts/seed.py 的 DEMO_ACCOUNTS，刻意不在此打印）：")
        for identifier, _password, role, subject_type, subject_id in DEMO_ACCOUNTS:
            print(f"  {identifier:<16} role={role:<18} {subject_type}#{subject_id}")

    below = {t: n for t, n in counts.items() if t in MIN_COUNTS and n < MIN_COUNTS[t]}
    if below:
        print(f"\n[WARN] 有表低于最小演示量: {below}")
        return 1
    print("\n[OK] 各表行数均达到最小演示量。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
