"""鉴权用例（JWT 签发/校验、算法固定、口令散列）。

**不需要数据库**：JWT 是无状态的，正因为如此才能水平扩容（架构第 8 节）。

重点覆盖三类真实攻击面：
1. **算法混淆**：篡改 `alg`（改 `none`、换成别的算法）伪造令牌；
2. **篡改载荷**：改 `sub`/`role` 提升权限（签名必须对不上）；
3. **类型混用**：刷新令牌当访问令牌用、过期令牌继续用。
"""

from __future__ import annotations

import base64
import json

import jwt
import pytest

from app.security import auth
from app.security.policy import CUSTOMER_ROLE


def _decode_unverified(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


# ── ① 正常路径 ───────────────────────────────────────────────────────────────


def test_create_and_parse_access_token():
    token = auth.create_access_token(42, role="manager")
    principal = auth.parse_token(token)
    assert principal.user_id == 42
    assert principal.role == "manager"
    assert principal.token_type == auth.TOKEN_TYPE_ACCESS


def test_token_contains_exp_iat_sub():
    """`sub` / `iat` / `exp` 必填：不给「永不过期」留口子。"""
    payload = _decode_unverified(auth.create_access_token(1))
    assert payload["sub"] == "1"
    assert "iat" in payload and "exp" in payload
    assert payload["exp"] > payload["iat"]


def test_get_current_user_reads_bearer_header():
    token = auth.create_access_token(7)
    principal = auth.get_current_user(f"Bearer {token}")
    assert principal.user_id == 7


@pytest.mark.parametrize(
    "header",
    [None, "", "token-without-scheme", "Basic abc", "Bearer ", "Bearer    "],
)
def test_bad_authorization_headers_are_rejected(header):
    with pytest.raises(auth.AuthError):
        auth.get_current_user(header)


# ── ② 攻击面：算法混淆 / 篡改 ────────────────────────────────────────────────


def test_alg_none_token_is_rejected():
    """经典 `alg: none` 伪造：必须按固定算法校验，不能听令牌自己的。"""
    payload = _decode_unverified(auth.create_access_token(1))
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    forged = f"{header.decode()}.{body.decode()}."

    with pytest.raises(auth.AuthError):
        auth.parse_token(forged)


def test_payload_tampering_is_rejected():
    """改 `role` 提权：签名对不上 → 拒绝。"""
    token = auth.create_access_token(1, role=CUSTOMER_ROLE)
    header, payload, signature = token.split(".")
    data = _decode_unverified(token)
    data["role"] = "admin"
    tampered = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    forged = f"{header}.{tampered}.{signature}"

    with pytest.raises(auth.AuthError):
        auth.parse_token(forged)


def test_signature_from_other_secret_is_rejected():
    """换密钥签发的令牌一律无效（测试里显式换一个密钥签）。"""
    other = jwt.encode(
        {"sub": "1", "role": "admin", "typ": "access"},
        "another-secret-long-enough-for-hs256-32b",
        algorithm="HS256",
    )
    with pytest.raises(auth.AuthError):
        auth.parse_token(other)


def test_short_hmac_key_is_rejected_by_ensure_usable():
    """`ensure_usable()` 要拦住过短的 HS256 密钥。

    PyJWT 对 <32 字节的 HMAC 密钥会发 `InsecureKeyLengthWarning`（RFC 7518 §3.2），
    本模块把它升级成启动期硬失败 —— 短密钥让 HS256 的抗爆破强度直接打折。
    """
    import os

    import pytest as _pytest

    from app.config import get_settings

    os.environ["JWT_SECRET"] = "short-secret"
    get_settings.cache_clear()
    try:
        with _pytest.raises(auth.AuthError):
            auth.ensure_usable()
    finally:
        os.environ.pop("JWT_SECRET", None)
        get_settings.cache_clear()


def test_expired_token_is_rejected():
    expired = auth.create_access_token(1, ttl_seconds=-1)
    with pytest.raises(auth.AuthError) as exc:
        auth.parse_token(expired)
    assert "过期" in str(exc.value)


def test_refresh_token_cannot_be_used_as_access_token():
    """类型校验：刷新令牌不能直接访问业务接口（很常见的一个实现漏洞）。"""
    refresh = auth.create_refresh_token(1)
    assert auth.parse_token(refresh, expected_type=auth.TOKEN_TYPE_REFRESH).token_type == "refresh"
    with pytest.raises(auth.AuthError) as exc:
        auth.parse_token(refresh)
    assert "类型" in str(exc.value)


def test_extra_claims_cannot_override_reserved_fields():
    """扩展字段不能覆盖 `sub`/`role`/`exp` 等保留字段（否则又是一条提权路径）。"""
    with pytest.raises(ValueError):
        auth.create_access_token(1, extra_claims={"role": "admin"})


def test_extra_claims_are_carried():
    token = auth.create_access_token(1, extra_claims={"tenant": "shop-a"})
    assert _decode_unverified(token)["tenant"] == "shop-a"


def test_algorithm_allowlist_is_explicit():
    """算法白名单必须显式且只有 HS256（越多越容易被误配）。"""
    assert auth.ALLOWED_ALGORITHMS == ("HS256",)


# ── ③ 密钥来源与生产 fail-fast ──────────────────────────────────────────────


def test_dev_secret_used_when_not_configured(monkeypatch):
    """开发环境没配 `JWT_SECRET` 时回落到开发默认密钥（但要能被 `is_dev_secret()` 识别出来）。"""
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("JWT_SECRET", "")
    from app.config import get_settings

    get_settings.cache_clear()
    assert auth.is_dev_secret() is True


def test_prod_without_secret_fails_fast(monkeypatch):
    """`ENV=prod` + 没配密钥 → 拒绝启动，而不是静默用弱默认密钥。"""
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("JWT_SECRET", "")
    from app.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(auth.AuthError):
        auth.create_access_token(1)


def test_configured_secret_is_used(monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("JWT_SECRET", "a-properly-long-secret-for-tests-1234")
    from app.config import get_settings

    get_settings.cache_clear()
    assert auth.is_dev_secret() is False
    token = auth.create_access_token(5)
    assert auth.parse_token(token).user_id == 5


def test_settings_never_leaks_jwt_secret():
    """配置的显示出口（repr / dump_for_logging）不能出现密钥明文。"""
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    assert settings.dump_for_logging().get("jwt_secret") in {"", "**********"}
    assert "JWT_SECRET" in repr(settings) or "jwt_secret" in repr(settings)


# ── ④ 口令散列（本步只做到「不存明文」）──────────────────────────────────────


def test_password_hash_roundtrip():
    stored = auth.hash_password("s3cret-pw")
    assert auth.verify_password("s3cret-pw", stored)
    assert not auth.verify_password("s3cret-pW", stored)


def test_password_hash_is_salted():
    """同一个口令两次散列结果必须不同（否则等于可反查的彩虹表输入）。"""
    assert auth.hash_password("same") != auth.hash_password("same")


@pytest.mark.parametrize("stored", ["", "garbage", "md5$aa$bb", "sha256$only-two-parts"])
def test_verify_password_survives_malformed_stored_value(stored):
    assert auth.verify_password("whatever", stored) is False


def test_hash_password_does_not_store_plaintext():
    stored = auth.hash_password("plaintext-password")
    assert "plaintext-password" not in stored
