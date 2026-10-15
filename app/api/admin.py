"""账号管理接口（变更 001 / T11）：管理员开账号、停用、改角色、重置口令。

```
GET   /api/admin/accounts                    列表（可按 role / status / subject_type 筛）
POST  /api/admin/accounts                    建账号
PATCH /api/admin/accounts/{account_id}       改状态 / 改角色
POST  /api/admin/accounts/{account_id}/password   重置口令
```

## 四条纪律

### ① 每一个口都要 `PERM_ADMIN`，**没有例外**

包括看起来「只是读一下」的列表接口：账号列表能看出「这家公司有哪些人、谁是管理员」，
那是**组织情报**（社工的第一步就是拿到管理员名单）。
用装饰器统一声明而不是在函数体里 `if not has_admin: raise` —— 后者的漏写不会有任何报错。

⚠️ `rbac.require_permission` 是按**关键字参数或第一个位置参数的 `role` 属性**取角色的。
本模块的写法是 `actor: Actor = Depends(require_actor)` + `@require_permission(PERM_ADMIN)`，
装饰器会从 `Actor.role` 取到它（`Actor` 是 dataclass，带 `role` 字段）。

### ② 绝不回显任何口令

响应体里没有 `password_hash`（`AccountViewResponse` 的字段集就是判据），
重置口令的响应是**被重置后的账号**，不是新口令本身。
理由与 `accounts.reset_password` 的 docstring 同源：口令一旦进响应/日志/截图就等价于泄露。

### ③ 「不能停用最后一个管理员」由服务层兜住

路由层**不再写一遍**这条判断。`accounts.update_account` 里有两条自我保护规则
（不能操作自己、不能减少到零个管理员），而且它是在**同一个事务里**读计数再写的。
在路由层再判一次等于制造第二处真相，而两处判断在并发下的结论可能不同。

### ④ 建号响应用 201，其余 200

`201 Created` 让客户端能区分「建好了」与「查到了」——
而本模块的 `POST` 是**唯一**一个会新建行的地方，
混用 200 会让「重试了一次建号请求」在客户端看起来与首次成功一样。

### ⑤ 数据库会话的类型注解写 `Any`

理由与 `app/api/auth.py` 的模块 docstring ⑤ 完全一样：API 层不 import `sqlalchemy`
（`tests/api/test_api_contracts.py::test_no_data_layer_imports` 钉住），
而本模块不需要 SQLAlchemy 的任何东西 —— 会话是 `Depends(get_db)` 注入的，
所有查询与写入都在 `app/security/accounts.py` 里。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.account_errors import account_errors
from app.api.deps import require_actor
from app.api.schemas import (
    AccountCreateRequest,
    AccountListResponse,
    AccountUpdateRequest,
    AccountViewResponse,
    PasswordResetRequest,
)
from app.db.session import get_db
from app.security import accounts as accounts_svc
from app.security.rbac import PERM_ADMIN, require_permission

router = APIRouter(prefix="/admin", tags=["admin"])


def _view(
    account: accounts_svc.AccountView, *, entry: str | None = None, is_admin: bool | None = None
) -> AccountViewResponse:
    """`AccountView` → 响应体（**唯一一处**做这个映射）。

    `entry` / `is_admin` 允许显式传入：`AccountView` 自己不带它们（它属于「数据」层，
    而「这个角色能进哪个界面」是 `accounts.entry_for_role` 的判断）。
    用显式参数而不是在响应模型里现算，是为了让「响应里每个字段的来源」在调用点可见。
    """
    return AccountViewResponse(
        account_id=account.account_id,
        identifier=account.identifier,
        role=account.role,
        status=account.status,
        subject_type=account.subject_type,
        subject_id=account.subject_id,
        entry=entry if entry is not None else accounts_svc.entry_for_role(account.role),
        is_admin=is_admin if is_admin is not None else account.is_admin,
        created_at=account.created_at.isoformat() if account.created_at else None,
        last_login_at=account.last_login_at.isoformat() if account.last_login_at else None,
    )


@router.get("/accounts", response_model=AccountListResponse)
@require_permission(PERM_ADMIN)
def list_accounts(
    actor: Any = Depends(require_actor),
    db: Any = Depends(get_db),
    account_role: str | None = Query(
        default=None,
        alias="role",
        description="按角色筛（取值同 `ROLE_PERMISSIONS`）。**参数对外仍叫 `role`**，见下",
    ),
    account_status: str | None = Query(
        default=None, alias="status", description="按状态筛：`active` / `disabled`"
    ),
    subject_type: str | None = Query(default=None, description="`customer` / `employee`"),
    limit: int = Query(default=100, ge=1, le=500, description="最多返回多少条"),
) -> AccountListResponse:
    """账号列表（**要 `admin` 权限**，见模块 ①）。

    ⚠️ **这个筛选参数的 Python 名字叫 `account_role`，不叫 `role`** —— 而对外仍是 `?role=`。
    原因是实测撞到的一个坑：`@require_permission` 会从**第一个位置参数的身份对象**取角色
    （变更 001 起它优先于关键字参数），但第一版的解析顺序是反的，
    于是这个筛选参数被当成了调用者身份 —— 报出「角色 None 缺少权限：admin」。
    服务层的顺序已经修好（`rbac.require_permission` 的 docstring 记了这件事），
    这里再改一次名字是**第二道防线**：让「身份」与「筛选条件」在任何读代码的人眼里
    都不是同一个词。`alias="role"` 保证前端看到的接口契约不变。
    """
    del actor  # 权限已由装饰器判定；这里不需要它的其它字段
    rows = accounts_svc.list_accounts(
        db, role=account_role, status=account_status, subject_type=subject_type, limit=limit
    )
    return AccountListResponse(
        accounts=[_view(item) for item in rows],
        total=len(rows),
    )


@router.post(
    "/accounts", response_model=AccountViewResponse, status_code=status.HTTP_201_CREATED
)
@require_permission(PERM_ADMIN)
def create_account(
    body: AccountCreateRequest,
    actor: Any = Depends(require_actor),
    db: Any = Depends(get_db),
) -> AccountViewResponse:
    """建账号（**管理员开账号**这条需求的落点）。

    服务层会校验：角色必须在 `ROLE_PERMISSIONS` 里、`subject_id` 必须真的存在
    （`accounts.subject_id` 没有外键，见 `models.Account` 的注释）、
    标识与主体都唯一。这里只做异常映射（`account_errors`）。
    """
    del actor
    with account_errors():
        view = accounts_svc.create_account(
            db,
            identifier=body.identifier,
            password=body.password,
            role=body.role,
            subject_type=body.subject_type,
            subject_id=body.subject_id,
            status=body.status,
        )
        db.commit()
    return _view(view)


@router.patch("/accounts/{account_id}", response_model=AccountViewResponse)
@require_permission(PERM_ADMIN)
def update_account(
    account_id: int,
    body: AccountUpdateRequest,
    actor: Any = Depends(require_actor),
    db: Any = Depends(get_db),
) -> AccountViewResponse:
    """改状态 / 改角色。

    两条自我保护规则由服务层兜住（**路由层不再写一遍**，见模块 ③）：
    不能停用或降级自己；不能把最后一个启用的管理员停用或降级。
    任何实际变化都会自增 `token_version` —— 也就是让该账号**所有在途令牌立刻失效**。
    """
    with account_errors():
        view = accounts_svc.update_account(
            db,
            account_id,
            actor_account_id=actor.account_id,
            status=body.status,
            role=body.role,
        )
        db.commit()
    return _view(view)


@router.post("/accounts/{account_id}/password", response_model=AccountViewResponse)
@require_permission(PERM_ADMIN)
def reset_password(
    account_id: int,
    body: PasswordResetRequest,
    actor: Any = Depends(require_actor),
    db: Any = Depends(get_db),
) -> AccountViewResponse:
    """重置口令（**就地设新口令，绝不回显**，见模块 ②）。

    ⚠️ 这个动作会让目标账号的在途令牌立刻失效（`token_version + 1`）——
    这正是「改密即踢下线」的落点，也是「口令被泄露后重置能夺回账号」的前提。

    ⚠️ 它与「本人改口令」是两条不同的路径：本人改口令**必须验旧口令**
    （`accounts.change_own_password`，由将来的 `/api/auth/change-password` 暴露）。
    管理员这条路不需要旧口令，因为管理员的权限本身就已经是更强的凭据
    （而他也不可能知道别人的旧口令）。
    """
    del actor  # 管理员身份已由 `@require_permission` 判定；重置不需要再带操作者 id 进服务层
    with account_errors():
        view = accounts_svc.reset_password(db, account_id, body.new_password)
        db.commit()
    return _view(view)


__all__ = ["router"]
