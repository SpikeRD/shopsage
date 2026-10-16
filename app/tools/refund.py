"""退款工具：**申请退款 / 查退款状态**（`6.tools.md` 第 4 节第 1 条点名的第二个写操作）。

为什么退款要单独成模块（`6.tools.md` 第 2 节的接口只列了工单）——
见 `docs/runbook/6.tools-runbook.md` §10.1 坑 2：该文档第 4 节与第 8 节都把「**退款**」
和「工单」并列成必须走预定义函数的写操作，且 A2 的验收是「工单/退款幂等」。
只做工单的话，A2 只能勾一半。

它是「写路径」的**完整形态**（比工单多三件事，都值得单独说清）：

1. **业务规则在工具里，不在 prompt 里**：订单必须属于本人、状态可退、
   金额不超过「订单金额 − 已申请退款」。这些规则如果交给 LLM 判断，
   就等于把资金安全押在模型的算术能力上。
2. **职责分离（SoD），但落点变了（变更 003 重写过，理由必须留住）**：
   变更 003 之前，`customer` 角色的权限集里**没有** `write:refund`，于是「客户自己点退款」
   在本系统的模型里只能走**建工单**，退款必须由客服/主管发起。
   变更 003 把这条**拆成了两个动作、两个权限点**：

   | 动作 | 权限点 | 谁能做 | 能产生什么状态 |
   |---|---|---|---|
   | **申请**退款 | `request:refund` | 客户本人、管理员 | 只能建 `requested` |
   | 代客户**发起** | `write:refund` | 客服/员工/主管/管理员 | 建 `requested`（同样要等审批） |
   | **审批**退款 | `approve:refund` | 客服/员工/主管/审计/管理员 | `requested → approved / rejected` |

   为什么不是「给客户 `write:refund` 了事」：那样客户就同时拿到了「发起」与
   （在旧模型里）**自己批准自己**的能力 —— 而「客户自己批准自己的退款」正是变更 003
   要修的那个 bug。所以拆点的判据是**能不能批准**，而不是「能不能建单」：
   客户可以建单（那是他的诉求），但**永远批不了**（`approve:refund` 不在他的权限集里）。
   一线与客户之间仍然有第二个人复核，只是复核的落点从「谁建的单」变成了「谁批的」。

3. **行锁 + 累计校验**：算「还能退多少」时必须对订单行加锁（`SELECT ... FOR UPDATE`），
   否则两笔并发退款会**各自读到同样的可用余额**、各退一笔，把钱退超。
   这是本模块唯一一处显式加锁的地方，理由写在 `_remaining_refundable()` 里。
4. **审批也走行锁 + 严格状态机**（变更 003）：`decide_refund` 锁住退款行，
   只允许 `requested → approved / rejected`。没有锁时「两个员工同时点通过」
   会双双读到 `requested`、双双写 `approved`（后写的覆盖前写的，且两份审计意见只有一份留下）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import REFUND_STATUSES, Order, Refund
from app.db.queries import DEFAULT_LIMIT, MAX_LIMIT
from app.db.session import get_read_engine
from app.security.masking import mask_pii
from app.security.rbac import (
    PERM_APPROVE_REFUND,
    PERM_REQUEST_REFUND,
    PERM_WRITE_REFUND,
    PermissionDenied,
    has_permission,
)
from app.tools.audit import audit_tool_call
from app.tools.errors import PermanentToolError, ToolContractError, translate_db_error
from app.tools.write_path import insert_once, validate_idempotency_key, write_session

CREATE_TOOL: Final[str] = "create_refund"
STATUS_TOOL: Final[str] = "get_refund_status"
# ── 变更 003 新增的三个工具名（审计里要能区分「谁做的哪个动作」）──────────────
REQUEST_TOOL: Final[str] = "request_refund"
DECIDE_TOOL: Final[str] = "decide_refund"
LIST_TOOL: Final[str] = "list_refunds"
#: 把退款单转成出网形状（脱敏那一步）的审计名（变更 003）
OUTPUT_TOOL: Final[str] = "refund_rows_for_output"

IDEMPOTENCY_SCOPE: Final[tuple[str, ...]] = ("user_id", "idempotency_key")

# 允许发起退款的订单状态。**刻意不含** `pending`/`paid`（还没发货就别退，走取消流程）
# 与 `cancelled`（已取消的订单没有可退的钱）。宁可在这里拒，也不要退出一笔"看起来对"的账。
REFUNDABLE_ORDER_STATUSES: Final[tuple[str, ...]] = ("shipped", "delivered")

# 计入「已占用额度」的退款状态：`rejected` 不算（被驳回的钱可以再退一次）
CONSUMING_REFUND_STATUSES: Final[tuple[str, ...]] = ("requested", "approved", "refunded")

# ── 变更 003：审批的两个取值与状态机 ─────────────────────────────────────────
DECISION_APPROVE: Final[str] = "approve"
DECISION_REJECT: Final[str] = "reject"
DECISIONS: Final[tuple[str, ...]] = (DECISION_APPROVE, DECISION_REJECT)

#: 决策动作 → 落到库里的目标状态（**唯一一处映射**：路由、审计、测试都从这里取）
DECISION_STATUS: Final[dict[str, str]] = {
    DECISION_APPROVE: "approved",
    DECISION_REJECT: "rejected",
}

#: **唯一**可被审批的起始状态。写死成一个元组而不是 `status == "requested"` 的内联判断：
#: 将来若引入「已打款待确认」这类中间态，要改的只有这一行 + `DECISION_STATUS`，
#: 而不是散落在路由与界面里的若干处 `if`。
DECIDABLE_STATUSES: Final[tuple[str, ...]] = ("requested",)

#: 审批意见与客户申请理由共用同一条长度上限（都是 `String(255)` 列）
MAX_NOTE_CHARS: Final[int] = 255

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


class RefundNotDecidable(PermanentToolError):
    """**状态机拒绝**（变更 003）：这笔退款单此刻不能被决策（已经被批过/驳过）。

    单独一个类型（而不是复用 `RefundNotAllowed`）的理由：两者的**修法完全不同**。
    `RefundNotAllowed` 的意思是「业务前提不成立，去改你的请求」（例如订单还没发货），
    而本类型的意思是「**别人已经处理过了**，你刷新一下列表就行」——
    它是并发与重复点击的正常结果，不是错误用法。

    路由把本类型映射成 **409 Conflict**（不是 400，也不是 404）：
    请求本身完全合法，冲突的是**资源当前的状态**。前端据此刷新列表而不是改参数。
    """

    def __init__(self, message: str, *, status: str = "", refund_id: int = 0) -> None:
        super().__init__(message)
        self.status = str(status)
        self.refund_id = int(refund_id)


@dataclass(frozen=True)
class RefundRef:
    """退款单的业务引用（金额是 `Decimal`，**不是 float**：钱的比较不能走二进制浮点）。"""

    refund_id: int
    order_id: int
    user_id: int
    amount: Decimal
    status: str


@dataclass(frozen=True)
class RefundView:
    """退款单的**完整对外形态**（变更 003：给订单页与审批台用）。

    与 `RefundRef` 的分工：`RefundRef` 是「刚刚那个动作产生了什么」的最小回执
    （幂等与断言只需要那 5 个字段）；`RefundView` 是「把这张单摊开给人看」，
    多出审批留痕与原始理由 —— 客户订单页与员工审批台都要它。

    ⚠️ 它**不是**脱敏后的形态：脱敏在 API 出口按调用者角色做
    （`app/security/masking.mask_pii`，`payment_method` 那类字段靠字段名规则生效）。
    这个对象只保证「取到什么」，不保证「给谁看多清楚」。
    """

    refund_id: int
    order_id: int
    user_id: int
    amount: Decimal
    status: str
    reason: str | None
    created_at: datetime | None
    decided_by: int | None
    decided_at: datetime | None
    decision_note: str | None
    approver_role: str | None

    def as_dict(self) -> dict[str, Any]:
        """翻成给 API 用的普通 dict（**字段名的唯一一处真相**）。

        为什么要有它：订单页与审批台两条出口都要序列化退款单，各写一遍 dict 字面量
        必然会漂移（加一个字段只改一处，另一处静默少一个键，而前端拿到 `undefined`
        不会报错，只会显示成空白）。脱敏那一步也吃这个 dict 的键名。
        """
        return {
            "refund_id": self.refund_id,
            "order_id": self.order_id,
            "user_id": self.user_id,
            "amount": self.amount,
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "decision_note": self.decision_note,
            "approver_role": self.approver_role,
        }


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


def _check_note(note: Any) -> str | None:
    """审批意见的形状校验（与 `_check_reason` 同口径，但**独立成函数**）。

    为什么不复用 `_check_reason`：两者的**语义**不同（客户诉求 vs 员工意见），
    而报错文案要指向正确的那个字段名（`note` / `reason`）。
    共用实现会让「客户填的备注太长」报出「note 过长」，反而更难查。
    """
    if note is None:
        return None
    if not isinstance(note, str):
        raise ToolContractError(f"note 必须是字符串或 None，收到 {type(note).__name__}")
    cleaned = note.strip().replace("\n", " ")
    if len(cleaned) > MAX_NOTE_CHARS:
        raise ToolContractError(
            f"note 过长（{len(cleaned)} > {MAX_NOTE_CHARS}）：请用一句话说明审批意见"
        )
    return cleaned or None


@dataclass(frozen=True)
class _InsertOutcome:
    """`_insert_refund_once` 的产物（给两个入口填审计用的**最小事实集**）。

    为什么用一个类型而不是 4 元组：调用方要分别取 `refund_id` / `created` /
    `stored_amount` / `order_status`，位置元组在改动字段顺序时会**静默错位**
    （都是标量，类型检查也拦不住）。名字让「哪个是哪个」在调用点可见。
    """

    refund_id: int
    created: bool
    order_id: int
    user_id: int
    amount: Decimal
    status: str
    #: 订单**当时**的状态（审计要留的现场：批的时候这单是可退状态）
    order_status: str


def _insert_refund_once(
    db: Session,
    *,
    user_id: int,
    order_id: int,
    amount: Decimal,
    reason: str | None,
    idempotency_key: str,
) -> _InsertOutcome:
    """退款**写路径的唯一核心**：行锁 → 归属/可退状态/额度校验 → 幂等插入。

    ⚠️ 调用方负责：参数形状校验、权限判定、审计、以及**事务提交**（`write_session` 已做）。

    ## 为什么抽成"核心"而不是让两个入口各自调 `create_refund_request`

    `create_refund_request`（员工代发起，要 `write:refund`）与 `request_refund`
    （客户自助申请，要 `request:refund`）**业务规则完全相同**：同样的行锁、同样的
    可退状态、同样的剩余额度、同样的幂等键。
    另一种写法是「`request_refund` 里传一个有权角色去调 `create_refund_request`」——
    **那是伪造身份**：授权判断作用在一个假角色上，`ROLE_PERMISSIONS` 一变
    （比如哪天给某角色收回 `write:refund`）就会静默取得或失去权限。所以拆法只能是
    「共享核心 + 两个各自校验真实角色」的入口。
    """
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
            role="",
        )
    if str(order.status) not in REFUNDABLE_ORDER_STATUSES:
        raise RefundNotAllowed(
            f"订单 {order_id} 当前状态是 {order.status}，不支持退款"
            f"（可退状态：{', '.join(REFUNDABLE_ORDER_STATUSES)}）"
        )

    remaining = _remaining_refundable(db, order_id, Decimal(order.total_amount))
    if amount > remaining:
        raise RefundNotAllowed(
            f"退款金额 {amount} 超过订单 {order_id} 的剩余可退额度 {remaining}"
            f"（订单金额 {order.total_amount} 减去已申请的退款）"
        )

    refund_id, created = insert_once(
        db,
        Refund,
        {
            "order_id": order_id,
            "user_id": user_id,
            "amount": amount,
            "status": INITIAL_STATUS,
            "reason": reason,
            "idempotency_key": idempotency_key,
        },
        conflict_on=IDEMPOTENCY_SCOPE,
        lookup={"user_id": user_id, "idempotency_key": idempotency_key},
    )
    row = db.execute(
        select(Refund.order_id, Refund.user_id, Refund.amount, Refund.status).where(
            Refund.refund_id == refund_id
        )
    ).one()
    return _InsertOutcome(
        refund_id=int(refund_id),
        created=bool(created),
        order_id=int(row.order_id),
        user_id=int(row.user_id),
        amount=Decimal(row.amount),
        status=str(row.status),
        order_status=str(order.status),
    )


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
    """**员工代客户**发起退款申请（幂等；金额不得超过订单剩余可退额度）。

    ⚠️ 变更 003 起它是**员工侧**的入口（要 `write:refund`）；
    客户自助申请走 `request_refund`（要 `request:refund`）。两者共用
    `_insert_refund_once`，业务规则只有一份。

    参数：
        user_id: 退款归属的客户（必须与订单的 user_id 一致，否则按越权拒绝）。
        order_id: 订单号。
        amount: 退款金额（`Decimal` / `int` / `float` / 数字字符串，统一按两位小数处理）。
        idempotency_key: 幂等键（必填）。
        caller: 审计字段「谁在调用」（必填）。
        role: 调用方角色，需要 `write:refund` 权限。
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
                "职责分离是刻意的：客户自助申请走 request:refund（只能建 requested），"
                "而 write:refund 代表「代客户发起」这个更强的动作（见 rbac.ROLE_PERMISSIONS）",
                role=role,
                missing=[PERM_WRITE_REFUND],
            )

        try:
            with write_session(session) as db:
                outcome = _insert_refund_once(
                    db,
                    user_id=user_id,
                    order_id=order_id,
                    amount=requested_amount,
                    reason=checked_reason,
                    idempotency_key=checked_key,
                )
                if session is None:
                    db.commit()
        except SQLAlchemyError as exc:
            raise translate_db_error(exc, action=f"发起退款（{CREATE_TOOL}）") from exc

        handle.rows = 1
        handle.details.update(
            {
                "created": outcome.created,
                "idempotent_hit": not outcome.created,
                "refund_id": outcome.refund_id,
                "requested_amount": str(requested_amount),
                "stored_amount": str(outcome.amount),
                # 审计要留「当时这单是什么状态」的现场（对账与告警的依据，既有用例钉住它）
                "order_status": outcome.order_status,
            }
        )
        if not outcome.created and outcome.amount != requested_amount:
            handle.details["idempotency_amount_mismatch"] = True

        return RefundRef(
            refund_id=outcome.refund_id,
            order_id=outcome.order_id,
            user_id=outcome.user_id,
            amount=outcome.amount,
            status=outcome.status,
        )


