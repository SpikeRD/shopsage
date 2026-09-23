"""行级过滤查询封装（D1 的数据库落点）。

**所有对客查询都必须从这里走**，不要在业务代码/LLM 生成的 SQL 里裸查表：
本模块的每个函数都固定带 `WHERE user_id = :当前用户` 与 `LIMIT`，并且默认走**只读引擎**。
- 行级过滤放在 SQL 层而不是应用层 `if`：应用层过滤＝先查出全部再截断，既慢又可能在中途泄露
  （`3.db.md` §5）；这里由数据库保证只返回该用户的数据。
- `LIMIT` 是 C1 的「结果行数截断」：Text-to-SQL 再怎么问，也拿不到超量结果。

自查「SQL 到底带没带过滤」有两条路：
- 本地/CI：`assert_row_filtered()` 直接编译语句看 SQL 文本（`tests/test_db_queries.py`）；
- 库侧：`pg_stat_statements`（`docs/runbook/3.db-runbook.md` §5.2）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Order, OrderItem, Refund, Ticket, User
from app.db.session import SessionLocal, get_read_engine

# 单次查询最多返回多少行（C1：限制返回行数）。Text-to-SQL 的 LIMIT 也复用这个值。
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# 编译后的 SQL 里必须出现 WHERE（否则就是忘了行级过滤）
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)


def _clamp_limit(limit: int, *, maximum: int = MAX_LIMIT) -> int:
    """把调用方给的 limit 夹到 [1, MAX_LIMIT]：谁都不能要求「返回全部」。"""
    if limit < 1:
        raise ValueError(f"limit 必须 >= 1，收到 {limit}")
    return min(limit, maximum)


@contextmanager
def _read_session(session: Session | None = None) -> Iterator[Session]:
    """给封装函数提供会话：调用方给了就用它的（测试/事务复用），否则自己开一个只读引擎的会话。"""
    if session is not None:
        yield session
        return
    with Session(get_read_engine()) as own:
        yield own


def assert_row_filtered(stmt: Select, *, expect_limit: bool = True) -> str:
    """编译语句并断言「带 WHERE（+ LIMIT）」，返回 SQL 文本用于测试断言/打印。

    这是把「行级过滤」从约定变成可执行检查的那一步：新写的封装函数忘了带
    `WHERE user_id = ...` 时，本地测试立刻红，而不是等越权事件暴露。
    """
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    if not _WHERE_RE.search(sql):
        raise AssertionError(f"对客查询缺少 WHERE 行级过滤：{sql}")
    if expect_limit and "LIMIT" not in sql.upper():
        raise AssertionError(f"对客查询缺少 LIMIT 截断：{sql}")
    return sql


# ── 对客路径（行级过滤 + 只读引擎 + LIMIT）────────────────────────────────────


def get_orders_for_customer(
    user_id: int, *, limit: int = DEFAULT_LIMIT, session: Session | None = None
) -> Sequence[Order]:
    """某客户的订单列表（按时间倒序，带明细）。`user_id` 就是行级过滤条件。"""
    stmt = (
        select(Order)
        .where(Order.user_id == user_id)
        .options(selectinload(Order.items))
        .order_by(Order.created_at.desc(), Order.order_id.desc())
        .limit(_clamp_limit(limit))
    )
    assert_row_filtered(stmt)
    with _read_session(session) as db:
        return db.execute(stmt).scalars().all()


def get_order_for_customer(
    user_id: int, order_id: int, *, session: Session | None = None
) -> Order | None:
    """单笔订单详情：**同时**按 user_id 与 order_id 过滤 —— 换个 order_id 也拿不到别人的单。"""
    stmt = (
        select(Order)
        .where(Order.user_id == user_id, Order.order_id == order_id)
        .options(selectinload(Order.items).selectinload(OrderItem.product))
    )
    assert_row_filtered(stmt, expect_limit=False)  # 主键等值查询，天然只有 0/1 行
    with _read_session(session) as db:
        return db.execute(stmt).scalars().one_or_none()


def get_refunds_for_customer(
    user_id: int, *, limit: int = DEFAULT_LIMIT, session: Session | None = None
) -> Sequence[Refund]:
    """某客户的退款单列表。"""
    stmt = (
        select(Refund)
        .where(Refund.user_id == user_id)
        .order_by(Refund.created_at.desc(), Refund.refund_id.desc())
        .limit(_clamp_limit(limit))
    )
    assert_row_filtered(stmt)
    with _read_session(session) as db:
        return db.execute(stmt).scalars().all()


def get_tickets_for_customer(
    user_id: int, *, limit: int = DEFAULT_LIMIT, session: Session | None = None
) -> Sequence[Ticket]:
    """某客户的工单列表。"""
    stmt = (
        select(Ticket)
        .where(Ticket.user_id == user_id)
        .order_by(Ticket.created_at.desc(), Ticket.ticket_id.desc())
        .limit(_clamp_limit(limit))
    )
    assert_row_filtered(stmt)
    with _read_session(session) as db:
        return db.execute(stmt).scalars().all()


def get_customer_profile(user_id: int, *, session: Session | None = None) -> User | None:
    """客户本人资料（同样按 user_id 过滤；脱敏在 security 模块做，这里只负责取数）。"""
    stmt = select(User).where(User.user_id == user_id)
    assert_row_filtered(stmt, expect_limit=False)
    with _read_session(session) as db:
        return db.execute(stmt).scalars().one_or_none()


def query_orders_for_customer(user_id: int, db: Session, *, limit: int = DEFAULT_LIMIT) -> Sequence[Order]:
    """给已有会话（如 FastAPI `Depends(get_db)`）用的版本，避免重复开连接。\n\n    命名口径：`get_*` 开头 = 对客出口（首参必须是 user_id，见 tests/test_db_queries.py 的签名检查），\n    所以「复用调用方会话」这个变体用 query_ 前缀，免得签名检查误判。\n    """
    return get_orders_for_customer(user_id, limit=limit, session=db)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "assert_row_filtered",
    "get_customer_profile",
    "get_order_for_customer",
    "get_orders_for_customer",
    "query_orders_for_customer",
    "get_refunds_for_customer",
    "get_tickets_for_customer",
    "SessionLocal",
]
