"""订单的**对客读出口**（变更 003：客户订单页的数据来源）。

本模块回答一个问题：**「这个客户自己的订单长什么样，给他看多清楚」**。它做三件事，
每一件都必须在这里（而不是在路由里）做，理由逐条写清：

1. **行级过滤**：只调 `app/db/queries.py` 的封装函数（那些函数固定带
   `WHERE user_id = :me`，并由 `assert_row_filtered` 在构造期断言）。
   本模块**不写 SQL**，也不接受调用方给的 `user_id` 之外的任何过滤主张。
2. **脱敏**：调 `app/security/masking.mask_pii(role=...)`，规则表只有一处真相。
   ⚠️ 为什么不在路由里脱敏：`app/api/` **被契约测试禁止调用 `mask_pii`**
   （`tests/api/test_api_contracts.py` 的 `FORBIDDEN_CALLS`），因为「出口脱敏」
   必须发生在**层与层之间的缝**上，而不是散在 HTTP 处理器里 —— 后者漏一处就是一次泄漏，
   而漏掉的那一处不会有任何报错。同一手法见 `app/tools/audit.py` 与
   `app/queue/agent_runs.py::_masked`。
3. **形状装配**：把 ORM 行翻成普通 dict（含 `remaining_refundable` / `refundable` 两个
   **派生字段**）。派生值放在这里而不是前端：它们依赖 `CONSUMING_REFUND_STATUSES`
   （`rejected` 不计入）这条业务口径，前端复刻一份迟早漂移。

## 「还剩多少可退」的口径（唯一一处）

```
remaining_refundable = total_amount − sum(refunds.amount where status in (requested, approved, refunded))
```

被驳回（`rejected`）的钱**不计入占用** —— 它可以再退一次。
这条口径与 `app/tools/refund.CONSUMING_REFUND_STATUSES` 同源（本模块 import 它，
不重写列表），所以「页面显示的额度」与「写路径实际校验的额度」不会漂移。

⚠️ 本模块算出来的是**展示值**，不是授权值：真正的额度判定发生在
`app/tools/refund._insert_refund_once`（那里对订单行加了锁）。
所以「页面说能退 100、提交却被拒」是**可能且正常**的（另一笔并发退款先占掉了额度），
界面必须把这种拒绝如实展示，而不是假装页面数字就是权威。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy.orm import Session

from app.db.models import Order, OrderItem, Refund
from app.db.queries import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    get_consumed_refund_by_order,
    get_order_for_customer,
    get_orders_for_customer,
    get_refunds_for_customer,
)
from app.security.masking import mask_pii
from app.security.rbac import PermissionDenied
from app.tools.audit import audit_tool_call
from app.tools.errors import ToolContractError
from app.tools.refund import REFUNDABLE_ORDER_STATUSES, remaining_refundable

#: 审计用的工具名（与其余工具的命名习惯一致；读操作也要能被归属）
LIST_TOOL: Final[str] = "list_orders"
VIEW_TOOL: Final[str] = "get_order_view"

#: 订单页最多返回多少行（与 `app/db/queries.MAX_LIMIT` 同源，不另写一个数字）
MAX_ORDER_ROWS: int = MAX_LIMIT


def _cent(value: Any) -> Decimal:
    """`Decimal` 归一到两位小数（与 `NUMERIC(12,2)` 一致；求和后用它出网）。"""
    return Decimal(value).quantize(Decimal("0.01"))


def _money_text(value: Any) -> str:
    """金额 → 出网字符串（**不是 float**：`NUMERIC(12,2)` 是精确十进制）。

    走 JSON float 会把 `0.1 + 0.2` 那类二进制尾巴带回来，而金额一旦带上尾巴，
    前端显示与后端校验就会差一点点 —— 那是资金类界面最不该有的东西。
    """
    return str(_cent(value))


def _iso(value: Any) -> str | None:
    """时间 → ISO8601 字符串（`None` 原样返回）。

    ⚠️ 必须在**服务层**转（与 `app/api/refunds.py::_iso` 同一口径）：
    响应模型的 `created_at` 字段类型是 `str | None`，把 `datetime` 直接塞进
    `model_validate` 会抛 `ValidationError`（实测：2 validation errors for OrderView）——
    而那是 500，不是「时间格式不好看」。统一在这里转，两个出口就不会漂移。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _items(items: Iterable[OrderItem]) -> list[dict[str, Any]]:
    """订单明细 → 普通 dict（含商品名；`product` 由 `selectinload` 预加载）。"""
    out: list[dict[str, Any]] = []
    for item in items or ():
        product = getattr(item, "product", None)
        out.append(
            {
                "order_item_id": int(item.order_item_id),
                "product_id": int(item.product_id),
                # 商品名可能因为数据缺失为 None（`order_items.product_id` 有外键，但不保证
                # 关系被加载过）；取不到就给 None，**不编造**。
                "product_name": getattr(product, "name", None),
                "quantity": int(item.quantity),
                "unit_price": _money_text(item.unit_price),
            }
        )
    return out


