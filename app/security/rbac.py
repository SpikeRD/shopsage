"""越权拦截：**对客走行级过滤（D1），对内走 RBAC**（`4.security.md` 第 4 节第 4 条）。

两条路径必须分开，因为它们的授权粒度根本不同：

| 路径 | 问题 | 手段 | 唯一真相在哪 |
|---|---|---|---|
| 对客（客户查自己的订单） | 「能不能看到**这一行**」 | 行级过滤：强制 `WHERE user_id = 当前用户` | `app/db/queries.py`（本模块只做**校验**，不另生成 SQL） |
| 对内（员工查全站/财务数据） | 「这个**角色**能不能做这件事」 | RBAC：角色 → 权限点 | 本模块的 `ROLE_PERMISSIONS` |

⚠️ 本模块**刻意不实现**「根据 user_id 现场拼一段 WHERE」的辅助函数 ——
`docs/runbook/3.db-runbook.md` §5.3 的决策记录写得很清楚：过滤只允许有一处真相，
第二处（无论视图还是 helper）都会让「应用忘了过滤」和「helper 已经过滤了」互相掩护。
所以对客路径这里只做两件事：
1. `row_filter_for_customer()`：**校验**调用方声明要查的 user_id 与当前身份一致，不一致直接拒；
2. `assert_row_filter_present()`：**校验**编译后的 SQL 真的带上了行级过滤列（拒绝裸查询）。
两条都通过之后，SQL 由 `app/db/queries.py` 生成并执行 —— 过滤仍然只有一处真相。
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, TypeVar

from app.security.policy import CUSTOMER_ROLE, ROLE_MAX_SENSITIVITY, ROW_FILTER_COLUMN

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注；运行期不 import，省掉 sqlalchemy 依赖
    from sqlalchemy import Select

F = TypeVar("F", bound=Callable[..., Any])

# ── 权限点 ───────────────────────────────────────────────────────────────────
# 用「动词:资源」的字符串而不是布尔开关：新增能力时只加权限点，不用给每个角色加字段。
PERM_READ_OWN: Final[str] = "read:own"
PERM_READ_ALL_ORDERS: Final[str] = "read:all_orders"
PERM_READ_FINANCE: Final[str] = "read:finance"
PERM_WRITE_REFUND: Final[str] = "write:refund"
PERM_WRITE_TICKET: Final[str] = "write:ticket"
PERM_ADMIN: Final[str] = "admin"

# 角色 → 权限点集合。**表里没有的角色 = 没有任何权限**（默认拒绝）。
ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    # 客户：只能看自己的东西，且只能走对客路径（不能读全站订单）
    CUSTOMER_ROLE: frozenset({PERM_READ_OWN, PERM_WRITE_TICKET}),
    # 客服：能建工单、能代客户发起退款申请，但仍不能读全站订单列表
    "customer_service": frozenset({PERM_READ_OWN, PERM_WRITE_TICKET, PERM_WRITE_REFUND}),
    # 普通员工：可读全站订单（对内分析路径），财务数据仍然不可见
    "internal_employee": frozenset(
        {PERM_READ_OWN, PERM_READ_ALL_ORDERS, PERM_WRITE_TICKET, PERM_WRITE_REFUND}
    ),
    "manager": frozenset(
        {
            PERM_READ_OWN,
            PERM_READ_ALL_ORDERS,
            PERM_WRITE_TICKET,
            PERM_WRITE_REFUND,
        }
    ),
    # 财务审计：唯一能读财务级数据的角色
    "finance_auditor": frozenset(
        {PERM_READ_OWN, PERM_READ_ALL_ORDERS, PERM_READ_FINANCE, PERM_WRITE_TICKET}
    ),
    # 管理员：全量权限（运维用，应尽量少人持有）
    "admin": frozenset(
        {
            PERM_READ_OWN,
            PERM_READ_ALL_ORDERS,
            PERM_READ_FINANCE,
            PERM_WRITE_REFUND,
            PERM_WRITE_TICKET,
            PERM_ADMIN,
        }
    ),
}


class PermissionDenied(Exception):
    """权限不足（`require_role` / `require_permission` 抛这个）。

    单独一个异常类型而不是复用 `SQLSecurityError`：前者是「身份不允许」，
    后者是「SQL 不允许」，在日志与告警里要能一眼区分（排障路径完全不同）。
    """

    def __init__(self, message: str, *, role: str = "", missing: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.role = role
        self.missing = tuple(missing)


# ── 对内：RBAC ───────────────────────────────────────────────────────────────


def permissions_for(role: str) -> frozenset[str]:
    """角色的权限点集合；**未登记角色返回空集**（默认拒绝，而不是报错放行）。"""
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: str, permission: str) -> bool:
    return permission in permissions_for(role)


def require_permission(*permissions: str, role_from: str = "role") -> Callable[[F], F]:
    """装饰器：要求调用方角色具备给定权限点（全部满足）。

    `role_from` 指定「从哪个参数/属性取角色」：默认按关键字参数 `role=` 取，
    取不到则回退到 `self`/第一个参数上的 `role` 属性（兼容 FastAPI 依赖对象与 dataclass 身份对象）。

    既可装饰同步函数，也可装饰 `async def`（FastAPI 路由都是 async 的）—— 用一个 wrapper 同时支持
    两种形态，避免「装饰器只对同步函数生效」这种隐藏坑。
    """

    def decorator(func: F) -> F:
        required = tuple(permissions)

        def _resolve_role(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
            if role_from in kwargs:
                return str(kwargs[role_from])
            if args:
                first = args[0]
                candidate = getattr(first, role_from, None)
                if candidate is not None:
                    return str(candidate)
            return ""

        def _check(role: str) -> None:
            missing = [p for p in required if not has_permission(role, p)]
            if missing:
                raise PermissionDenied(
                    f"角色 {role or '(未提供)'} 缺少权限：{', '.join(missing)}",
                    role=role,
                    missing=missing,
                )

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                _check(_resolve_role(args, kwargs))
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            _check(_resolve_role(args, kwargs))
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def require_role(*roles: str) -> Callable[[F], F]:
    """装饰器：要求角色在给定白名单里（`4.security.md` 第 2 节的 `require_role`）。"""
    allowed = frozenset(roles)

    def decorator(func: F) -> F:
        def _resolve_role(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
            if "role" in kwargs:
                return str(kwargs["role"])
            if args:
                candidate = getattr(args[0], "role", None)
                if candidate is not None:
                    return str(candidate)
            return ""

        def _check(role: str) -> None:
            if role not in allowed:
                raise PermissionDenied(
                    f"角色 {role or '(未提供)'} 不在允许列表：{', '.join(sorted(allowed))}",
                    role=role,
                )

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                _check(_resolve_role(args, kwargs))
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            _check(_resolve_role(args, kwargs))
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


# ── 对客：行级过滤的校验（不生成 SQL）────────────────────────────────────────


@dataclass(frozen=True)
class CustomerIdentity:
    """对客请求的身份：`user_id` 是行级过滤的唯一依据，`role` 决定脱敏等级。"""

    user_id: int
    role: str = CUSTOMER_ROLE


def row_filter_for_customer(identity: CustomerIdentity, requested_user_id: int) -> str:
    """校验「请求里要查的 user_id」与「当前身份」一致，返回过滤列名。

    为什么是**校验**而不是**生成 SQL**：见模块 docstring —— 生成 SQL 会制造第二处真相。
    这里把「你是谁」与「你要查谁」对齐，ORM 侧的条件仍由 `app/db/queries.py` 写。

    ⚠️ 内部角色（员工/审计）走的是 RBAC 路径，**不能**通过这里获准查任意 user_id：
    要跨用户查必须显式持有 `read:all_orders` 权限，且那条路径不带行级过滤、走脱敏出口。
    """
    if identity.role != CUSTOMER_ROLE and not has_permission(identity.role, PERM_READ_ALL_ORDERS):
        raise PermissionDenied(
            f"角色 {identity.role} 不能查询任意用户的订单（缺少 {PERM_READ_ALL_ORDERS}）",
            role=identity.role,
            missing=[PERM_READ_ALL_ORDERS],
        )
    if identity.role == CUSTOMER_ROLE and requested_user_id != identity.user_id:
        raise PermissionDenied(
            f"越权拦截：身份 user_id={identity.user_id} 请求查询 user_id={requested_user_id} 的数据",
            role=identity.role,
        )
    return ROW_FILTER_COLUMN


def assert_row_filter_present(stmt: "Select", *, column: str = ROW_FILTER_COLUMN) -> str:
    """校验 SQLAlchemy 语句编译后**真的**带上了行级过滤列，返回 SQL 文本。

    与 `app/db/queries.py::assert_row_filtered` 的分工：那个检查「有没有 WHERE」，
    这个检查「WHERE 里有没有**行级过滤那一列**」—— `WHERE 1=1` 能过前者、过不了后者。
    两者叠加才堵得住「形似过滤、实则全表」。对应 `tests/security` 里的越权用例。
    """
    import re

    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    pattern = re.compile(rf"\bWHERE\b[\s\S]*\b{re.escape(column)}\b\s*=", re.IGNORECASE)
    if not pattern.search(sql):
        raise PermissionDenied(
            f"越权拦截：查询缺少行级过滤 `WHERE {column} = ...`（裸查询不允许执行）"
        )
    return sql


def visible_sensitivity_ceiling(role: str) -> int:
    """角色能看到的最高敏感等级；未登记角色 = -1（什么都看不到，默认拒绝）。"""
    return ROLE_MAX_SENSITIVITY.get(role, -1)


__all__ = [
    "PERM_ADMIN",
    "PERM_READ_ALL_ORDERS",
    "PERM_READ_FINANCE",
    "PERM_READ_OWN",
    "PERM_WRITE_REFUND",
    "PERM_WRITE_TICKET",
    "ROLE_PERMISSIONS",
    "CustomerIdentity",
    "PermissionDenied",
    "assert_row_filter_present",
    "has_permission",
    "permissions_for",
    "require_permission",
    "require_role",
    "row_filter_for_customer",
    "visible_sensitivity_ceiling",
]
