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
    """`sub` / `iat` / `exp` 必填：不给「永不过期」留口子。

    ⚠️ 变更 001 起 `sub` 是**主体标识串**（`c1` / `e7`），不再是裸 user_id。
    裸数字会让员工 `emp_id=7` 与客户 `user_id=7` 派生同一个会话桶（见 `auth` 模块 docstring ①），
    所以这条用例的期望值随契约一起改，并新增 `stype` / `sid` 的断言。
    """
    payload = _decode_unverified(auth.create_access_token(1))
    assert payload["sub"] == "c1"
    assert payload["stype"] == "customer"
    assert payload["sid"] == 1
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


# ── ②b 主体标识与会话桶隔离（变更 001）───────────────────────────────────────


def test_subject_roundtrip():
    for subject_type, subject_id in (("customer", 7), ("employee", 7), ("employee", 1234)):
        assert auth.parse_subject(auth.subject_for(subject_type, subject_id)) == (
            subject_type,
            subject_id,
        )


def test_customer_and_employee_subjects_never_collide():
    """**这是变更 001 的核心安全断言之一。**

    员工 `emp_id=7` 与客户 `user_id=7` 是**两个不同的人**。若主体标识只用裸数字，
    `thread_id` 会派生同一个值 —— 那不是越权读一行数据，而是「读到别人上一轮的原话」。
    """
    assert auth.subject_for("customer", 7) == "c7"
    assert auth.subject_for("employee", 7) == "e7"
    assert auth.subject_for("customer", 7) != auth.subject_for("employee", 7)


def test_employee_token_carries_account_and_subject_but_no_customer_row():
    """员工令牌：`user_id` 必须是 `0`（他在 `users` 表里没有行），主体信息走 `subject_id`。"""
    identity = auth.SigningIdentity(
        subject_type="employee", subject_id=7, role="admin", account_id=3, token_version=5
    )
    token = auth.create_session_token(identity)
    payload = _decode_unverified(token)
    assert payload["sub"] == "e7"
    assert payload["aid"] == 3
    assert payload["ver"] == 5

    principal = auth.parse_token(token)
    assert principal.user_id == 0, "员工不该被安上一个客户 user_id（那会污染行级过滤语义）"
    assert principal.subject_id == 7
    assert principal.subject_type == "employee"
    assert principal.subject == "e7"
    assert principal.account_id == 3
    assert principal.token_version == 5
    assert principal.is_customer is False


def test_customer_token_keeps_real_user_id_for_row_filtering():
    """客户令牌：`user_id` 就是真实客户 id（对客行级过滤 `WHERE user_id = :me` 靠它）。"""
    token = auth.create_access_token(42, role="customer")
    principal = auth.parse_token(token)
    assert principal.user_id == 42
    assert principal.subject_id == 42
    assert principal.is_customer is True


def test_legacy_numeric_sub_is_read_as_customer():
    """旧形态（裸数字 `sub`）按客户解释，且 `token_version` 落成哨兵 `-1`。

    ⚠️ 兼容窗口是**有意的**（见 `auth.classify_subject` 的 docstring）：
    `parse_token` 被限流中间件等**不查库**的路径复用，收紧它会一起打断它们。
    「拒绝旧令牌」由鉴权依赖负责 —— 它查账号表，而旧令牌没有 `ver`。
    """
    legacy = jwt.encode(
        {"sub": "9", "role": "customer", "typ": "access", "iat": 1, "exp": 9999999999},
        auth._secret(),
        algorithm="HS256",
    )
    principal = auth.parse_token(legacy)
    assert principal.subject_type == "customer"
    assert principal.subject_id == 9
    assert principal.user_id == 9
    assert principal.token_version == -1, "旧令牌必须能与非零世代号区分开"


def test_current_tokens_use_sentinel_minus_one_only_for_legacy():
    """新令牌的 `ver` 是真实世代号（`0` 也合法），不能被误当成「旧令牌」。"""
    identity = auth.SigningIdentity(subject_type="customer", subject_id=1, account_id=1, token_version=0)
    assert auth.parse_token(auth.create_session_token(identity)).token_version == 0