def _refunds(refunds: Sequence[Refund]) -> list[dict[str, Any]]:
    """退款记录 → 普通 dict（按调用方给的顺序，通常是 `created_at DESC`）。"""
    return [
        {
            "refund_id": int(row.refund_id),
            "amount": _money_text(row.amount),
            "status": str(row.status),
            "reason": row.reason,
            "decision_note": row.decision_note,
            "created_at": _iso(row.created_at),
            "decided_at": _iso(row.decided_at),
        }
        for row in refunds
    ]


def _order_dict(
    order: Order,
    *,
    refunds: Sequence[Refund],
    consumed: Decimal,
) -> dict[str, Any]:
    """订单 → 原始 dict（**未脱敏**；脱敏由 `_mask_rows` 统一做）。"""
    total = _cent(order.total_amount)
    remaining = _cent(total - consumed)
    if remaining < 0:  # pragma: no cover - 数据不一致时不该出现负数（写路径有额度校验）
        remaining = Decimal("0.00")
    return {
        "order_id": int(order.order_id),
        "status": str(order.status),
        "total_amount": _money_text(total),
        # 这一列**会被脱敏**（`FIELD_RULES["payment_method"]` 是财务级）：
        # 客户看到的将是 `***`，而不是 null ——「看不到」与「没有」必须能区分。
        "payment_method": order.payment_method,
        "created_at": _iso(order.created_at),
        "items": _items(order.items),
        "refunds": _refunds(refunds),
        "consumed_amount": _money_text(consumed),
        "remaining_refundable": _money_text(remaining),
        # 可退 = 状态可退 **且** 还有额度。两个条件都要，缺一个都会让界面给出
        # 一个必然失败的按钮（状态不对 → 写路径拒；额度为 0 → 写路径拒）。
        "refundable": (
            str(order.status) in REFUNDABLE_ORDER_STATUSES and remaining > Decimal("0.00")
        ),
    }


def _mask_rows(rows: list[dict[str, Any]], *, role: str) -> list[dict[str, Any]]:
    """按调用者角色脱敏（**唯一出口**：订单页与订单详情共用）。"""
    return mask_pii(rows, role=role).rows


def list_orders_for_customer(
    user_id: int,
    *,
    role: str,
    caller: str | None = None,
    limit: int = DEFAULT_LIMIT,
    session: Session | None = None,
) -> list[dict[str, Any]]:
    """某客户的订单页数据（行级过滤 + 脱敏 + 派生额度）。

    ⚠️ `user_id` 只能来自**已验签的令牌**（路由里的 `Actor.user_id`）。
    本函数不提供「查别人订单」的模式：想跨用户看必须走对内路径（`read:all_orders`），
    那条路与本函数无关。

    ⚠️ `caller` 是 keyword-only 的（工具层契约：每个公开工具都要能被审计归属）。
    读操作也留痕：客户看自己的订单是**被记录**的行为，「谁在什么时候拉过谁的订单」
    在排障与合规复盘时是第一条要问的。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        # 员工主体是 `0`（他在 users 表里没有行）。走到这里说明调用方接错了线 ——
        # 那不是「查不到订单」，而是「根本不知道查谁」，必须显式暴露。
        raise ToolContractError(
            f"user_id 必须是正整数，收到 {user_id!r}"
            "（员工主体的 user_id 是 0，他没有自己的 orders；跨用户查询要走对内路径）"
        )
    effective = max(1, min(int(limit), MAX_ORDER_ROWS))

    with audit_tool_call(
        LIST_TOOL, caller=caller, user_id=user_id, role=role, limit=effective
    ) as handle:
        orders = list(get_orders_for_customer(user_id, limit=effective, session=session))
        refunds = list(get_refunds_for_customer(user_id, limit=effective, session=session))
        consumed_by_order = get_consumed_refund_by_order(user_id, session=session)

        grouped: dict[int, list[Refund]] = {}
        for row in refunds:
            grouped.setdefault(int(row.order_id), []).append(row)

        rows = [
            _order_dict(
                order,
                refunds=grouped.get(int(order.order_id), []),
                consumed=Decimal(consumed_by_order.get(int(order.order_id), Decimal("0"))),
            )
            for order in orders
        ]
        handle.rows = len(rows)
        handle.details.update({"role": role, "orders": len(rows)})
        return _mask_rows(rows, role=role)


def get_order_view(
    user_id: int,
    order_id: int,
    *,
    role: str,
    caller: str | None = None,
    session: Session | None = None,
) -> dict[str, Any] | None:
    """单笔订单详情（同时按 `user_id` 与 `order_id` 过滤）。

    查不到时返回 `None`（而不是抛）：**「不是我的单」与「这单不存在」必须不可区分** ——
    路由据此统一按 404 处置，攻击者无法用「403 vs 404」的差异探测订单号是否真实存在
    （与 `app/api/errors.py` 的越权口径同源）。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ToolContractError(f"user_id 必须是正整数，收到 {user_id!r}")
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1:
        raise ToolContractError(f"order_id 必须是正整数，收到 {order_id!r}")

    with audit_tool_call(
        VIEW_TOOL, caller=caller, user_id=user_id, order_id=order_id, role=role
    ) as handle:
        order = get_order_for_customer(user_id, order_id, session=session)
        if order is None:
            handle.rows = 0
            handle.details["found"] = False
            return None
        refunds = [
            row
            for row in get_refunds_for_customer(user_id, session=session)
            if int(row.order_id) == int(order_id)
        ]
        consumed = Decimal(
            get_consumed_refund_by_order(user_id, session=session).get(order_id, Decimal("0"))
        )
        rows = _mask_rows([_order_dict(order, refunds=refunds, consumed=consumed)], role=role)
        handle.rows = 1
        handle.details.update({"found": True, "role": role})
        return rows[0]