def request_refund(
    user_id: int,
    order_id: int,
    amount: Any,
    *,
    idempotency_key: str,
    caller: str | None = None,
    role: str = "customer",
    reason: str | None = None,
    session: Session | None = None,
) -> RefundRef:
    """**客户自助**提交退款申请（变更 003；要 `request:refund`）。

    与 `create_refund_request` 的唯一差别是**要哪个权限点**：
    本函数要 `request:refund`（`customer` 持有），而它产出的行与员工发起的**完全一样**
    （`status="requested"`，等员工审批）。

    ## 三条必须写清的口径

    1. **它永远建不出「已批准」的单**。`status` 由 `INITIAL_STATUS` 常量写死，
       调用方**没有**任何参数能指定状态 —— 客户拿到 `request:refund` 不等于拿到审批权。
    2. **归属只能是本人**。`user_id` 必须与订单的 `user_id` 一致（核心里的越权校验），
       所以「拿别人的订单号申请退款」在这里就被拦下并留痕。
    3. **默认 `role="customer"`** 是刻意的：这个函数的调用方就是对客路径。
       传别的角色进来也**不会**放宽任何东西 —— 它只查 `request:refund`。

    ⚠️ 幂等键必须是**确定性**的（LLM 路径会经历图级重放，见 `app/agents/refund.py`）：
    随机键在重放时会建出两张单。手动路径由前端在打开表单时生成一次随机键。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ToolContractError(f"user_id 必须是正整数，收到 {user_id!r}")
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1:
        raise ToolContractError(f"order_id 必须是正整数，收到 {order_id!r}")

    with audit_tool_call(
        REQUEST_TOOL,
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

        if not has_permission(role, PERM_REQUEST_REFUND):
            raise PermissionDenied(
                f"角色 {role or '(未提供)'} 缺少权限：{PERM_REQUEST_REFUND}（申请退款）。"
                "审批权是另一个权限点（approve:refund）：申请人批不了自己的单，"
                "这正是变更 003 的职责分离落点（见 rbac.ROLE_PERMISSIONS）",
                role=role,
                missing=[PERM_REQUEST_REFUND],
            )

        try:
            with write_session(session) as db:
                outcome = _insert_refund_once(
                    db,
                    user_id=user_id,
                    order_id=order_id,
                    amount=requested_amount,
                    reason=checked_reason,
                    idempotency_key=checked_key,
                )
                if session is None:
                    db.commit()
        except SQLAlchemyError as exc:
            raise translate_db_error(exc, action=f"申请退款（{REQUEST_TOOL}）") from exc

        handle.rows = 1
        handle.details.update(
            {
                "created": outcome.created,
                "idempotent_hit": not outcome.created,
                "refund_id": outcome.refund_id,
                "requested_amount": str(requested_amount),
                "stored_amount": str(outcome.amount),
                # 客户自助申请**只能**产出 requested（状态由核心写死，见本函数口径 1）
                "stored_status": outcome.status,
                "order_status": outcome.order_status,
            }
        )
        if not outcome.created and outcome.amount != requested_amount:
            handle.details["idempotency_amount_mismatch"] = True

        return RefundRef(
            refund_id=outcome.refund_id,
            order_id=outcome.order_id,
            user_id=outcome.user_id,
            amount=outcome.amount,
            status=outcome.status,
        )


def remaining_refundable(
    user_id: int,
    order_id: int,
    *,
    session: Session | None = None,
) -> Decimal:
    """某笔订单**现在还能退多少钱**（只读；给订单页算默认金额用）。

    它是 `_remaining_refundable` 的**对客出口**：只有加了归属校验，客户才不能拿它去
    推算别人订单的额度（越权 → `PermissionDenied`，路由按 404 处置）。

    ⚠️ 这里**不加行锁**（只读且不写）：无锁读到的额度可能在下一条语句之前被另一笔
    退款占掉，那是**正常的并发**——真正的额度判定发生在 `_insert_refund_once` 里，
    它对订单行加了锁。本函数的结果只用于「给输入框填个默认值」，
    所以「不权威」是它的设计属性，不是缺陷；界面必须把「超额的拒绝」如实展示。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ToolContractError(f"user_id 必须是正整数，收到 {user_id!r}")
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1:
        raise ToolContractError(f"order_id 必须是正整数，收到 {order_id!r}")

    db = session if session is not None else Session(get_read_engine())
    try:
        order = db.execute(
            select(Order.order_id, Order.user_id, Order.status, Order.total_amount).where(
                Order.order_id == order_id
            )
        ).one_or_none()
        if order is None:
            raise ToolContractError(f"订单不存在：order_id={order_id}")
        if int(order.user_id) != user_id:
            raise PermissionDenied(
                f"越权拦截：订单 {order_id} 属于 user_id={order.user_id}，"
                f"请求方声明 user_id={user_id}",
                role="",
            )
        return _remaining_refundable(db, order_id, Decimal(order.total_amount))
    finally:
        if session is None:
            db.close()


