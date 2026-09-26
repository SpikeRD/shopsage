"""鉴权：JWT 签发/校验 + FastAPI `get_current_user` 依赖（`4.security.md` 第 2 节）。

**为什么用 JWT 而不是 session**（`4.security.md` 第 5 节 / 架构第 8 节）：
无状态、每个请求自带身份，API 多副本水平扩容时不需要共享 session 存储；
代价是**签发后无法即时吊销**，只能靠短 `exp` + 刷新令牌兜住 —— 这是有意的取舍。

本模块的四个安全口径（每条都有对应测试）：

1. **算法必须显式固定**：`decode(..., algorithms=["HS256"])`。
   不写死算法是经典的 JWT 算法混淆漏洞（攻击者把 `alg` 改成 `none` 或换成非对称算法来伪造令牌）。
2. **密钥不落地**：从 `Settings` 读 `JWT_SECRET`（`.env` 注入，`SecretStr` 包着）。
   开发期允许缺省（方便本机起服务），`ENV=prod` 时缺失直接 fail-fast —— 见 `ensure_usable()`。
3. **`exp` / `iat` 必填**：不给「永不过期」留口子；过期用统一的 401 语义（`AuthError`）。
4. **令牌里的角色不可信地用于授权前会先查表**：`parse_token()` 只负责「令牌没被改」，
   角色是否够权限一律回到 `app/security/rbac.py` 的权限表判断。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt

from app.security.policy import CUSTOMER_ROLE

# 令牌类型：区分「拿来做鉴权的访问令牌」与「只用来换令牌的刷新令牌」，
# 防止把刷新令牌直接当访问令牌用（这是很常见的实现漏洞）。
TOKEN_TYPE_ACCESS: Final[str] = "access"
TOKEN_TYPE_REFRESH: Final[str] = "refresh"

DEFAULT_TTL_SECONDS: Final[int] = 30 * 60  # 访问令牌 30 分钟
DEFAULT_REFRESH_TTL_SECONDS: Final[int] = 14 * 24 * 3600  # 刷新令牌 14 天

# 算法白名单：只允许 HS256。名单只有一个元素是**故意的** —— 越多越容易被误配。
ALLOWED_ALGORITHMS: Final[tuple[str, ...]] = ("HS256",)

# 开发期默认密钥：只在「.env 没配 JWT_SECRET」时生效，且 prod 环境会直接拒绝启动。
_DEV_SECRET: Final[str] = "dev-only-insecure-jwt-secret-change-me"


class AuthError(Exception):
    """鉴权失败（令牌缺失/过期/签名不对/类型不对）。对应 HTTP 401。"""


@dataclass(frozen=True)
class Principal:
    """通过鉴权的调用者身份。`user_id` 是行级过滤的唯一依据。"""

    user_id: int
    role: str = CUSTOMER_ROLE
    token_type: str = TOKEN_TYPE_ACCESS


def _secret_source() -> tuple[str, bool]:
    """返回 `(密钥, 是否开发默认值)`。

    读取顺序：`Settings.jwt_secret`（`.env` 注入）→ 开发默认值。
    `Settings` 是 `extra="allow"`，所以新加的 `JWT_SECRET` 会落在 `__pydantic_extra__` 里，
    `getattr` 就能取到 —— 不必为了本模块去改 config 的字段表。
    """
    secret = ""
    try:
        from app.config import get_settings

        value = getattr(get_settings(), "jwt_secret", "")
        secret = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value or "")
    except Exception:  # noqa: BLE001 - 配置不可读时退到开发默认值，不阻断本机起服务
        secret = ""
    if secret:
        return secret, False
    if os.getenv("ENV", "dev") == "prod":
        # 生产环境缺密钥必须**停**：静默用弱密钥比崩掉危险得多。
        raise AuthError("ENV=prod 未配置 JWT_SECRET：拒绝用开发默认密钥启动（请在 .env 注入）")
    return _DEV_SECRET, True


def _secret() -> str:
    return _secret_source()[0]


def is_dev_secret() -> bool:
    """当前是否在用开发默认密钥（给启动日志/健康检查用）。"""
    try:
        return _secret_source()[1]
    except AuthError:
        return False  # prod 缺密钥：不是「在用开发密钥」，而是「根本起不来」，由 _secret() 负责报错


def ensure_usable() -> None:
    """启动自检：走一遍取密钥的路径，prod 缺密钥时在这里就失败。"""
    secret, is_dev = _secret_source()
    if os.getenv("ENV", "dev") == "prod" and is_dev:
        raise AuthError("ENV=prod 使用了开发默认 JWT_SECRET：请注入真实密钥")
    if len(secret) < 16:
        raise AuthError("JWT_SECRET 过短（<16 字符）：HS256 的密钥长度直接决定抗爆破强度")


def hash_password(password: str, *, salt: str | None = None) -> str:
    """口令散列：`sha256(salt + password)`，返回 `salt$digest`。

    ⚠️ **这只是让演示切片不必存明文口令**，不是生产级口令方案：
    `sha256` 快、可被 GPU 爆破，也没有内存硬度。生产要换 Argon2id / bcrypt
    （两者的参数还会随硬件迭代），所以这里把算法名与 salt 一起编码进结果，
    将来换算法时可以按前缀识别旧口令并平滑迁移。
    """
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"sha256${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令。用 `hmac.compare_digest` 做**定时安全**比较（防时序侧信道）。"""
    try:
        algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    candidate = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return hmac.compare_digest(candidate, digest)