def assert_own_order(user_id: int, order_id: int, *, session: Session | None = None) -> None:
    """校验这笔订单属于该客户；不属于 → `PermissionDenied`（路由按 404 处置）。

    单独一个函数（而不是让调用方自己查一遍）：退款申请接口需要它，
    而「越权必须用同一种方式拒绝」这件事只能有一处实现 —— 两处实现里迟早有一处
    会改成 403，而那个差异就是订单号存在性探测的入口。
    """
    if get_order_for_customer(user_id, order_id, session=session) is None:
        raise PermissionDenied(
            f"越权拦截：订单 {order_id} 不属于 user_id={user_id}（或不存在）",
            role="customer",
        )


def remaining_for_order(user_id: int, order_id: int, *, session: Session | None = None) -> Decimal:
    """当前剩余可退额度（**只读**；与 `app/tools/refund.remaining_refundable` 同一实现）。

    这里是转发而不是重新实现：额度口径只有一处真相（那个函数做了归属校验，
    所以「拿别人的订单号问额度」同样会被拒）。
    """
    return remaining_refundable(user_id, order_id, session=session)


def refundable_orders(
    user_id: int, *, session: Session | None = None
) -> list[dict[str, Any]]:
    """该客户**此刻可以发起退款**的订单候选（变更 003；`refund` 图节点用）。

    每条：`{"order_id", "remaining" (Decimal), "status"}`。按 `created_at DESC`
    （与订单页同序）——「最近那笔」通常是客户想退的那笔，所以多笔时列出来的顺序
    要让他一眼认出来。

    ⚠️ 它与 `list_orders_for_customer` 的区别是**不脱敏、不出网**：这是给
    `app/agents/refund.py` 做决策用的**内部事实**（要不要落单、退多少），
    不是给任何界面渲染的。所以它返回 `Decimal` 而不是造一个字符串，
    也不经过 `mask_pii` —— 少走一层，就少一处「脱敏后的值被当成决策依据」的机会。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ToolContractError(f"user_id 必须是正整数，收到 {user_id!r}")

    consumed_by_order = get_consumed_refund_by_order(user_id, session=session)
    out: list[dict[str, Any]] = []
    for order in get_orders_for_customer(user_id, session=session):
        status = str(order.status)
        total = _cent(order.total_amount)
        remaining = _cent(total - Decimal(consumed_by_order.get(int(order.order_id), Decimal("0"))))
        if status not in REFUNDABLE_ORDER_STATUSES or remaining <= Decimal("0.00"):
            continue
        out.append(
            {
                "order_id": int(order.order_id),
                "remaining": remaining,
                "status": status,
            }
        )
    return out


def customer_order_ids(user_id: int, *, session: Session | None = None) -> list[int]:
    """该客户**全部**订单号（含不可退的）。

    用途：客户说了一个订单号时，要能区分「这不是你的单」与「这单现在不能退」——
    两者的答复完全不同（前者不该确认它存在），所以需要一份「他名下有哪些单」的清单。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ToolContractError(f"user_id 必须是正整数，收到 {user_id!r}")
    return [int(order.order_id) for order in get_orders_for_customer(user_id, session=session)]


__all__ = [
    "LIST_TOOL",
    "MAX_ORDER_ROWS",
    "VIEW_TOOL",
    "assert_own_order",
    "customer_order_ids",
    "get_order_view",
    "list_orders_for_customer",
    "refundable_orders",
    "remaining_for_order",
]