def decide_refund(
    refund_id: int,
    *,
    decision: str,
    note: Any = None,
    decider_subject_id: int = 0,
    decider_subject_type: str = "employee",
    caller: str | None = None,
    role: str = "customer_service",
    session: Session | None = None,
) -> RefundView:
    """**审批**一笔退款单（变更 003；要 `approve:refund`）。

    状态机（写死在这里，路由层不再判一遍）：
    ```
    requested ──approve──→ approved
              └─reject───→ rejected
    其余任何起始状态 → RefundNotDecidable（路由映射 409）
    ```

    ## 三条设计口径

    1. **行锁**：`SELECT ... FOR UPDATE` 锁住退款行。没有锁时，两个员工同时点「通过」
       会双双读到 `requested`、双双写 `approved` —— 后写的覆盖前写的，
       而两份审计意见只有一份留下（另一份静默消失）。员工审批是**并发真实存在**的场景
       （待办列表被多个人同时打开）。
    2. **`decided_by` 只对员工主体落 id**：`accounts.subject_type` 允许是 `customer`
       （一个客户账号理论上可以持有 `approve:refund`，例如将来的「客户代表」角色）。
       那时写一个**客户 user_id** 进 `employees.emp_id` 外键会直接违反约束 ——
       所以非员工主体落 `NULL`（语义：这张单不是员工批的），而 `approver_role`
       照样记下来，审计仍能追到「是哪个角色批的」。
    3. **`decision_note` 与客户填的 `reason` 分开存**：`reason` 是客户的原始诉求，
       用审批意见覆盖它会让「客户当时说了什么」永久消失。
    """
    if isinstance(refund_id, bool) or not isinstance(refund_id, int) or refund_id < 1:
        raise ToolContractError(f"refund_id 必须是正整数，收到 {refund_id!r}")
    normalized = str(decision or "").strip().lower()
    if normalized not in DECISIONS:
        raise ToolContractError(
            f"decision 不合法：{decision!r}（只接受 {', '.join(DECISIONS)}）"
        )

    with audit_tool_call(
        DECIDE_TOOL,
        caller=caller,
        refund_id=refund_id,
        decision=normalized,
        note=note,
        role=role,
        decider_subject_id=decider_subject_id,
    ) as handle:
        checked_note = _check_note(note)

        if not has_permission(role, PERM_APPROVE_REFUND):
            raise PermissionDenied(
                f"角色 {role or '(未提供)'} 缺少权限：{PERM_APPROVE_REFUND}（审批退款）。"
                "客户只有 request:refund，做不了这个动作 —— 这是刻意的职责分离",
                role=role,
                missing=[PERM_APPROVE_REFUND],
            )

        target_status = DECISION_STATUS[normalized]
        # 预置一份，保证「异常路径不读未绑定变量」（`current` 在下面的会话块里赋值）
        current = ""
        # 只有员工主体能进 employees.emp_id 外键（见口径 2）
        decided_by = (
            int(decider_subject_id)
            if str(decider_subject_type) == "employee" and int(decider_subject_id) > 0
            else None
        )

        try:
            with write_session(session) as db:
                row = db.execute(
                    select(Refund).where(Refund.refund_id == refund_id).with_for_update()
                ).scalar_one_or_none()
                if row is None:
                    raise ToolContractError(f"退款单不存在：refund_id={refund_id}")
                current = str(row.status)
                if current not in DECIDABLE_STATUSES:
                    raise RefundNotDecidable(
                        f"退款单 {refund_id} 当前状态是 {current}，不能再审批"
                        f"（只有 {', '.join(DECIDABLE_STATUSES)} 状态可决策）",
                        status=current,
                        refund_id=refund_id,
                    )
                row.status = target_status
                row.decided_by = decided_by
                row.decided_at = datetime.now(UTC)
                row.decision_note = checked_note
                row.approver_role = str(role or "") or None
                db.flush()
                view = _to_view(row)
                if session is None:
                    db.commit()
        except SQLAlchemyError as exc:
            raise translate_db_error(exc, action=f"审批退款（{DECIDE_TOOL}）") from exc

        handle.rows = 1
        handle.details.update(
            {
                "from_status": current,
                "to_status": target_status,
                "decided_by": decided_by,
                "approver_role": str(role or ""),
            }
        )
        return view