def create_access_token(
    user_id: int,
    *,
    role: str = CUSTOMER_ROLE,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """签发访问令牌。`sub` 放 user_id（JWT 规范里 `sub` 是字符串，所以显式 str 化）。"""
    return _create_token(
        user_id,
        role=role,
        token_type=TOKEN_TYPE_ACCESS,
        ttl_seconds=ttl_seconds,
        extra_claims=extra_claims,
    )


def create_refresh_token(
    user_id: int,
    *,
    role: str = CUSTOMER_ROLE,
    ttl_seconds: int = DEFAULT_REFRESH_TTL_SECONDS,
) -> str:
    """签发刷新令牌（**不能**直接用来访问业务接口，见 `parse_token` 的类型校验）。"""
    return _create_token(
        user_id,
        role=role,
        token_type=TOKEN_TYPE_REFRESH,
        ttl_seconds=ttl_seconds,
        extra_claims=None,
    )


def _create_token(
    user_id: int,
    *,
    role: str,
    token_type: str,
    ttl_seconds: int,
    extra_claims: dict[str, Any] | None,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        # jti 给未来的「令牌黑名单/审计」留位（当前无状态实现用不上，但加上不花成本）
        "jti": secrets.token_hex(8),
    }
    if extra_claims:
        reserved = {"sub", "role", "typ", "iat", "exp", "jti"}
        overlap = reserved & set(extra_claims)
        if overlap:
            raise ValueError(f"extra_claims 不能覆盖保留字段：{', '.join(sorted(overlap))}")
        payload.update(extra_claims)
    return jwt.encode(payload, _secret(), algorithm=ALLOWED_ALGORITHMS[0])


def parse_token(token: str, *, expected_type: str = TOKEN_TYPE_ACCESS) -> Principal:
    """校验并解析令牌，返回 `Principal`；任何问题都抛 `AuthError`（HTTP 401 语义）。"""
    if not token:
        raise AuthError("缺少令牌")

    try:
        payload = jwt.decode(
            token,
            _secret(),
            # ⚠️ 必须显式给算法列表：留空会按令牌自带的 `alg` 解，等于让攻击者选算法
            algorithms=list(ALLOWED_ALGORITHMS),
            options={"require": ["exp", "iat", "sub"]},  # 不给「永不过期」留口子
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("令牌已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"令牌无效：{exc}") from exc

    token_type = str(payload.get("typ", ""))
    if token_type != expected_type:
        raise AuthError(f"令牌类型不对：需要 {expected_type}，收到 {token_type or '(缺失)'}")

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthError("令牌缺少有效的 sub（user_id）") from exc

    return Principal(user_id=user_id, role=str(payload.get("role", CUSTOMER_ROLE)), token_type=token_type)


def bearer_token_from_header(authorization: str | None) -> str:
    """从 `Authorization` 头里取出 Bearer 令牌（不依赖 FastAPI，便于单测）。"""
    if not authorization:
        raise AuthError("缺少 Authorization 头")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise AuthError("Authorization 头格式应为 `Bearer <token>`")
    return value.strip()


def get_current_user(authorization: str | None = None) -> Principal:
    """从 `Authorization` 头解析当前用户（**零依赖纯函数**，本模块单测直接用）。

    ⚠️ 直接把它写成 `def route(user=Depends(get_current_user))` 会让 FastAPI 把
    `authorization` 当成**查询参数**。要接进路由请用 `fastapi_dependency()`，
    那里用 `Header(...)` 正确声明了取值位置。
    """
    return parse_token(bearer_token_from_header(authorization))


def fastapi_dependency() -> Any:
    """返回一个可直接 `Depends(...)` 的函数（惰性 import fastapi）。

    用法（第 9 步 api 模块）：
        from fastapi import Depends
        from app.security.auth import fastapi_dependency
        get_current_user = fastapi_dependency()

        @app.get("/orders")
        def list_orders(user = Depends(get_current_user)): ...
    """
    try:
        from fastapi import Header, HTTPException
    except ImportError as exc:  # pragma: no cover - 第 9 步才装 fastapi
        raise ImportError(
            "用了 FastAPI 依赖但没装 fastapi：`uv add fastapi`（第 9 步 api 模块需要）"
        ) from exc

    def dependency(authorization: str | None = Header(default=None)) -> Principal:
        try:
            return get_current_user(authorization)
        except AuthError as exc:
            # 401 + WWW-Authenticate 是标准语义；不把内部异常细节回给客户端
            raise HTTPException(
                status_code=401,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    return dependency


__all__ = [
    "ALLOWED_ALGORITHMS",
    "DEFAULT_REFRESH_TTL_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_REFRESH",
    "AuthError",
    "Principal",
    "bearer_token_from_header",
    "create_access_token",
    "create_refresh_token",
    "ensure_usable",
    "fastapi_dependency",
    "get_current_user",
    "hash_password",
    "is_dev_secret",
    "parse_token",
    "verify_password",
]
