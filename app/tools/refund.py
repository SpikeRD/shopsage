"""退款工具：**申请退款 / 查退款状态**（`6.tools.md` 第 4 节第 1 条点名的第二个写操作）。

为什么退款要单独成模块（`6.tools.md` 第 2 节的接口只列了工单）——
见 `docs/runbook/6.tools-runbook.md` §10.1 坑 2：该文档第 4 节与第 8 节都把「**退款**」
和「工单」并列成必须走预定义函数的写操作，且 A2 的验收是「工单/退款幂等」。
只做工单的话，A2 只能勾一半。

它是「写路径」的**完整形态**（比工单多三件事，都值得单独说清）：

1. **业务规则在工具里，不在 prompt 里**：订单必须属于本人、状态可退、
   金额不超过「订单金额 − 已申请退款」。这些规则如果交给 LLM 判断，
   就等于把资金安全押在模型的算术能力上。
2. **职责分离（SoD）**：`customer` 角色的权限集里**没有** `write:refund`
   （见 `app/security/rbac.py`），所以「客户自己点退款」在本系统的模型里走的是
   **建工单**（客户有 `write:ticket`），退款必须由客服/主管发起。
   这不是为了复杂而复杂：退款是资金操作，一线客服与客户之间需要有第二个人复核。
3. **行锁 + 累计校验**：算「还能退多少」时必须对订单行加锁（`SELECT ... FOR UPDATE`），
   否则两笔并发退款会**各自读到同样的可用余额**、各退一笔，把钱退超。
   这是本模块唯一一处显式加锁的地方，理由写在 `_remaining_refundable()` 里。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import Order, Refund
from app.db.session import get_read_engine
from app.security.rbac import PERM_WRITE_REFUND, PermissionDenied, has_permission
from app.tools.audit import audit_tool_call
from app.tools.errors import PermanentToolError, ToolContractError, translate_db_error
from app.tools.write_path import insert_once, validate_idempotency_key, write_session

CREATE_TOOL: Final[str] = "create_refund"
STATUS_TOOL: Final[str] = "get_refund_status"

IDEMPOTENCY_SCOPE: Final[tuple[str, ...]] = ("user_id", "idempotency_key")

# 允许发起退款的订单状态。**刻意不含** `pending`/`paid`（还没发货就别退，走取消流程）
# 与 `cancelled`（已取消的订单没有可退的钱）。宁可在这里拒，也不要退出一笔"看起来对"的账。
REFUNDABLE_ORDER_STATUSES: Final[tuple[str, ...]] = ("shipped", "delivered")

# 计入「已占用额度」的退款状态：`rejected` 不算（被驳回的钱可以再退一次）
CONSUMING_REFUND_STATUSES: Final[tuple[str, ...]] = ("requested", "approved", "refunded")

# 金额精度与 `models.MONEY`（NUMERIC(12,2)）一致
_CENT: Final[Decimal] = Decimal("0.01")

# 新建退款单的初始状态（与库的 server_default 一致）
INITIAL_STATUS: Final[str] = "requested"

# 单笔退款的申诉理由长度上限（与 models.Refund.reason 的 VARCHAR(255) 一致）
MAX_REASON_CHARS: Final[int] = 255


class RefundNotAllowed(PermanentToolError):
    """**业务拒绝**：请求本身合法（参数没问题、也有权限），但当前状态不允许。

    与 `ToolContractError` 的区别值得保留：契约错误是「调用方写错了」（重试无用，
    要改代码/改 prompt），业务拒绝是「这个业务动作此刻不成立」（重试无用，
    但**改的是业务前提**，比如「先等货到」）。第 7 步的 agent 对两者的措辞完全不同：
    前者回喂给 LLM 让它改参数，后者应当直接告诉用户原因。
    """


@dataclass(frozen=True)
class RefundRef:
    """退款单的业务引用（金额是 `Decimal`，**不是 float**：钱的比较不能走二进制浮点）。"""

    refund_id: int
    order_id: int
    user_id: int
    amount: Decimal
    status: str


def _to_money(value: Any) -> Decimal:
    """入参金额 → `Decimal`（两位小数）。

    ⚠️ `float` 走 `Decimal(str(value))` 而不是 `Decimal(value)`：后者会把
    `0.1` 的二进制近似值原样带进来（`Decimal(0.1)` = 0.1000000000000000055511151231257827…），
    金额一旦沾上这个尾巴，后面所有比较都会莫名其妙地差一点点。
    """
    if value is None or isinstance(value, bool):
        raise ToolContractError(f"amount 必须是正数（金额），收到 {value!r}")
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        amount = Decimal(str(value))
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ToolContractError(f"amount 不是合法数字：{value!r}") from exc
    else:
        raise ToolContractError(f"amount 类型不支持：{type(value).__name__}")

    amount = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise ToolContractError(f"amount 必须大于 0，收到 {amount}")
    return amount


def _check_reason(reason: Any) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise ToolContractError(f"reason 必须是字符串或 None，收到 {type(reason).__name__}")
    cleaned = reason.strip().replace("\n", " ")
    if len(cleaned) > MAX_REASON_CHARS:
        raise ToolContractError(
            f"reason 过长（{len(cleaned)} > {MAX_REASON_CHARS}）：请用一句话说明原因"
        )
    return cleaned or None


def _remaining_refundable(db: Session, order_id: int, order_amount: Decimal) -> Decimal:
    """该订单**当前还能退多少钱** = 订单金额 − 已占用额度（不含 rejected）。

    ⚠️ 调用方必须已经对订单行加了写锁（`with_for_update()`）：
    没有锁时，「读余额 → 插入」之间会插进另一笔退款请求，两笔都认为余额够 ——
    这是资金类功能最经典的一类超发 bug，而且在单线程测试里**永远不会暴露**。
    锁的粒度选「订单行」而不是「整个 refunds 表」：同一订单的并发退款串行化，
    不同订单互不影响（退款是低频操作，行锁的争用可以忽略）。
    """
    consumed = db.execute(
        select(func.coalesce(func.sum(Refund.amount), 0)).where(
            Refund.order_id == order_id,
            Refund.status.in_(CONSUMING_REFUND_STATUSES),
        )
    ).scalar_one()
    return (order_amount - Decimal(consumed)).quantize(_CENT, rounding=ROUND_HALF_UP)


def create_refund_request(
    user_id: int,
    order_id: int,
    amount: Any,
    *,
    idempotency_key: str,
    caller: str | None = None,
    role: str = "customer_service",
    reason: str | None = None,
    session: Session | None = None,
) -> RefundRef:
    """为某笔订单发起退款申请（幂等；金额不得超过订单剩余可退额度）。

    参数：
        user_id: 退款归属的客户（必须与订单的 user_id 一致，否则按越权拒绝）。
        order_id: 订单号。
        amount: 退款金额（`Decimal` / `int` / `float` / 数字字符串，统一按两位小数处理）。
        idempotency_key: 幂等键（必填）。
        caller: 审计字段「谁在调用」（必填）。
        role: 调用方角色，需要 `write:refund` 权限（**客户没有**这个权限，见模块 docstring）。
        reason: 退款原因（可选，单行文本）。
        session: 复用调用方事务（提交由调用方负责）；`None` = 自己开写会话并提交。

    异常：
        `PermissionDenied`：角色缺 `write:refund`，或订单不属于该用户；
        `RefundNotAllowed`：订单状态不可退、金额超过剩余可退额度；
        `ToolContractError`：参数形状非法、订单不存在；
        `RetryableToolError` / `PermanentToolError`：数据库侧失败。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ToolContractError(f"user_id 必须是正整数，收到 {user_id!r}")
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1:
        raise ToolContractError(f"order_id 必须是正整数，收到 {order_id!r}")

    # 审计包住参数校验与权限判定：资金操作被拒的那一刻，日志里必须留下「谁、对哪笔订单、想退多少」
    with audit_tool_call(
        CREATE_TOOL,
        caller=caller,
        user_id=user_id,
        order_id=order_id,
        amount=amount,
        idempotency_key=idempotency_key,
        role=role,
        reason=reason,
    ) as handle:
        requested_amount = _to_money(amount)
        checked_key = validate_idempotency_key(idempotency_key)
        checked_reason = _check_reason(reason)

        if not has_permission(role, PERM_WRITE_REFUND):
            raise PermissionDenied(
                f"角色 {role or '(未提供)'} 缺少权限：{PERM_WRITE_REFUND}（发起退款）。"
                "职责分离是刻意的：对客角色只能建工单，退款由客服/主管发起（见 rbac.ROLE_PERMISSIONS）",
                role=role,
                missing=[PERM_WRITE_REFUND],
            )

        try:
            with write_session(session) as db:
                # 行锁：同一订单的并发退款串行化（见 _remaining_refundable 的说明）
                order = db.execute(
                    select(Order.order_id, Order.user_id, Order.status, Order.total_amount)
                    .where(Order.order_id == order_id)
                    .with_for_update()
                ).one_or_none()
                if order is None:
                    raise ToolContractError(f"订单不存在：order_id={order_id}")
                if int(order.user_id) != user_id:
                    # 越权：拿别人的订单号来退款。★这条必须留痕（审计在 __exit__ 里记 error）
                    raise PermissionDenied(
                        f"越权拦截：订单 {order_id} 属于 user_id={order.user_id}，"
                        f"请求方声明 user_id={user_id}",
                        role=role,
                    )
                if str(order.status) not in REFUNDABLE_ORDER_STATUSES:
                    raise RefundNotAllowed(
                        f"订单 {order_id} 当前状态是 {order.status}，不支持退款"
                        f"（可退状态：{', '.join(REFUNDABLE_ORDER_STATUSES)}）"
                    )

                remaining = _remaining_refundable(db, order_id, Decimal(order.total_amount))
                if requested_amount > remaining:
                    raise RefundNotAllowed(
                        f"退款金额 {requested_amount} 超过订单 {order_id} 的剩余可退额度 {remaining}"
                        f"（订单金额 {order.total_amount} 减去已申请的退款）"
                    )

                refund_id, created = insert_once(
                    db,
                    Refund,
                    {
                        "order_id": order_id,
                        "user_id": user_id,
                        "amount": requested_amount,
                        "status": INITIAL_STATUS,
                        "reason": checked_reason,
                        "idempotency_key": checked_key,
                    },
                    conflict_on=IDEMPOTENCY_SCOPE,
                    lookup={"user_id": user_id, "idempotency_key": checked_key},
                )
                row = db.execute(
                    select(Refund.order_id, Refund.user_id, Refund.amount, Refund.status).where(
                        Refund.refund_id == refund_id
                    )
                ).one()
                if session is None:
                    db.commit()
        except SQLAlchemyError as exc:
            raise translate_db_error(exc, action=f"发起退款（{CREATE_TOOL}）") from exc

        handle.rows = 1
        handle.details.update(
            {
                "created": created,
                "idempotent_hit": not created,
                "refund_id": int(refund_id),
                "requested_amount": str(requested_amount),
                "stored_amount": str(row.amount),
                "order_status": str(order.status),
            }
        )
        if not created and Decimal(row.amount) != requested_amount:
            handle.details["idempotency_amount_mismatch"] = True

        return RefundRef(
            refund_id=int(refund_id),
            order_id=int(row.order_id),
            user_id=int(row.user_id),
            amount=Decimal(row.amount),
            status=str(row.status),
        )