@pytest.mark.parametrize(
    "forged",
    [
        # sub 推出 customer，stype 却写 employee
        {"sub": "c7", "stype": "employee", "sid": 7},
        # sub 推出 id=7，sid 却写 8
        {"sub": "c7", "stype": "customer", "sid": 8},
    ],
)
def test_contradictory_subject_claims_are_rejected(forged):
    """`sub` 与它的分解声明不一致 → **拒绝**，不挑一个信。

    为什么不能「以某个为准」：两个来源不一致意味着令牌被手工拼过（或签发侧有 bug），
    而「该信哪个」正是攻击者想要的那个问题。
    """
    token = jwt.encode(
        {**forged, "typ": "access", "iat": 1, "exp": 9999999999},
        auth._secret(),
        algorithm="HS256",
    )
    with pytest.raises(auth.AuthError):
        auth.parse_token(token)


@pytest.mark.parametrize("subject_type", ["staff", "admin", "", "Customer", "C"])
def test_unknown_subject_types_are_rejected(subject_type):
    """未知主体类别默认拒绝：多一个类别就多一处要审的授权分支。"""
    with pytest.raises(auth.AuthError):
        auth.subject_for(subject_type, 1)


@pytest.mark.parametrize("claim", ["sub", "role", "exp", "stype", "sid", "aid", "ver"])
def test_extra_claims_cannot_override_any_reserved_claim(claim):
    """保留声明的名单必须覆盖变更 001 新增的四个 —— 漏掉任意一个就是一条提权路径。"""
    identity = auth.SigningIdentity(subject_type="employee", subject_id=1, role="admin")
    with pytest.raises(ValueError):
        auth.create_session_token(identity, extra_claims={claim: "forged"})


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


# ── ④b Argon2id 与旧值迁移（变更 001）────────────────────────────────────────


def test_new_passwords_use_argon2id():
    """新口令必须是 Argon2id，且**参数写死在代码里**（不随库版本漂移）。"""
    stored = auth.hash_password("s3cret-pw")
    assert stored.startswith(f"{auth.PASSWORD_SCHEME}$argon2id$")
    assert f"m={auth.ARGON2_MEMORY_COST},t={auth.ARGON2_TIME_COST},p={auth.ARGON2_PARALLELISM}" in stored


def test_stored_format_has_exactly_one_scheme_separator():
    """存储值里 **只能有一个方案分隔符**：`argon2$argon2id$...`。

    ⚠️ 第一版拼成 `argon2$$argon2id$...`（双 `$`）。它能被本模块认出来（按第一个 `$` 剥离），
    但自描述格式的全部价值是**外部工具也读得懂** —— 双分隔符会让任何外部校验判为坏值。
    """
    stored = auth.hash_password("s3cret-pw")
    assert not stored.startswith("argon2$$")
    assert stored.split("$")[1].startswith("argon2id")


def test_legacy_sha256_hash_still_verifies_and_is_flagged_for_upgrade():
    """存量旧口令：**仍可登录**，但被标记为「该升级」。

    这条是「不要求全体重置口令」这句承诺的落点 —— 旧的 `sha256$...</hash>` 值
    在用户下次登录时由调用方（登录流程）原地重写成 Argon2id。
    """
    legacy = auth.hash_password("s3cret-pw", scheme=auth.LEGACY_PASSWORD_SCHEME)
    assert legacy.startswith(f"{auth.LEGACY_PASSWORD_SCHEME}$")
    assert auth.verify_password("s3cret-pw", legacy) is True
    assert auth.verify_password("wrong", legacy) is False
    assert auth.needs_rehash(legacy) is True


def test_fresh_argon2_hash_does_not_need_rehash():
    assert auth.needs_rehash(auth.hash_password("s3cret-pw")) is False


@pytest.mark.parametrize("stored", ["", "garbage", "md5$aa$bb", "argon2$not-a-hash"])
def test_needs_rehash_treats_broken_values_as_rewritable(stored):
    """认不出来的存储值一律返回「该重写」：趁「刚验证过明文口令」这个唯一时机修好它。"""
    assert auth.needs_rehash(stored) is True


def test_legacy_scheme_rejects_unknown_scheme_name():
    """`hash_password` 不接受未登记的方案名（否则会静默产出没人能验的值）。"""
    with pytest.raises(ValueError):
        auth.hash_password("x", scheme="md5")
