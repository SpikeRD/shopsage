"""账号接口的异常映射（变更 001：`app/api/auth.py` 与 `app/api/admin.py` 共用）。

## 为什么单独一个模块，而不是在路由里各写一份 `try/except`

`app/security/accounts.py` 抛的是**领域异常**（`AccountError` / `AccountAuthError` /
`AccountPermissionError`），路由层要把它们翻成 HTTP 错误信封（`ApiError` → `errors.py`
统一渲染）。两个路由模块（登录三个口、管理五个口）都要做这件事，
抄两份的后果是「登录返回 401 而管理返回 400」这种**同一类错误两种状态码**的漂移 ——
而它只会在某一条路径上被用户撞到。

## 状态码口径（每一档都有理由，不要随手改）

| 异常 | 状态码 | 为什么 |
|---|---|---|
| `AccountAuthError` | **401** | 「凭据不对」。文案**不区分**账号不存在与口令错误（防枚举，见变更单 §3.6） |
| `AccountPermissionError` | **403** | 「你的身份不允许做这件事」（例如停用最后一个管理员）。与 401 的区别是：身份是有效的，只是这个动作不被允许 |
| `AccountError` | **422** | 「参数不对」（角色未登记、主体不存在、标识被占用）。是**输入**的问题，不是身份的问题 |
| 其它异常 | 交给全局处理器 | 不在这里吞：吞掉会把 500 伪装成 4xx，而排障时最需要知道的就是「这是 bug 还是用户输入错了」 |
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from app.api.errors import ApiError
from app.security.accounts import AccountAuthError, AccountError, AccountPermissionError

#: 错误码（前端按它做分支，不要按文案）
CODE_UNAUTHORIZED: str = "invalid_credentials"
CODE_FORBIDDEN: str = "account_forbidden"
CODE_INVALID: str = "invalid_account"


@contextmanager
def account_errors() -> Iterator[None]:
    """把账号领域的异常翻成 `ApiError`。用法：`with account_errors(): ...`"""
    try:
        yield
    except AccountAuthError as exc:
        # ⚠️ 401 但**不**加 `WWW-Authenticate: Bearer`：那个头是「你该带令牌来」的语义，
        # 而这里的失败是「你带来的凭据不对」。带了它会让一些客户端（以及浏览器的
        # 默认提示）去弹一个「输入用户名密码」的原生对话框 —— 而本接口要的是 JSON 体。
        raise ApiError(401, CODE_UNAUTHORIZED, str(exc)) from exc
    except AccountPermissionError as exc:
        raise ApiError(403, CODE_FORBIDDEN, str(exc)) from exc
    except AccountError as exc:
        raise ApiError(422, CODE_INVALID, str(exc)) from exc


__all__ = [
    "CODE_FORBIDDEN",
    "CODE_INVALID",
    "CODE_UNAUTHORIZED",
    "account_errors",
]