def get_refund_status(
    refund_id: int,
    *,
    caller: str | None = None,
    requester_user_id: int | None = None,
    session: Session | None = None,
) -> RefundRef | None:
    """查退款单状态（与 `get_ticket_status` 同口径：对客路径拦越权，读走只读引擎）。"""
    if isinstance(refund_id, bool) or not isinstance(refund_id, int) or refund_id < 1:
        raise ToolContractError(f"refund_id 必须是正整数，收到 {refund_id!r}")
    if requester_user_id is not None and (
        isinstance(requester_user_id, bool)
        or not isinstance(requester_user_id, int)
        or requester_user_id < 1
    ):
        raise ToolContractError(f"requester_user_id 必须是正整数或 None，收到 {requester_user_id!r}")

    with audit_tool_call(
        STATUS_TOOL, caller=caller, refund_id=refund_id, requester_user_id=requester_user_id
    ) as handle:
        try:
            db = session if session is not None else Session(get_read_engine())
            try:
                row = db.execute(
                    select(
                        Refund.refund_id, Refund.order_id, Refund.user_id, Refund.amount, Refund.status
                    ).where(Refund.refund_id == refund_id)
                ).one_or_none()
            finally:
                if session is None:
                    db.close()
        except SQLAlchemyError as exc:
            raise translate_db_error(exc, action=f"查退款状态（{STATUS_TOOL}）") from exc

        if row is None:
            handle.rows = 0
            handle.details["found"] = False
            return None

        if requester_user_id is not None and int(row.user_id) != requester_user_id:
            handle.rows = None
            handle.details.update({"found": True, "owned_by_requester": False})
            raise PermissionDenied(
                f"越权拦截：退款单 {refund_id} 属于 user_id={row.user_id}，"
                f"调用方身份是 user_id={requester_user_id}",
                role="customer",
            )

        handle.rows = 1
        handle.details.update({"found": True, "owned_by_requester": True, "status": str(row.status)})
        return RefundRef(
            refund_id=int(row.refund_id),
            order_id=int(row.order_id),
            user_id=int(row.user_id),
            amount=Decimal(row.amount),
            status=str(row.status),
        )


__all__ = [
    "CONSUMING_REFUND_STATUSES",
    "CREATE_TOOL",
    "IDEMPOTENCY_SCOPE",
    "INITIAL_STATUS",
    "REFUNDABLE_ORDER_STATUSES",
    "STATUS_TOOL",
    "RefundNotAllowed",
    "RefundRef",
    "create_refund_request",
    "get_refund_status",
]
