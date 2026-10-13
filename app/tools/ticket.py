"""工单工具：**建工单 / 查工单状态**（`6.tools.md` 第 2 节的 `create_ticket` / `get_ticket_status`）。

这是「写路径」的第一个具体例子，也是 D1 的落点之一：

* **LLM 拿到的是「建工单」这个动作，不是写库能力**。它传的是 `type`（枚举）、`user_id`、
  幂等键这类**显式参数**；SQL 由本模块固定生成，模型没有任何机会拼出 `INSERT`/`UPDATE`。
  这就是 `docs/architecture.md`「读写路径分离」在代码里的样子。
* **权限在工具内校验**（`PERM_WRITE_TICKET`），不依赖调用方自觉：
  第 7 步的 agent、第 9 步的 API、第 15 步的 MCP 都从同一个门口进；
  把校验放在调用方，等于每加一个入口就多一次「忘写校验」的机会。
* **幂等由数据库唯一约束保证**：见 `app/tools/write_path.py` 与
  `alembic/versions/20260929_29d823809e4f_add_idempotency_key_to_tickets_and_.py`。

关于返回值的一个刻意选择
------------------------

`create_ticket()` 返回 `TicketRef`，**不含 `created`（是否本次新建）字段**。
幂等的语义就是「同一个键的多次调用返回**同一个**结果」——
带一个每次都变的标志位，`k1 == k2` 就不成立，调用方得自己挑字段比较，
幂等就从「类型保证」退化成了「口头约定」。
「这次是新建还是命中」属于**审计字段**（`details.created`）：它影响运维视角
（要不要对重复请求告警），不影响业务语义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import TICKET_TYPES, Ticket, User
from app.db.session import get_read_engine
from app.observability.instrument import record_tool_call
from app.security.rbac import PERM_WRITE_TICKET, PermissionDenied, has_permission
from app.tools.audit import audit_tool_call
from app.tools.errors import ToolContractError, translate_db_error
from app.tools.write_path import insert_once, validate_idempotency_key, write_session

CREATE_TOOL: Final[str] = "create_ticket"
STATUS_TOOL: Final[str] = "get_ticket_status"

# 幂等键的唯一性命名空间 = (user_id, idempotency_key)，与迁移里的复合唯一约束一一对应
IDEMPOTENCY_SCOPE: Final[tuple[str, ...]] = ("user_id", "idempotency_key")

# 新建工单的初始状态（`models.TICKET_STATUSES` 的第一个，与库的 server_default 一致）
INITIAL_STATUS: Final[str] = "open"


@dataclass(frozen=True)
class TicketRef:
    """工单的**业务引用**（不含 `created` 标志，见模块 docstring）。"""

    ticket_id: int
    user_id: int
    type: str
    status: str


def _check_ticket_type(ticket_type: Any) -> str:
    if ticket_type not in TICKET_TYPES:
        raise ToolContractError(
            f"工单类型 {ticket_type!r} 不合法，只能是：{', '.join(TICKET_TYPES)}"
            "（枚举写错也照样会撞库里的 CHECK 约束，但在工具入口先拦能给出可读的报错）"
        )
    return str(ticket_type)


def create_ticket(
    user_id: int,
    ticket_type: str,
    *,
    idempotency_key: str,
    caller: str | None = None,
    role: str = "customer",
    assignee_id: int | None = None,
    session: Session | None = None,
) -> TicketRef:
    """建一张工单；同一个 `(user_id, idempotency_key)` 重复调用只会建一张。

    参数：
        user_id: 工单归属的客户。
        ticket_type: 工单类型（`TICKET_TYPES` 之一，如 `refund` / `logistics`）。
        idempotency_key: 幂等键（必填，形状校验见 `write_path.validate_idempotency_key`）。
        caller: 审计字段「谁在调用」。`None` = 用**入口声明的**调用者
            （`app/tools/audit.caller_override`，第 15 步 MCP 薄壳走这条）。
        role: 调用方角色，决定有没有 `write:ticket` 权限（默认 `customer`：客户可以建工单）。
        assignee_id: 预分配的客服（可空；真实系统通常由派单规则决定）。
        session: 复用调用方的事务（测试/组合操作，**提交由调用方负责**）；`None` = 自己开写会话并提交。

    异常：
        `PermissionDenied`：角色缺 `write:ticket`；
        `ToolContractError`：类型/幂等键/用户非法，或没有任何调用者信息（审计不可绕过）；
        `RetryableToolError` / `PermanentToolError`：数据库侧失败（分类见 `errors.py`）。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ToolContractError(f"user_id 必须是正整数，收到 {user_id!r}")

    # ⚠️ 审计包住**参数校验与权限判定**：被拒的调用（枚举写错、缺权限、越权）
    #    恰恰是最需要留痕的一类。「先校验、通过了才开始审计」会让 D1/D2 的证据链缺一大块 ——
    #    实测就是这么踩到的：越权用例抛了 PermissionDenied，日志里却一条记录都没有。
    #
    # 第 11 步：埋点包在**审计之外**（`with record_tool_call(...)` 再进 `with audit_tool_call(...)`）。
    # 顺序有理由：审计只记「谁调了什么」（供取证），而埋点要记「这次调用花了多久、成没成」
    # —— 后者必须包含**被拒的那条路径**（一次被拒的调用同样消耗了时间，
    # 而「拒绝率突然升高」是权限配置出错的第一信号）。
    # 所以埋点在外、审计在内：审计的 `__exit__` 先写完它的记录，埋点再收口。
    with record_tool_call(tool=CREATE_TOOL) as call:
        with audit_tool_call(
            CREATE_TOOL,
            caller=caller,
            user_id=user_id,
            ticket_type=ticket_type,
            idempotency_key=idempotency_key,
            role=role,
            assignee_id=assignee_id,
        ) as handle:
            checked_type = _check_ticket_type(ticket_type)
            checked_key = validate_idempotency_key(idempotency_key)

            # 权限闸：与 SQL 守卫（拦 SQL 形态）性质不同 —— 这里拦的是「这个身份能不能做这件事」
            if not has_permission(role, PERM_WRITE_TICKET):
                raise PermissionDenied(
                    f"角色 {role or '(未提供)'} 缺少权限：{PERM_WRITE_TICKET}（建工单）",
                    role=role,
                    missing=[PERM_WRITE_TICKET],
                )

            try:
                with write_session(session) as db:
                    if db.execute(select(User.user_id).where(User.user_id == user_id)).first() is None:
                        # 先给一条能读懂的报错，而不是让外键约束抛 23503（那种报文对 agent 毫无信息量）
                        raise ToolContractError(
                            f"用户不存在：user_id={user_id}（工单必须挂在真实用户上）"
                        )

                    ticket_id, created = insert_once(
                        db,
                        Ticket,
                        {
                            "user_id": user_id,
                            "type": checked_type,
                            "status": INITIAL_STATUS,
                            "assignee_id": assignee_id,
                            "idempotency_key": checked_key,
                        },
                        conflict_on=IDEMPOTENCY_SCOPE,
                        lookup={"user_id": user_id, "idempotency_key": checked_key},
                    )
                    # 幂等命中时返回**既有行**的真实字段，而不是把本次请求参数回显成结果：
                    # 两次调用可能带着不同的 type/assignee，回显会让调用方以为「按我说的写进去了」。
                    row = db.execute(
                        select(Ticket.type, Ticket.status, Ticket.user_id).where(
                            Ticket.ticket_id == ticket_id
                        )
                    ).one()
                    if session is None:
                        db.commit()
            except SQLAlchemyError as exc:
                raise translate_db_error(exc, action=f"建工单（{CREATE_TOOL}）") from exc
            # ToolContractError / PermissionDenied 直接冒泡：审计在 __exit__ 里照旧记一条 error

            handle.rows = 1
            handle.details.update(
                {
                    "created": created,  # ← 「本次是否新建」在这里，不在返回值里
                    "idempotent_hit": not created,
                    "ticket_id": int(ticket_id),
                    "requested_type": checked_type,
                    "stored_type": str(row.type),
                }
            )
            if not created and str(row.type) != checked_type:
                handle.details["idempotency_type_mismatch"] = True

            # 埋点的结果标签：**幂等命中要单独可见**（见 `TOOL_OUTCOME` 的说明）——
            # 「一直在命中」是上游重复投递的信号，而它不该混在 `created` 里被平均掉。
            call.outcome("idempotent_hit" if not created else "created", rows=1)
            call.set_attributes({"stage": "write"})

            return TicketRef(
                ticket_id=int(ticket_id),
                user_id=int(row.user_id),
                type=str(row.type),
                status=str(row.status),
            )


