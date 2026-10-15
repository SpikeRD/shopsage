"""账号接口：登录 / 刷新 / 我是谁（变更 001 / T10）。

```
POST /api/auth/login     {identifier, password} → TokenResponse
POST /api/auth/refresh   {refresh_token}        → TokenResponse（换一对新令牌）
GET  /api/auth/me                                → 当前令牌的身份（前端启动时自检用）
```

## 这个模块补的是**变更前根本不存在的那一环**

变更前「拿令牌」只有 `scripts/dev_token.py`（一个不需要密码的命令行工具），
而 `app/security/auth.py` 只负责「验」。真登录需要有人回答
「**这组凭据对应哪个账号**」—— 本模块是那个答案的 HTTP 出口，
判断逻辑全在 `app/security/accounts.py`（这里不许有业务判断，与 `9.api.md` §4 第 4 条一致）。

## 四条口径

### ① 登录响应的 `entry` 由服务端算

前端不复制「哪个角色进哪个界面」的规则（`accounts.entry_for_role`）。副本必然漂移，
而漂移的症状是「登录成功却停在空白页」——`web/src/lib/auth.ts` 自己已标注它的角色表
「是副本不是真相」。现在连入口也不用猜了。

### ② 登录与刷新都要**提交事务**

`accounts.authenticate()` 会写 `last_login_at`，并在口令散列需要升级时**原地重写**
（旧 `sha256$` → Argon2id）。不 commit 的话这两件事都会随请求结束被回滚，
症状是「每次登录都在升级散列，但库里永远是旧值」。

### ③ 刷新走**同一套校验**，但用刷新令牌

刷新令牌的 `typ=refresh`，所以 `parse_token(..., expected_type=refresh)` 才能解。
之后同样要查账号（状态、`token_version`）—— 这正是「停用后连刷新也换不到新令牌」的落点。

### ④ `me` 不加参数、不查库

它直接回显**已经过 `require_principal` 校验**的身份（那个依赖已经查过账号了）。
再查一次是纯粹的浪费，且会让「同一请求里两次查库看到不同结果」成为可能。

### ⑤ 数据库会话的类型注解写 `Any`，不 import `sqlalchemy`

`tests/api/test_api_contracts.py::test_no_data_layer_imports` 禁止 API 层 import
`sqlalchemy` / `psycopg` 等数据层库 —— 那条纪律管的是「API 层不许自己拼 SQL」。
本模块**确实不需要** SQLAlchemy 的任何东西：会话是 `Depends(get_db)` 注入的，
用到的只有「按主键查账号」与「按标识查账号」两个方法，它们都包在
`app/security/accounts.py` 里。为了一个类型注解去 import 整个 ORM 包，
只会让那条契约失去约束力（它按模块名判，import 了就放行）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.account_errors import account_errors
from app.api.deps import require_principal
from app.api.errors import ApiError
from app.api.schemas import LoginRequest, RefreshRequest, TokenResponse
from app.db.session import get_db
from app.security import accounts as accounts_svc
from app.security.auth import (
    DEFAULT_REFRESH_TTL_SECONDS,
    DEFAULT_TTL_SECONDS,
    TOKEN_TYPE_REFRESH,
    AuthError,
    Principal,
    create_session_token,
    parse_token,
)
from app.security.rbac import ROLE_PERMISSIONS

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(record: accounts_svc.AccountRecord, *, access: str, refresh: str) -> TokenResponse:
    """把「账号记录 + 两条令牌」拼成响应（**唯一一处**，登录与刷新共用）。"""
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=DEFAULT_TTL_SECONDS,
        refresh_expires_in=DEFAULT_REFRESH_TTL_SECONDS,
        account_id=record.account_id,
        subject=record.subject,
        subject_type=record.subject_type,
        role=record.role,
        entry=record.entry,
        permissions=sorted(ROLE_PERMISSIONS.get(record.role, frozenset())),
    )


def issue_tokens(record: accounts_svc.AccountRecord) -> TokenResponse:
    """按账号签发一对令牌（登录与刷新共用，保证两条路径的声明完全一致）。"""
    identity = record.signing_identity()
    access = create_session_token(identity, token_type="access")
    refresh = create_session_token(identity, token_type=TOKEN_TYPE_REFRESH)
    return _token_response(record, access=access, refresh=refresh)


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    db: Any = Depends(get_db),
) -> TokenResponse:
    """账号 + 口令换令牌。

    ⚠️ **不区分「账号不存在」与「口令错误」**（统一 401 + 同一句文案）：
    区分它们等于提供一个免费的账号枚举接口，而枚举出来的标识可以被集中爆破
    （那比盲猜高效得多）。`accounts.authenticate` 还做了耗时对齐（见其 docstring ①）。
    """
    with account_errors():
        record = accounts_svc.authenticate(db, body.identifier, body.password)
        # ② 必须提交：`authenticate` 写了 `last_login_at`，并可能在升级口令散列
        db.commit()
    return issue_tokens(record)


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    body: RefreshRequest,
    request: Request,
    db: Any = Depends(get_db),
) -> TokenResponse:
    """刷新令牌换一对新令牌。

    三条校验（顺序不能换）：

    1. **类型必须是 refresh**：拿访问令牌来刷新要报错，而不是「顺便也给一对新的」——
       否则「刷新」就成了一个能把短命令牌续成 14 天的口子；
    2. **账号必须存在且启用**：停用之后连刷新也换不到新令牌（否则停用只是延迟 30 分钟生效）；
    3. **世代号必须一致**：改密/改角色/改主体作废过的刷新令牌一律拒绝
       （与 `deps.principal_with_account` 是同一套判据，只是令牌类型不同）。

    ⚠️ 失败一律 401 + **同一句话**：不区分「格式不对」「账号不存在」「被停用」「被作废」。
    这些区别对合法用户没有用（他该做的就是重新登录），对攻击者却是有用的信息。
    """
    try:
        principal = parse_token(body.refresh_token, expected_type=TOKEN_TYPE_REFRESH)
    except AuthError as exc:
        raise _refresh_rejected(str(exc)) from exc

    if principal.account_id <= 0 or principal.token_version < 0:
        raise _refresh_rejected("这个刷新令牌是变更 001 之前的旧格式（没有账号/世代号声明）")

    record = accounts_svc.get_account(db, principal.account_id)
    if record is None:
        raise _refresh_rejected("刷新令牌对应的账号不存在")
    if not record.is_active:
        raise _refresh_rejected("账号已停用")
    if record.token_version != principal.token_version:
        raise _refresh_rejected("刷新令牌已被作废（口令或权限发生过变更）")
    return issue_tokens(record)


def _refresh_rejected(message: str) -> ApiError:
    """刷新失败的统一错误（401 + 固定后缀：让所有失败看起来一样）。"""
    return ApiError(401, "invalid_refresh_token", f"{message}：请重新登录")


@router.get("/me")
def me(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
    """回显当前令牌的身份（前端启动时用它判断「这令牌还能不能用」）。

    ⚠️ 不查库：`require_principal` 已经查过账号并做过状态/世代号校验（见模块 ④）。
    """
    return {
        "account_id": principal.account_id,
        "subject": principal.subject,
        "subject_type": principal.subject_type,
        "subject_id": principal.subject_id,
        "role": principal.role,
        "entry": accounts_svc.entry_for_role(principal.role),
        "permissions": sorted(ROLE_PERMISSIONS.get(principal.role, frozenset())),
    }


__all__ = ["issue_tokens", "router"]
