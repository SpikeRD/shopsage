"""8 张业务表模型（对应 `docs/architecture.md` 第 5 节 Schema）。

设计口径（与 3.db.md §4 一致）：
- 关系数据与向量数据同库不同表：这里只有业务表，向量表由第 10 步 `rag` 建。
- 对客路径的行级过滤**不在这里**，在 `queries.py` 的封装函数里（SQL 层 WHERE user_id = ...）。
- 状态/类型用 VARCHAR + CHECK，不用 PG 原生 ENUM：ENUM 加值要改类型、迁移更麻烦，
  而本项目这些取值会在开发期反复调整（架构第 5 节的取值本身就是示意）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampTZ

# ── 取值白名单（同时用于 CHECK 约束与种子数据，避免两处真相漂移）─────────────────

ORDER_STATUSES = ("pending", "paid", "shipped", "delivered", "cancelled", "refunded")
PAYMENT_METHODS = ("alipay", "wechat", "card", "cod")
REFUND_STATUSES = ("requested", "approved", "rejected", "refunded")
STOCK_MOVEMENT_TYPES = ("in", "out", "adjust", "return")
TICKET_TYPES = ("order", "refund", "product", "logistics", "other")
TICKET_STATUSES = ("open", "processing", "resolved", "closed")
EMPLOYEE_LEVELS = ("L1", "L2", "L3")
CUSTOMER_LEVELS = ("normal", "silver", "gold", "vip")
#: 队列任务种类（第 12 步；与 `app.queue.jobs.JOB_KINDS` 同源，有对账用例钉住）
QUEUE_JOB_KINDS = ("report", "agent", "kb_rebuild")
#: 账号状态（变更 001）：`disabled` 是**唯一的停用态**，不做 `locked`/`pending` ——
#: 「锁定」是登录失败计数的派生结果，「待激活」是邀请流程的派生结果，两者都是**策略**，
#: 不是身份的固有属性。加进状态机就得多处判断，而它们本来可以由一次计数或一条邀请记录回答。
ACCOUNT_STATUSES = ("active", "disabled")
#: 账号指向的业务主体类别（变更 001）。刻意**没有**第三种 `staff`：
#: 管理员就是 `employees` 里 `role='admin'` 的一行，这样审计日志能一路追到人，
#: 且 `tickets.assignee_id` / `stock_movements.operator_id` 的外键天然可用。
ACCOUNT_SUBJECT_TYPES = ("customer", "employee")

# 金额统一 NUMERIC(12, 2)：不用 float —— 金额比较/累加不能用二进制浮点。
MONEY = Numeric(12, 2)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    """生成 CHECK 表达式：`status IN ('a', 'b')`。"""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class User(Base):
    """客户（对客路径的主体；orders/refunds/tickets 都按 user_id 行级过滤）。"""

    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(128))
    address: Mapped[str | None] = mapped_column(String(255))
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="normal", server_default="normal")
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=func.now(), index=True
    )

    orders: Mapped[list[Order]] = relationship(back_populates="user", cascade="all, delete-orphan")
    refunds: Mapped[list[Refund]] = relationship(back_populates="user", cascade="all, delete-orphan")
    tickets: Mapped[list[Ticket]] = relationship(back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(_in_list("level", CUSTOMER_LEVELS), name="level_valid"),
        Index("ix_users_name", "name"),
    )


class Product(Base):
    """商品（库存 stock 与流水 stock_movements 对账）。"""

    __tablename__ = "products"

    product_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    description: Mapped[str | None] = mapped_column(Text)

    order_items: Mapped[list[OrderItem]] = relationship(back_populates="product")
    stock_movements: Mapped[list[StockMovement]] = relationship(back_populates="product")

    __table_args__ = (
        CheckConstraint("price >= 0", name="price_non_negative"),
        CheckConstraint("stock >= 0", name="stock_non_negative"),
        Index("ix_products_name", "name"),
    )


class Order(Base):
    """订单（对客查询的核心表：`WHERE user_id = :me` 就是 D1 的落点）。"""

    __tablename__ = "orders"

    order_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    total_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    payment_method: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(back_populates="order", cascade="all, delete-orphan")
    refunds: Mapped[list[Refund]] = relationship(back_populates="order", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(_in_list("status", ORDER_STATUSES), name="status_valid"),
        CheckConstraint(
            f"payment_method IS NULL OR {_in_list('payment_method', PAYMENT_METHODS)}",
            name="payment_method_valid",
        ),
        CheckConstraint("total_amount >= 0", name="total_non_negative"),
        # 对客查询固定是「本用户的订单 + 按时间倒序 + LIMIT」→ 复合索引正好覆盖这条路径
        Index("ix_orders_user_id_created_at", "user_id", "created_at"),
        Index("ix_orders_status_created_at", "status", "created_at"),
    )


class OrderItem(Base):
    """订单明细（total_amount 与它的一致性由种子脚本与测试保证）。"""

    __tablename__ = "order_items"

    order_item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.product_id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(back_populates="order_items")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        Index("ix_order_items_product_id", "product_id"),
    )


class Refund(Base):
    """退款单（对客查询同样按 user_id 行级过滤；写路径走预定义 service，不经 LLM 生成 SQL）。"""

    __tablename__ = "refunds"

    refund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="requested", server_default="requested")
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False, server_default=func.now())
    # 幂等键（第 6 步 tools 落地，A2）：同一「用户 + 幂等键」的重复请求不重复建退款单。
    # 唯一性由下面的**复合唯一约束**保证（见 app/tools/refund.py 为什么必须带 user_id）。
    idempotency_key: Mapped[str | None] = mapped_column(String(64))

    order: Mapped[Order] = relationship(back_populates="refunds")
    user: Mapped[User] = relationship(back_populates="refunds")

    __table_args__ = (
        CheckConstraint(_in_list("status", REFUND_STATUSES), name="status_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_refunds_user_id_created_at", "user_id", "created_at"),
        Index("ix_refunds_order_id", "order_id"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_refunds_user_id_idempotency_key"),
    )


class StockMovement(Base):
    """库存流水（变更审计：products.stock 的任何变化都要在这里留痕）。"""

    __tablename__ = "stock_movements"

    movement_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.product_id", ondelete="RESTRICT"), nullable=False
    )
    change_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    operator_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("employees.emp_id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False, server_default=func.now())

    product: Mapped[Product] = relationship(back_populates="stock_movements")
    operator: Mapped[Employee | None] = relationship(back_populates="stock_movements")

    __table_args__ = (
        CheckConstraint(_in_list("type", STOCK_MOVEMENT_TYPES), name="type_valid"),
        CheckConstraint("change_qty <> 0", name="change_qty_non_zero"),
        Index("ix_stock_movements_product_id_created_at", "product_id", "created_at"),
    )


class Ticket(Base):
    """工单（写路径示例：工单状态变更走预定义 service API，不允许 LLM 动态生成 SQL）。"""

    __tablename__ = "tickets"

    ticket_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", server_default="open")
    assignee_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("employees.emp_id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False, server_default=func.now())
    # 幂等键（第 6 步 tools 落地，A2）：同一「用户 + 幂等键」的重复请求不重复建工单。
    # 唯一性由下面的**复合唯一约束**保证（见 app/tools/ticket.py 为什么必须带 user_id）。
    idempotency_key: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="tickets")
    assignee: Mapped[Employee | None] = relationship(back_populates="tickets")

    __table_args__ = (
        CheckConstraint(_in_list("type", TICKET_TYPES), name="type_valid"),
        CheckConstraint(_in_list("status", TICKET_STATUSES), name="status_valid"),
        Index("ix_tickets_user_id_created_at", "user_id", "created_at"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_tickets_user_id_idempotency_key"),
    )


class Employee(Base):
    """内部员工（对内路径的 RBAC 主体：level 决定能看到哪些聚合数据）。"""

    __tablename__ = "employees"

    emp_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    dept: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(8), nullable=False)

    tickets: Mapped[list[Ticket]] = relationship(back_populates="assignee")
    stock_movements: Mapped[list[StockMovement]] = relationship(back_populates="operator")

    __table_args__ = (CheckConstraint(_in_list("level", EMPLOYEE_LEVELS), name="level_valid"),)


class QueueDeadLetter(Base):
    """死信表（第 12 步 queue）：**长任务失败之后的唯一持久留痕处**。

    为什么任务本体留在 Redis、只有失败才落库（`12.queue.md` §4 第 4 条）：

    * **成功的任务不值得一行数据库记录**：它每天可能几十万条，而它的价值只有
      「结果」——那已经在 Redis 里（TTL 24h）且已经回给了调用方；
    * **失败的任务必须留下来**：它是「零任务丢失」这条验收的落点，也是人工兜底的入口。
      而「人工」意味着**人能筛、能查、能标记已处理** —— Redis 的字符串结果做不到这些。

    字段口径（每个都对应一次排障）：

    | 列 | 用途 |
    |---|---|
    | `job_id` | 唯一键：重放的幂等键 + 与 Redis 的对应关系 |
    | `function` / `kind` / `payload` | **重放需要的一切**（原样喂回任务） |
    | `attempts` | 试了几次（区分「一次就永久失败」与「三次都恢复不了」） |
    | `error_type` / `error_message` / `error_traceback` | 出了什么错、在哪一行 |
    | `retryable` | 「运气不好」还是「代码坏了」（决定该不该重放） |
    | `policy` | 当时用的是哪套退避策略（策略改了之后回看旧记录仍然说得通） |
    | `resolution` / `resolved_at` / `replay_count` / `replayed_job_id` | 人工处理的结果 |

    ⚠️ `resolution` 是**文本**而不是布尔位：收场方式不止一种
    （`replayed_ok` / `replayed_failed` / `wont_fix` / `manual_fix`），
    布尔位会逼着后来的人往别处记「怎么处理的」。
    """

    __tablename__ = "queue_dead_letters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: 幂等键：同一 job_id 只留一行（重投后再失败 → 更新失败现场，不新增一行）
    job_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    function: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 任务入参（重放时原样喂回）。JSONB 而不是 Text：能按字段查、写入时即校验。
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    error_type: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: 完整栈（截断到 4000 字符，见 `app/queue/core.traceback_text`）——
    #: 它是**唯一**能看到「错在哪一行」的地方（Redis 的失败结果会过期）。
    error_traceback: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    #: 当时的退避策略（JSONB：字段与 `jobs.RetryPolicy` 一一对应）
    policy: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    enqueued_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ)
    failed_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False, server_default=func.now())
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    replay_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: 最近一次重放用的 job_id（追「这条死信是被哪次执行补上的」）
    replayed_job_id: Mapped[str] = mapped_column(String(96), nullable=False, default="", server_default="")
    #: 收场方式（空 = 还没人处理）：replayed_ok / replayed_failed / wont_fix / manual_fix
    resolution: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    resolved_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ)

    __table_args__ = (
        CheckConstraint(_in_list("kind", QUEUE_JOB_KINDS), name="kind_valid"),
        CheckConstraint("attempts >= 1", name="attempts_positive"),
        CheckConstraint("replay_count >= 0", name="replay_count_non_negative"),
        # 排障的主查询：「最近还有哪些没处理」→ (resolution, failed_at)
        Index("ix_queue_dead_letters_resolution_failed_at", "resolution", "failed_at"),
        Index("ix_queue_dead_letters_kind_failed_at", "kind", "failed_at"),
    )


class Account(Base):
    """账号（变更 001）：**「凭据 → 身份」的唯一真相**。

    为什么单独一张表、而不是给 `users` / `employees` 各加口令列（决策记录 §9 第 1 条）：

    * 登录只需要**一条**查询路径。分叉成「先查客户、再查员工」就等于两处真相，
      而两处真相的典型故障是「客户侧加了口令校验、员工侧忘了加」——它不会有任何报错；
    * `users.role` 会直接与对客路径的语义打架：`WHERE user_id = :me` 这条行级过滤
      假定「客户表里只有客户」。一旦客户表里出现 `role='admin'` 的行，
      「这个身份该不该被过滤」就变成每个查询都要重新回答的问题；
    * 停用、改角色、重置口令都只动一行，审计路径也只有一条。

    ⚠️ **`subject_id` 不能声明外键**（这是本表唯一的硬伤，必须显式记住）：
    PostgreSQL 不支持「指向多张表之一」的外键，所以 `(customer → users.user_id)` 与
    `(employee → employees.emp_id)` 都只能靠应用层校验。**代价**是脏数据（指向不存在的客户）
    不会被数据库拦住；**换来**的是不引入第三种身份类别（见 `ACCOUNT_SUBJECT_TYPES` 的注释）。
    应用层的建号/改号路径必须校验主体存在，且要有对账用例兜住。

    ⚠️ **`role` 与 `employees.role` 同名但权威性不同**（变更单 §3.1）：
    `accounts.role` 是**授权**真相（写进令牌、查 `rbac.ROLE_PERMISSIONS`）；
    `employees.role` 是**组织**属性（部门/岗位口径）。鉴权路径**只读本表**。
    两者由建号逻辑保持同步，并有一条对账用例钉住 —— 否则将来必出现「改了一个没生效」。

    ⚠️ **`password_hash` 绝不允许进 SQL 白名单**：`app/security/policy.ALLOWED_TABLES`
    是给 LLM 自由生成 SQL 用的表清单，本表**不在其中**（有测试断言这一点）。
    口径与 `users` 的 PII 列一致：账号凭据只能经应用代码读写，不能经文本转 SQL 读到。
    """

    __tablename__ = "accounts"

    account_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: 登录标识：员工填用户名（`zhangsan`），客户填手机号。**唯一**，且大小写敏感性交给数据库默认
    #: （PostgreSQL 默认区分大小写；登录时由应用层统一小写化，见 `app/security/accounts.py`）。
    identifier: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: 口令散列，**自描述格式**：`argon2$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>`，
    #: 兼容旧值 `sha256$<salt>$<digest>`（登录成功后原地升级为 argon2，见 `auth.py`）。
    #: 把算法名与参数一起编码进结果，是为了换算法时能按前缀识别旧口令并平滑迁移。
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 授权角色。取值必须是 `rbac.ROLE_PERMISSIONS` 的键（未登记角色 → 零权限，默认拒绝）。
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: **令牌世代号**（变更单 §3.4）。停用/改密/改角色时 `+1`，于是所有在途令牌
    #: （access 与 refresh 都带 `ver` 声明）在下一次鉴权时立刻失效。
    #:
    #: 为什么需要它而不是「只查 status」：只查 status 能挡住登录，但**挡不住已经发出去的
    #: refresh 令牌继续换 access** —— 而那正是「停用要立即生效」想消灭的窗口。
    #: 它的成本是零：鉴权路径本来就要查这一行，同一次查询顺带取回来。
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ)

    __table_args__ = (
        CheckConstraint(_in_list("status", ACCOUNT_STATUSES), name="status_valid"),
        CheckConstraint(_in_list("subject_type", ACCOUNT_SUBJECT_TYPES), name="subject_type_valid"),
        CheckConstraint("token_version >= 0", name="token_version_non_negative"),
        # 一个业务主体最多一个账号（防止「同一个客户两条凭据」这种谁都不知道哪条有效的状态）。
        # ⚠️ **不要再为 `(subject_type, subject_id)` 声明 `Index`**（第一版就是这么写的，已实测踩到）：
        # 这条唯一约束在 PostgreSQL 里就是一个唯一 B-tree，已经覆盖「按主体查账号」这条路径。
        # 额外声明索引不是「多一层保险」而是**错的**：`compare_metadata` 按模型口径比对，
        # 会认为库里少了一个索引，于是人工 autogenerate 生成一条多余的 `CREATE INDEX`
        # 而自动化测试（`test_no_pending_migration_against_models`）同时判为不一致。
        # 唯一约束 = 约束 + 索引，一件事只有一个落点。
        UniqueConstraint("subject_type", "subject_id", name="uq_accounts_subject_type_subject_id"),
    )


# `db.md` 第 2 节的 import 清单要能一次性拿到 8 张表
BUSINESS_MODELS = (User, Product, Order, OrderItem, Refund, StockMovement, Ticket, Employee)

#: **非业务**表：属于基础设施而非领域模型。
#:
#: 为什么分开列而不是塞进 `BUSINESS_MODELS`：那条元组的语义是「8 张业务表」
#: （架构第 5 节的 Schema），而 `tests/test_db_migrations.py` 与 `scripts/db_check.py`
#: 用它的**长度**做断言。把它混进去会让「业务表数量对不对」这条检查失去意义 ——
#: 队列是基础设施，它加表不该改动业务 Schema 的口径。
#:
#: 第 12 步在这里只有 `QueueDeadLetter`（一张**有 ORM 模型**的基础设施表）；
#: 第 10 步的 `rag_*` 与第 13 步的 `cache_*` 也是基础设施，但它们是**纯 DDL 建的、
#: 没有 ORM 模型**（`app/cache/schema.py` 里就是一段 DDL 字符串，
#: 与 `app/rag` 同一种做法），所以不在这条元组里 ——
#: 这也是为什么 `MODEL_TABLES == BUSINESS_MODELS + INFRA_MODELS` 只在
#: 「**有 ORM 模型的**表」这个口径上成立。
#:
#: 变更 001 的 `Account` 属于**身份基础设施**：它不参与「8 张业务表」的口径，
#: 但它是本仓的 ORM 模型，所以必须进这条元组（否则
#: `tests/test_db_migrations.py` 的表数断言会红）。
INFRA_MODELS = (QueueDeadLetter, Account)

ALL_MODELS = BUSINESS_MODELS + INFRA_MODELS

__all__ = [
    "ACCOUNT_STATUSES",
    "ACCOUNT_SUBJECT_TYPES",
    "ALL_MODELS",
    "Account",
    "Base",
    "BUSINESS_MODELS",
    "CUSTOMER_LEVELS",
    "EMPLOYEE_LEVELS",
    "Employee",
    "INFRA_MODELS",
    "ORDER_STATUSES",
    "Order",
    "OrderItem",
    "PAYMENT_METHODS",
    "Product",
    "QUEUE_JOB_KINDS",
    "QueueDeadLetter",
    "REFUND_STATUSES",
    "Refund",
    "STOCK_MOVEMENT_TYPES",
    "StockMovement",
    "TICKET_STATUSES",
    "TICKET_TYPES",
    "Ticket",
    "User",
]
