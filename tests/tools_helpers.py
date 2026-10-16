"""tools 模块测试的公共帮助（**不是**测试文件，也不放 pytest 装置 —— 装置在 `conftest.py`）。

两条纪律：

1. **测试写库必须能收尾**：工单/退款用例会真的插行（写路径没有内存假实现，
   假实现就测不出唯一约束与幂等），所以统一用 `test-tools-` 前缀命名幂等键，
   用例结束后按前缀清理。不清的话演示库会被测试数据越堆越乱，
   `scripts/db_check.py --check` 的观感也会变差。
2. **挑演示数据要挑「一定存在」的**：种子数据（`scripts/seed.py`）里
   订单状态是确定性随机的，所以不能硬编码「order_id=3 一定是 delivered」——
   要按条件查出来。否则换一个 seed 值用例就红，而红的原因与代码无关。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import delete, func, select

from app.db.models import Order, Refund, Ticket
from app.db.session import SessionLocal
from app.tools.refund import CONSUMING_REFUND_STATUSES, REFUNDABLE_ORDER_STATUSES

# 所有测试造的幂等键都带这个前缀，便于「只删自己造的行」
TEST_KEY_PREFIX = "test-tools-"

#: **图侧退款节点**造的行的幂等键前缀（`app/tools/refund.llm_refund_key`）。
#:
#: ⚠️ 它必须被单独清掉，否则会**永久吃掉演示库的可退额度**（实测踩到）：
#: `tests/graph/test_graph_refund.py` 里每个用例都会让节点真的落一张单，
#: 而这些单按设计**只增不减**（`requested`/`approved` 计入 `CONSUMING_REFUND_STATUSES`）。
#: 它们用的是 `llm-` 前缀，不在 `TEST_KEY_PREFIX` 的清理范围内 ——
#: 于是跑一遍图用例之后，「客户 2/3 没有可退订单」，后续用例成片 skip，
#: 而现象看起来像「种子数据坏了」。
#:
#: 为什么清理函数按前缀分两个：`test-tools-` 是本套件**自己**造的，
#: `llm-` 是**被图节点造出来的**（用例只喂了 state）。两者都要清，
#: 但它们是不同的来源，分开写才能说清「谁造的」。
LLM_KEY_PREFIX = "llm-"


def tool_key(suffix: str) -> str:
    """造一个测试用幂等键（`tool_key("ticket-1")` → `test-tools-ticket-1`）。"""
    return f"{TEST_KEY_PREFIX}{suffix}"


def cleanup_tool_rows() -> dict[str, int]:
    """删掉本测试套件造出来的工单/退款行（按幂等键前缀），返回删除条数。

    只删 `idempotency_key` 命中前缀的行：种子数据与别的用例造的行（幂等键为 NULL）不受影响。

    ⚠️ **同时清 `llm-` 前缀**（图侧退款节点造的）：那些行会让订单额度永久减少，
    留着它们会让后续用例以为「演示库没有可退订单」（理由见 `LLM_KEY_PREFIX`）。
    真实的对话（人通过 /api/chat 或界面）也造 `llm-` 键 —— 但**测试不该在演示库里
    留下真实的业务数据**，所以测试跑完就清是正确的一侧。
    """
    with SessionLocal() as db:
        tickets = db.execute(
            delete(Ticket).where(Ticket.idempotency_key.like(f"{TEST_KEY_PREFIX}%"))
        ).rowcount
        refunds = db.execute(
            delete(Refund).where(
                Refund.idempotency_key.like(f"{TEST_KEY_PREFIX}%")
                | Refund.idempotency_key.like(f"{LLM_KEY_PREFIX}%")
            )
        ).rowcount
        db.commit()
    return {"tickets": int(tickets or 0), "refunds": int(refunds or 0)}


def find_refundable_order(*, user_id: int | None = None) -> tuple[int, int, Decimal, Decimal]:
    """挑一笔「状态可退、且还有剩余可退额度」的订单。

    返回 `(order_id, user_id, total_amount, remaining)`。

    ⚠️ `remaining` 必须真的从库里算出来，不能假设「订单金额全都能退」：
    实测演示数据里就有订单已经被种子退款占掉了大部分额度
    （订单 4：总额 141.00，已申请 112.80，只剩 28.20）。
    测试若按总额算金额，会得到一条与代码无关的 `RefundNotAllowed` 失败 ——
    这正是「用例的失败原因必须与它的名字对得上」那条纪律要挡的情况。

    挑不出来是**环境问题**（种子数据没灌或全被退过），调用方应当 `pytest.skip` 并说明怎么修。
    """
    consumed = (
        select(Refund.order_id, func.coalesce(func.sum(Refund.amount), 0).label("used"))
        .where(Refund.status.in_(CONSUMING_REFUND_STATUSES))
        .group_by(Refund.order_id)
        .subquery()
    )
    statement = (
        select(
            Order.order_id,
            Order.user_id,
            Order.total_amount,
            (Order.total_amount - func.coalesce(consumed.c.used, 0)).label("remaining"),
        )
        .outerjoin(consumed, consumed.c.order_id == Order.order_id)
        .where(Order.status.in_(REFUNDABLE_ORDER_STATUSES))
        .where(Order.total_amount > func.coalesce(consumed.c.used, 0))
        .order_by(Order.order_id)
        .limit(1)
    )
    if user_id is not None:
        statement = statement.where(Order.user_id == user_id)
    with SessionLocal() as db:
        row = db.execute(statement).one_or_none()
    if row is None:
        raise LookupError(
            "演示库里没有「状态可退且还有额度」的订单：请先跑 `uv run python -m scripts.seed`"
            f"（可退状态 {REFUNDABLE_ORDER_STATUSES}）"
        )
    return (
        int(row.order_id),
        int(row.user_id),
        Decimal(row.total_amount),
        Decimal(row.remaining).quantize(Decimal("0.01")),
    )


def count_tickets(idempotency_key: str) -> int:
    """按幂等键数行数（幂等用例的核心断言：**库里只有一行**，而不只是返回值相同）。"""
    with SessionLocal() as db:
        return int(
            db.execute(
                select(func.count()).select_from(Ticket).where(Ticket.idempotency_key == idempotency_key)
            ).scalar_one()
        )


def count_refunds(idempotency_key: str) -> int:
    with SessionLocal() as db:
        return int(
            db.execute(
                select(func.count()).select_from(Refund).where(Refund.idempotency_key == idempotency_key)
            ).scalar_one()
        )


__all__ = [
    "LLM_KEY_PREFIX",
    "TEST_KEY_PREFIX",
    "cleanup_tool_rows",
    "count_refunds",
    "count_tickets",
    "find_refundable_order",
    "tool_key",
]