def get_refund_view(
    refund_id: int,
    *,
    owner_user_id: int | None = None,
    session: Session | None = None,
) -> RefundView | None:
    """按 id 取一张退款单的**完整视图**（变更 003）。

    `owner_user_id` 的语义与 `get_refund_status` 一致：
      * 传了 → **对客路径**：单子不属于这个用户就抛 `PermissionDenied`（路由按 404 处置）；
      * `None` → 内部路径（调用方必须自己已经过了 `approve:refund` 的授权）。

    ## 为什么它必须存在（而不是复用 `list_refunds_for_review`）

    `POST /api/refunds` 落单后要**回读**一次才能拿到完整视图
    （`request_refund` 返回的是最小回执 `RefundRef`，刻意不含审批留痕字段）。
    第一版就是用 `list_refunds_for_review` 回读的 —— 而那个函数**要求 `approve:refund`**，
    于是客户提交退款时会得到一个 404：提交成功了，却在响应里被自己的权限拦住
    （实测：`missing=['approve:refund']`）。
    这说明「读自己的单」与「读全站单」是两件事，必须各有一个函数。
    """
    if isinstance(refund_id, bool) or not isinstance(refund_id, int) or refund_id < 1:
        raise ToolContractError(f"refund_id 必须是正整数，收到 {refund_id!r}")

    db = session if session is not None else Session(get_read_engine())
    try:
        row = db.execute(select(Refund).where(Refund.refund_id == refund_id)).scalar_one_or_none()
        if row is None:
            return None
        if owner_user_id is not None and int(row.user_id) != int(owner_user_id):
            raise PermissionDenied(
                f"越权拦截：退款单 {refund_id} 属于 user_id={row.user_id}，"
                f"调用方身份是 user_id={owner_user_id}",
                role="customer",
            )
        return _to_view(row)
    finally:
        if session is None:
            db.close()


