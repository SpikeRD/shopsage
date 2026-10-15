"""演示/运维用的**账号令牌**助手（变更 001 / T14）。

## 为什么需要这个模块

变更 001 之前，「拿一条能用的令牌」是 `auth.create_access_token(user_id, role=...)` ——
**不需要数据库**，也不需要账号。改成真登录之后这条路必须收窄，两个理由：

1. **主体类别错了会串会话**：`create_access_token` 只能签出「客户主体」（`sub="c7"`），
   于是 `--role internal_employee` 会签出「员工伪装成客户主体」的令牌。它能验过签名，
   但 `thread_id` 会按客户派生（`c7:...`），与真实员工账号（`e1:...`）不是同一个桶。
2. **鉴权依赖要查账号**：`app/api/deps.py::require_principal` 会比对令牌里的 `ver`
   与 `accounts.token_version`，并检查账号是否 `active`。没有账号行的令牌**一定被拒**。

所以「脚本侧签发」必须与「登录接口签发」**同一套语义**：都从账号行出发。
本模块就是那一条路径，三个脚本（`dev_token` / `api_demo` / `probe_internal_query`）共用它。

## 一条纪律

⚠️ **本模块只读账号，绝不改**：它不做 `last_login_at` 写入、不做口令升级。
那些是登录接口（`app/security/accounts.authenticate`）的副作用，
脚本不该因为「签了个令牌」而改掉演示库的状态。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.security import accounts as accounts_svc
from app.security.auth import (
    DEFAULT_REFRESH_TTL_SECONDS,
    DEFAULT_TTL_SECONDS,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    create_session_token,
)
from app.security.rbac import ROLE_PERMISSIONS


class DemoAccountError(Exception):
    """按账号取令牌失败（账号不存在 / 被停用 / 依赖不可用），带一句能照做的提示。"""


@dataclass(frozen=True)
class IssuedToken:
    """一条签好的令牌 + 它的身份现场（打印/自检用）。"""

    token: str
    identifier: str
    role: str
    subject: str
    account_id: int
    token_version: int
    kind: str
    ttl_seconds: int

    @property
    def permissions(self) -> list[str]:
        return sorted(ROLE_PERMISSIONS.get(self.role, frozenset()))

    @property
    def entry(self) -> str:
        return accounts_svc.entry_for_role(self.role)


def issue_for_identifier(
    identifier: str,
    *,
    kind: str = TOKEN_TYPE_ACCESS,
    ttl_seconds: int | None = None,
    require_active: bool = True,
) -> IssuedToken:
    """按登录标识（用户名/手机号）查账号并签发令牌。

    `require_active=False` 用来**故意**签一条「已停用账号」的令牌（验证 401 用）——
    与 `dev_token --kind refresh` 故意签刷新令牌是同一种用法。
    """
    from app.db.session import SessionLocal

    try:
        with SessionLocal() as db:
            record = accounts_svc.get_account_by_identifier(db, identifier)
    except Exception as exc:  # noqa: BLE001 - 连不上库时给一句能照做的话，而不是抛栈
        raise DemoAccountError(
            f"连不上数据库（{type(exc).__name__}: {exc}）：账号令牌需要读 `accounts` 表。\n"
            "   先确认 `DATABASE_URL` 可达、并跑过 `uv run alembic upgrade head`。"
        ) from exc

    if record is None:
        raise DemoAccountError(
            f"账号 {identifier!r} 不存在：先灌演示账号 `uv run python -m scripts.seed`，"
            "或在管理后台开一个。"
        )
    if require_active and not record.is_active:
        raise DemoAccountError(
            f"账号 {identifier!r} 已停用（status={record.status}）：停用账号签出的令牌会被鉴权拒掉。"
            "要验证 401 语义请显式传 require_active=False。"
        )

    resolved_ttl = ttl_seconds
    if resolved_ttl is None:
        resolved_ttl = (
            DEFAULT_REFRESH_TTL_SECONDS if kind == TOKEN_TYPE_REFRESH else DEFAULT_TTL_SECONDS
        )
    token = create_session_token(
        record.signing_identity(), token_type=kind, ttl_seconds=resolved_ttl
    )
    return IssuedToken(
        token=token,
        identifier=record.identifier,
        role=record.role,
        subject=record.subject,
        account_id=record.account_id,
        token_version=record.token_version,
        kind=kind,
        ttl_seconds=resolved_ttl,
    )


def issue_for_role(
    role: str, *, kind: str = TOKEN_TYPE_ACCESS, ttl_seconds: int | None = None
) -> IssuedToken:
    """按角色取**第一个**可用的启用账号（给「我不关心具体是谁、只要一个该角色的令牌」用）。

    ⚠️ 一个角色可能对应多个账号（例如两个客户、两个管理员），这里只取「账号主键最小」的
    那一个，并**按主键排序**保证结果稳定（否则同一台机器上两次调用可能给出不同的人，
    而演示脚本的输出应当是可比对的）。
    """
    from app.db.session import SessionLocal

    if role not in ROLE_PERMISSIONS:
        raise DemoAccountError(f"未登记的角色 {role!r}：只接受 {sorted(ROLE_PERMISSIONS)}")
    try:
        with SessionLocal() as db:
            candidates = list(
                accounts_svc.list_accounts(
                    db, role=role, status=accounts_svc.STATUS_ACTIVE, limit=50
                )
            )
    except Exception as exc:  # noqa: BLE001
        raise DemoAccountError(f"连不上数据库（{type(exc).__name__}: {exc}）") from exc

    if not candidates:
        raise DemoAccountError(
            f"没有 role={role!r} 的启用账号：先灌演示账号 `uv run python -m scripts.seed`"
            "（它只建 admin / customer / internal_employee 三个角色），或在管理后台开一个。"
        )
    chosen = sorted(candidates, key=lambda view: view.account_id)[0]
    return issue_for_identifier(chosen.identifier, kind=kind, ttl_seconds=ttl_seconds)


def resolve(
    *,
    identifier: str | None = None,
    role: str | None = None,
    kind: str = TOKEN_TYPE_ACCESS,
    ttl_seconds: int | None = None,
) -> IssuedToken:
    """优先按标识、否则按角色取一条令牌。"""
    if identifier:
        return issue_for_identifier(identifier, kind=kind, ttl_seconds=ttl_seconds)
    if role:
        return issue_for_role(role, kind=kind, ttl_seconds=ttl_seconds)
    raise DemoAccountError("必须给 `identifier` 或 `role` 之一")


__all__ = [
    "DemoAccountError",
    "IssuedToken",
    "issue_for_identifier",
    "issue_for_role",
    "resolve",
]
