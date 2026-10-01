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

from sqlalchemy import (
    BigInteger,
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


# `db.md` 第 2 节的 import 清单要能一次性拿到 8 张表
BUSINESS_MODELS = (User, Product, Order, OrderItem, Refund, StockMovement, Ticket, Employee)

__all__ = [
    "Base",
    "CUSTOMER_LEVELS",
    "EMPLOYEE_LEVELS",
    "Employee",
    "ORDER_STATUSES",
    "Order",
    "OrderItem",
    "PAYMENT_METHODS",
    "Product",
    "REFUND_STATUSES",
    "Refund",
    "STOCK_MOVEMENT_TYPES",
    "StockMovement",
    "TICKET_STATUSES",
    "TICKET_TYPES",
    "Ticket",
    "User",
    "BUSINESS_MODELS",
]