def get_ticket_status(
    ticket_id: int,
    *,
    caller: str | None = None,
    requester_user_id: int | None = None,
    session: Session | None = None,
) -> TicketRef | None:
    """查工单状态；`requester_user_id` 给出时按**对客规则**拦越权。

    两条路径的差别只有一处，但很关键：

    * `requester_user_id=<客户本人>`：查的是别人的工单 → `PermissionDenied`；
    * `requester_user_id=None`：内部路径（调用方必须是已通过 RBAC 的内部角色，
      第 9 步的 API 层负责这一步，本函数不重复判角色）。

    **403 还是 404？** 查到别人的工单时本函数抛 `PermissionDenied`（403 语义），
    而不是假装「不存在」。理由：① 越权尝试必须在审计与指标里**看得见**，
    伪装成 404 就让 D1 的攻击用例失去了信号；② `ticket_id` 是自增整数，
    本来就具备可枚举性，「假装不存在」并不能真的隐藏什么。
    代价要认下来：这给了调用方一个「这个 id 存在」的判定。
    若某天业务要求「连存在性都不许探测」，改法是把这里的第二种情况也返回 `None`，
    并接受「越权尝试不再有独立信号」——这是一次有意的取舍，不是遗漏。

    读路径走**只读引擎**（`get_read_engine()`）：查状态是纯读操作，
    没有理由占用写引擎的连接预算（C1 的连接数就是被这类顺手读吃掉的）。
    """
    if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id < 1:
        raise ToolContractError(f"ticket_id 必须是正整数，收到 {ticket_id!r}")
    if requester_user_id is not None and (
        isinstance(requester_user_id, bool)
        or not isinstance(requester_user_id, int)
        or requester_user_id < 1
    ):
        raise ToolContractError(f"requester_user_id 必须是正整数或 None，收到 {requester_user_id!r}")

    with audit_tool_call(
        STATUS_TOOL, caller=caller, ticket_id=ticket_id, requester_user_id=requester_user_id
    ) as handle:
        try:
            db = session if session is not None else Session(get_read_engine())
            try:
                row = db.execute(
                    select(Ticket.ticket_id, Ticket.user_id, Ticket.type, Ticket.status).where(
                        Ticket.ticket_id == ticket_id
                    )
                ).one_or_none()
            finally:
                if session is None:
                    db.close()
        except SQLAlchemyError as exc:
            raise translate_db_error(exc, action=f"查工单状态（{STATUS_TOOL}）") from exc

        if row is None:
            handle.rows = 0
            handle.details["found"] = False
            return None

        if requester_user_id is not None and int(row.user_id) != requester_user_id:
            handle.rows = None
            handle.details.update({"found": True, "owned_by_requester": False})
            raise PermissionDenied(
                f"越权拦截：工单 {ticket_id} 属于 user_id={row.user_id}，"
                f"调用方身份是 user_id={requester_user_id}",
                role="customer",
            )

        handle.rows = 1
        handle.details.update({"found": True, "owned_by_requester": True, "status": str(row.status)})
        return TicketRef(
            ticket_id=int(row.ticket_id),
            user_id=int(row.user_id),
            type=str(row.type),
            status=str(row.status),
        )


__all__ = [
    "CREATE_TOOL",
    "IDEMPOTENCY_SCOPE",
    "INITIAL_STATUS",
    "STATUS_TOOL",
    "TicketRef",
    "create_ticket",
    "get_ticket_status",
]