def list_refunds_for_review(
    *,
    refund_id: int | None = None,
    order_id: int | None = None,
    user_id: int | None = None,
    statuses: Sequence[str] | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    caller: str | None = None,
    role: str = "customer_service",
    session: Session | None = None,
) -> list[RefundView]:
    """员工审批台的**查询**（变更 003；要 `approve:refund`）。

    支持的条件（决策 12）：退款单号 / 订单号 / 客户 `user_id` / 状态（多选）/ 时间范围。
    **刻意不支持按客户手机号搜**：手机号是 PII，而 `customer_service` 的清晰度上限是
    「公开级(0)」—— 把手机号做成可搜字段等于让最低可见度的角色用它做**存在性探测**
    （「这个号有没有单」本身就是信息）。

    ⚠️ 这是**内部路径**（不带行级过滤），所以权限校验在这里是唯一的一道门：
    没有 `approve:refund` 一律拒绝。它只读、走只读引擎。
    """
    if not has_permission(role, PERM_APPROVE_REFUND):
        raise PermissionDenied(
            f"角色 {role or '(未提供)'} 缺少权限：{PERM_APPROVE_REFUND}（查询退款列表）",
            role=role,
            missing=[PERM_APPROVE_REFUND],
        )
    for name, value in (("refund_id", refund_id), ("order_id", order_id), ("user_id", user_id)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ToolContractError(f"{name} 必须是正整数或 None，收到 {value!r}")

    cleaned_statuses: list[str] = []
    for item in statuses or ():
        text = str(item or "").strip().lower()
        if not text:
            continue
        if text not in REFUND_STATUSES:
            raise ToolContractError(
                f"status 取值不合法：{item!r}（合法取值：{', '.join(REFUND_STATUSES)}）"
            )
        if text not in cleaned_statuses:
            cleaned_statuses.append(text)

    stmt = select(Refund).order_by(Refund.created_at.desc(), Refund.refund_id.desc())
    if refund_id is not None:
        stmt = stmt.where(Refund.refund_id == int(refund_id))
    if order_id is not None:
        stmt = stmt.where(Refund.order_id == int(order_id))
    if user_id is not None:
        stmt = stmt.where(Refund.user_id == int(user_id))
    if cleaned_statuses:
        stmt = stmt.where(Refund.status.in_(cleaned_statuses))
    if created_from is not None:
        stmt = stmt.where(Refund.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(Refund.created_at <= created_to)
    stmt = stmt.limit(max(1, min(int(limit), MAX_LIMIT)))

    with audit_tool_call(
        LIST_TOOL,
        caller=caller,
        role=role,
        refund_id=refund_id,
        order_id=order_id,
        user_id=user_id,
        statuses=cleaned_statuses,
    ) as handle:
        db = session if session is not None else Session(get_read_engine())
        try:
            rows = list(db.execute(stmt).scalars())
        finally:
            if session is None:
                db.close()
        handle.rows = len(rows)
        handle.details["count"] = len(rows)
        return [_to_view(row) for row in rows]


def _to_view(row: Refund) -> RefundView:
    """`Refund` ORM 行 → `RefundView`（**唯一一处**做这个映射）。"""
    return RefundView(
        refund_id=int(row.refund_id),
        order_id=int(row.order_id),
        user_id=int(row.user_id),
        amount=Decimal(row.amount),
        status=str(row.status),
        reason=row.reason,
        created_at=row.created_at,
        decided_by=int(row.decided_by) if row.decided_by is not None else None,
        decided_at=row.decided_at,
        decision_note=row.decision_note,
        approver_role=row.approver_role,
    )


def refund_rows_for_output(
    views: Sequence[RefundView],
    *,
    role: str,
    caller: str | None = None,
) -> list[dict[str, Any]]:
    """退款单 → **可按角色出网**的普通 dict（脱敏在这里做，路由层被禁止调 `mask_pii`）。

    ⚠️ 为什么它必须在 `app/tools/` 而不是路由里：`tests/api/test_api_contracts.py`
    的 `FORBIDDEN_CALLS` 明令禁止 API 层调用 `mask_pii`（理由见 `app/tools/orders.py`
    的模块 docstring 第 2 条）。脱敏是层与层之间的缝，不是 HTTP 处理器的职责。

    ⚠️ `caller` 是 keyword-only 的（工具层契约：每个公开工具都要能被审计归属）。
    它**不改变输出**，只让「谁把退款数据取出去过」留下一条记录 ——
    退款单里有客户写的理由，出网这个动作本身值得留痕。

    ⚠️ **诚实说明：今天它对本表的所有字段都是空操作**。`refunds` 的字段
    （id / 金额 / 状态 / 时间 / 意见）没有一个登记在 `FIELD_RULES` 里，也没有一个长得像
    手机号/身份证/银行卡（`_VALUE_PATTERNS` 兜底也不会命中）。保留这一步的理由是
    **未来**：哪天给 `refunds` 加一列 PII（例如收款账号），只有一处入口需要接上规则表 ——
    而不是「每个出口各自记得脱敏」。这是刻意的过度设计，不是遗漏。
    """
    with audit_tool_call(
        OUTPUT_TOOL, caller=caller, role=role, count=len(views)
    ) as handle:
        rows = mask_pii([view.as_dict() for view in views], role=role).rows
        handle.rows = len(rows)
        handle.details["role"] = role
        return rows


#: 图侧退款节点的幂等键前缀（与手动路径的键区分开，便于在库里辨认「谁发起的」）
LLM_KEY_PREFIX: Final[str] = "llm-"

#: 哈希摘要截断长度：`llm-` + 24 位十六进制 = 28 字符，
#: 距 `MAX_IDEMPOTENCY_KEY_CHARS`（64）留足余量。
LLM_KEY_DIGEST_CHARS: Final[int] = 24


def llm_refund_key(*, subject: str, order_id: int, message: str) -> str:
    """给**图侧退款节点**派生一个**确定性**幂等键（变更 003）。

    ## 为什么这个函数在 `app/tools/` 而不是 `app/agents/`

    两个理由，都不是风格问题：

    1. **agent 层的依赖面被测试钉死了**：`tests/test_agents_state.py::test_agents_layer_only_reaches_out_through_app_modules`
       给 `app/agents/` 划了一份很小的 stdlib 白名单（`json`/`re`/`logging`…），
       **不含 `hashlib`**。那条约束是「六个 Agent 独立可测」的成本控制，不该为了一个键去放宽。
    2. **键的形状是写路径的契约**（列宽 64 + 字符集 `[A-Za-z0-9._:-]`），
       与 `validate_idempotency_key` 是同一类知识 —— 它属于 `tools`，不属于某个 Agent。

    ## 为什么必须确定性（这是本函数存在的全部理由）

    图在节点失败时会**从断点重放**（`app/graph/run.py`），同一个节点可能执行两次。
    随机键在重放时会建出**两张退款单**，各占一份额度 —— 客户会发现可退额度莫名少了一半。
    这类 bug 在单线程测试里永远不会暴露，所以键的确定性必须在**签名**上就看得出来：
    没有随机源、没有时间戳、同样的入参必然得到同样的输出。

    ## ⚠️ 键里**不含金额**（第一版含了，被重放用例抓出来）

    第一版的入参是 `(subject, order_id, amount, message)`。它在重放时**根本走不到**：
    第一次执行已经把订单额度占满，第二次执行时「剩余可退额度」已变成 0，
    于是节点在**候选判断**那一步就认定「没有可退订单」并返回一句话，
    **永远到不了幂等插入** —— 客户重放一次就得到「你没有可退订单」，
    而他明明刚刚提交过。

    去掉金额之后，键只由**稳定事实**派生（谁、哪一单、说了什么），
    重放时可以用同一个键**先回查**（`find_refund_by_idempotency_key`），
    直接命中第一次落的那一行。这条修正正是「幂等必须在判断之前发生」的体现：
    **回查早于任何业务判断**，否则判断会用「已经变了的世界」否掉一个已完成的事实。

    ## 为什么键里含「客户原话」

    只用 `(subject, order_id)` 的话，客户「改了理由再申请同一笔」会被判成同一次
    请求而静默丢弃。含原话之后语义正好是我们要的两条：
    **同一句话重放 = 同键（幂等）**，**不同的话 = 不同键（新意图）**。

    ## 为什么哈希而不是拼接

    `idempotency_key` 列宽 64、字符集不含中文与空格。客户原话直接拼进去必然撞形状校验；
    哈希把它压成固定长度的十六进制串，同时避免「原话原样进库」。
    """
    raw = f"{subject}|{int(order_id)}|{message}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:LLM_KEY_DIGEST_CHARS]
    return f"{LLM_KEY_PREFIX}{digest}"


def find_refund_by_idempotency_key(
    user_id: int,
    idempotency_key: str,
    *,
    session: Session | None = None,
) -> RefundView | None:
    """按 `(user_id, idempotency_key)` **回查**既有的退款单（图侧幂等的关键一步）。

    复合键与 `refunds` 上的唯一约束 `uq_refunds_user_id_idempotency_key` 一一对应 ——
    带上 `user_id` 是刻意的（单列全局唯一时，别人用了同名的键就会命中你的行）。

    用途与顺序都很重要：图侧退款节点必须在**任何业务判断之前**先调它。
    否则重放时「额度已被第一次占满」会把一次**已经发生**的提交否掉，
    客户得到「你没有可退订单」——而他刚刚提交过（实测：这正是本函数被写出来的原因）。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ToolContractError(f"user_id 必须是正整数，收到 {user_id!r}")
    if not str(idempotency_key or "").strip():
        raise ToolContractError("idempotency_key 不能为空")

    db = session if session is not None else Session(get_read_engine())
    try:
        row = db.execute(
            select(Refund).where(
                Refund.user_id == user_id,
                Refund.idempotency_key == str(idempotency_key).strip(),
            )
        ).scalar_one_or_none()
        return _to_view(row) if row is not None else None
    finally:
        if session is None:
            db.close()



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
    "DECISIONS",
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "DECISION_STATUS",
    "DECIDABLE_STATUSES",
    "DECIDE_TOOL",
    "IDEMPOTENCY_SCOPE",
    "INITIAL_STATUS",
    "LIST_TOOL",
    "MAX_NOTE_CHARS",
    "OUTPUT_TOOL",
    "REFUNDABLE_ORDER_STATUSES",
    "REQUEST_TOOL",
    "STATUS_TOOL",
    "RefundNotAllowed",
    "RefundNotDecidable",
    "RefundRef",
    "RefundView",
    "create_refund_request",
    "decide_refund",
    "find_refund_by_idempotency_key",
    "get_refund_status",
    "get_refund_view",
    "list_refunds_for_review",
    "llm_refund_key",
    "refund_rows_for_output",
    "remaining_refundable",
    "request_refund",
]
