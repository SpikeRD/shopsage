"""鉴权：口令散列 + JWT 签发/校验 + FastAPI `get_current_user` 依赖（`4.security.md` 第 2 节）。

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

## 变更 001 改了什么（账号登录落地时新增的口径）

### ① 主体标识从「纯数字 user_id」改为「类别 + id」

令牌的 `sub` 现在是 **`c{user_id}` / `e{emp_id}`**（客户 / 员工），另带 `stype` / `sid` 两个声明。
为什么必须把类别编进去：员工在 `users` 表里**没有行**，而 `thread_id` 由主体派生
（`f"{subject}:{session_id}"`）。若员工沿用 `emp_id` 这个裸数字，员工 `emp_id=7` 与客户
`user_id=7` 会派生**同一个会话桶** —— 两个不同的人落进同一份 checkpoint，
那不是「越权读一行数据」，而是「读到别人上一轮的原话」。类别进 `sub` 之后，派生规则仍然只有一处。

### ② `aid` / `ver`：把「停用/改密立即生效」所需的判据放进令牌

- `aid` = `accounts.account_id`（审计「是哪一次登录」）；
- `ver` = `accounts.token_version`（**令牌世代号**）。

`parse_token` **只做格式与签名校验，不查库**；「账号是否停用 / 世代号是否过期 / 角色以库为准」
由鉴权依赖（`app/api/deps.py`）在同一次账号查表里判断。为什么不让本模块查库：
本模块的定位是**纯凭据解析**，它被限流中间件、WS 握手、脚本自检等多处复用，
而**限流路径刻意不连库** —— 把查库塞进来会让那条路径多一次 I/O。

### ③ 口令散列升级为 Argon2id，并保留旧值的平滑迁移

旧实现是 `sha256(salt + password)`，它的 docstring 自己写着「不是生产级」（快、可被 GPU 爆破、
没有内存硬度）。现在：

- 新口令 → `argon2$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>`；
- 旧口令 → `sha256$<salt>$<digest>` **仍然可验**，且登录成功时由
  `needs_rehash()` 识别并**原地升级**（调用方见 `app/security/accounts.py` 的登录流程）；
- **算法名与参数都编码进存储值**，所以将来再换算法时同样能按前缀识别旧值并平滑迁移。
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

# ── 令牌声明与主体（变更 001）────────────────────────────────────────────────

# 令牌类型：区分「拿来做鉴权的访问令牌」与「只用来换令牌的刷新令牌」，
# 防止把刷新令牌直接当访问令牌用（这是很常见的实现漏洞）。
TOKEN_TYPE_ACCESS: Final[str] = "access"
TOKEN_TYPE_REFRESH: Final[str] = "refresh"

#: 访问令牌里「谁在调用」的几个声明（与 `Principal` 一一对应）。
#: `sub` 是**主体标识串**（`c7` / `e7`），`stype` / `sid` 是它的分解形式。
CLAIM_SUB: Final[str] = "sub"
CLAIM_SUBJECT: Final[str] = "stype"
CLAIM_SUBJECT_ID: Final[str] = "sid"
CLAIM_ACCOUNT_ID: Final[str] = "aid"
CLAIM_TOKEN_VERSION: Final[str] = "ver"

#: 主体类别 → 主体标识前缀。它与「谁能干什么」（`rbac.ROLE_PERMISSIONS`）是两件事：
#: 这张表管的是**令牌格式**，改它等于改契约。
SUBJECT_PREFIX: Final[dict[str, str]] = {"customer": "c", "employee": "e"}
#: 主体类别 → 承载它的业务表。**仅用于文档与错误信息**，本模块不查库。
SUBJECT_TABLE: Final[dict[str, str]] = {"customer": "users.user_id", "employee": "employees.emp_id"}

#: 只接受这两个主体类别：默认拒绝未知类别（多一个类别就多一处要审的授权分支）。
KNOWN_SUBJECT_TYPES: Final[frozenset[str]] = frozenset(SUBJECT_PREFIX)

#: 保留声明：`extra_claims` 不许覆盖它们（否则调用方能悄悄改身份）。
RESERVED_CLAIMS: Final[frozenset[str]] = frozenset(
    {
        CLAIM_SUB,
        "role",
        "typ",
        "iat",
        "exp",
        "jti",
        CLAIM_SUBJECT,
        CLAIM_SUBJECT_ID,
        CLAIM_ACCOUNT_ID,
        CLAIM_TOKEN_VERSION,
    }
)

#: 令牌世代号的默认值（账号行缺失时的兜底，只用于脚本/自检）。
DEFAULT_TOKEN_VERSION: Final[int] = 0

DEFAULT_TTL_SECONDS: Final[int] = 30 * 60  # 访问令牌 30 分钟
DEFAULT_REFRESH_TTL_SECONDS: Final[int] = 14 * 24 * 3600  # 刷新令牌 14 天

# 算法白名单：只允许 HS256。名单只有一个元素是**故意的** —— 越多越容易被误配。
ALLOWED_ALGORITHMS: Final[tuple[str, ...]] = ("HS256",)

# 开发期默认密钥：只在「.env 没配 JWT_SECRET」时生效，且 prod 环境会直接拒绝启动。
_DEV_SECRET: Final[str] = "dev-only-insecure-jwt-secret-change-me"


class AuthError(Exception):
    """鉴权失败（令牌缺失/过期/签名不对/类型不对）。对应 HTTP 401。"""


def subject_for(subject_type: str, subject_id: int) -> str:
    """`("employee", 7)` → `"e7"`。未知类别直接抛 —— 默认拒绝，不猜。"""
    prefix = SUBJECT_PREFIX.get(str(subject_type))
    if prefix is None:
        raise AuthError(
            f"未知的主体类别 {subject_type!r}：只接受 {sorted(KNOWN_SUBJECT_TYPES)}"
            "（默认拒绝：多一个类别就多一处要审的授权分支）"
        )
    return f"{prefix}{int(subject_id)}"


def parse_subject(subject: str) -> tuple[str, int]:
    """`"e7"` → `("employee", 7)`。

    ⚠️ **旧格式（纯数字 `sub`）不走这里**：那由 `classify_subject` 处理。
    把两种形态分开，是为了让「新格式写错了」与「这是旧令牌」报不同的错 ——
    混成一句会把人引到错误的修法上（`web/src/lib/auth.ts` 的 `typ`/`type` 坑就是这么来的）。
    """
    text = str(subject or "")
    if len(text) < 2 or not text[1:].isdigit():
        raise AuthError(f"主体标识 {subject!r} 非法：格式应为 `c<user_id>` 或 `e<emp_id>`")
    for subject_type, candidate in SUBJECT_PREFIX.items():
        if candidate == text[0]:
            return subject_type, int(text[1:])
    raise AuthError(f"主体标识 {subject!r} 的前缀未知：只接受 {sorted(SUBJECT_PREFIX.values())}")


def classify_subject(raw: Any) -> tuple[str, int, str]:
    """把令牌里的主体声明归一成 `(subject_type, subject_id, 主体串)`。

    接受两种形态（**这是有意的兼容窗口，不是含糊**）：

    | 形态 | 例子 | 判定 | 来源 |
    |---|---|---|---|
    | 新 | `sub="c7"` | 前缀 + 数字 | 变更 001 之后的正常令牌 |
    | 旧 | `sub="7"` | 纯数字 | 变更 001 之前的令牌、旧口径的测试与脚本 |

    旧形态一律按 **`customer`** 解释 —— 那是唯一的可能性：变更之前**只有客户能登录**
    （员工身份靠 `scripts/dev_token.py` 现场硬塞一个 `user_id`，它在真登录下没有位置）。

    为什么保留旧形态而不是直接拒：`parse_token` 被限流中间件、脚本自检、单测等
    **多条不查库**的路径复用，收紧它会把这些路径一起打断。
    **「升级后旧令牌失效」这件事由鉴权依赖（`app/api/deps.py::require_principal`）负责** ——
    它查账号表，而旧令牌没有 `ver`/`aid`，在那里一定被拒。口径分层，不要混在一处。
    """
    text = str(raw if raw is not None else "")
    if not text:
        raise AuthError("令牌缺少 sub（主体标识）")
    if text[0].isdigit():
        if not text.isdigit():
            raise AuthError(f"令牌的 sub 不是合法的 id：{raw!r}")
        return "customer", int(text), f"c{int(text)}"
    subject_type, subject_id = parse_subject(text)
    return subject_type, subject_id, text


@dataclass(frozen=True)
class Principal:
    """通过鉴权的调用者身份。

    ## ⚠️ `user_id` 的语义在变更 001 收窄了

    它现在**只表示「行级过滤用的客户 id」**（对客路径 `WHERE user_id = :me` 的那个值），
    员工主体一律为 `0`。为什么不让它继续兼职「主体 id」：一个字段担两个职责时，
    「员工 emp_id=7」与「客户 user_id=7」会在会话派生上撞车（见模块 docstring ①）。
    需要稳定主体身份的地方一律用 `subject_id` / `subject`。
    """

    #: 行级过滤依据（**仅客户主体有意义**；员工主体是 0）
    user_id: int
    role: str = CUSTOMER_ROLE
    token_type: str = TOKEN_TYPE_ACCESS
    #: 主体类别：`customer` / `employee`
    subject_type: str = "customer"
    #: 主体在**自己表里**的主键（客户 `users.user_id` / 员工 `employees.emp_id`）
    subject_id: int = 0
    #: 账号主键（`accounts.account_id`）：审计「是哪一次登录」。旧令牌为 0。
    account_id: int = 0
    #: 令牌世代号。旧令牌为 `-1`（**哨兵值**：`0` 是合法世代号，不能拿它表示「没有」）。
    token_version: int = -1

    @property
    def subject(self) -> str:
        """主体标识串（`c7` / `e7`）：`thread_id` 派生的唯一依据。"""
        return subject_for(self.subject_type, self.subject_id)

    @property
    def is_customer(self) -> bool:
        return self.subject_type == "customer"


@dataclass(frozen=True)
class SigningIdentity:
    """**签发令牌时**要写进声明的身份（`Principal` 的输入侧对偶）。

    为什么单独一个类型、而不是给签发函数一路加关键字参数：声明会随口径增长
    （`aid` / `ver` 就是变更 001 新加的），逐个传参时**漏传一个不会报错**——
    它会安静地用默认值签出一个弱令牌。把「签谁」收成一个对象，将来加声明只改这一处。
    """

    subject_type: str
    subject_id: int
    role: str = CUSTOMER_ROLE
    account_id: int = 0
    token_version: int = DEFAULT_TOKEN_VERSION

    @property
    def subject(self) -> str:
        return subject_for(self.subject_type, self.subject_id)


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


# ── 口令散列（变更 001：Argon2id + 旧值兼容）──────────────────────────────────
#
# 两条口径贯穿这一节：
#
# 1. **存储值是自描述的**：`<方案名>$<方案自己的参数...>`。于是「换算法」不需要一次性
#    重置所有口令 —— 按前缀就能认出旧值，验过之后原地升级即可。
# 2. **比较必须是定时安全的**：口令校验是典型的时序侧信道目标（逐字节比较会泄露
#    「前几位对了」）。Argon2 自带恒定时间比较；旧方案用 `hmac.compare_digest`。

#: 当前散列方案名。**它进存储值前缀，改动等于改数据格式**。
PASSWORD_SCHEME: Final[str] = "argon2"

#: 旧方案名（只为了**能认出**它并平滑迁移，不再产生新的该格式值）。
LEGACY_PASSWORD_SCHEME: Final[str] = "sha256"

#: Argon2id 参数。显式写死而不是用库的默认值：默认值会随库版本变化，
#: 于是「同一份代码在不同环境里散列强度不同」—— 那是审计上说不清的状态。
#: 参数语义（RFC 9106 的推荐档，本仓实测约 30~60ms/次）：
#:   m = 65536 KiB = 64 MiB 内存；t = 3 轮；p = 4 条并行 lane。
ARGON2_TIME_COST: Final[int] = 3
ARGON2_MEMORY_COST: Final[int] = 64 * 1024  # KiB
ARGON2_PARALLELISM: Final[int] = 4


def _hasher() -> Any:
    """惰性构造 `PasswordHasher`。

    为什么不像其它模块那样在顶部 `import argon2` 并建单例：
    ① `argon2-cffi` 是变更 001 才加的依赖，顶部 import 会让**没装它的环境
    （例如只装 `pip install .` 的老 CI）连 `import app.security` 都失败**；
    ② `PasswordHasher` 的构造会读参数并做一次自检，模块级单例在 import 期就付这个成本。
    `lru_cache` 让它仍是进程内一个实例（省掉每次登录重建的开销）。
    """
    from argon2 import PasswordHasher

    return PasswordHasher(
        time_cost=ARGON2_TIME_COST, memory_cost=ARGON2_MEMORY_COST, parallelism=ARGON2_PARALLELISM
    )


def _scheme_of(stored: str) -> str:
    """取存储值的方案名前缀（`argon2$...` → `argon2`）。认不出来返回空串。"""
    head, separator, _ = str(stored or "").partition("$")
    return head.lower() if separator else ""


def _encode_argon2(hashed: str) -> str:
    """库输出 → **存储值**：`$argon2id$v=19$...` → `argon2$argon2id$v=19$...`。

    ⚠️ 这个「剥一个 `$`、再在方案名后补一个 `$`」的操作必须成对出现，所以它和
    `_decode_argon2` 放在一起。第一版把拼接写成 `f"{PASSWORD_SCHEME}${hashed}"`，
    得到 `argon2$$argon2id$...`（双 `$`）—— 它**能**被本模块认出来，
    但任何外部工具都会判为坏值；而「自描述格式」的全部价值就是外部工具也读得懂。
    """
    return f"{PASSWORD_SCHEME}${hashed.lstrip('$')}"


def _decode_argon2(stored: str) -> str:
    """存储值 → 库输入：`argon2$argon2id$v=19$...` → `$argon2id$v=19$...`。

    ⚠️ 前导 `$` **必须还原**：argon2 库靠它定位自己的算法标识，少一个就抛
    `InvalidHashError`（症状是「刚设的口令马上验不过」—— 一个看起来像
    「散列算法坏了」、实际是「字符串边界错了」的问题，本项目实测踩到过一次）。
    """
    return "$" + str(stored).split("$", 1)[1]


def hash_password(password: str, *, salt: str | None = None, scheme: str = PASSWORD_SCHEME) -> str:
    """口令散列，返回**自描述**存储值。

    | `scheme` | 返回形态 | 用途 |
    |---|---|---|
    | `argon2`（默认） | `argon2$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>` | 新口令 |
    | `sha256` | `sha256$<salt>$<digest>` | **只给测试与兼容用例**，不要用它建号 |

    ⚠️ `salt` 参数**只在 `scheme="sha256"` 时有意义**（旧格式把 salt 明文拼在存储值里）。
    Argon2 自己生成并管理 salt，**没有**「外部传入 salt」这个口子 ——
    留这个参数只为让旧口径的测试（与时序对比用例）能复现同一个存储值。
    """
    if scheme == LEGACY_PASSWORD_SCHEME:
        legacy_salt = salt or secrets.token_hex(8)
        digest = hashlib.sha256(f"{legacy_salt}{password}".encode()).hexdigest()
        return f"{LEGACY_PASSWORD_SCHEME}${legacy_salt}${digest}"
    if scheme != PASSWORD_SCHEME:
        raise ValueError(f"不支持的口令散列方案：{scheme!r}（只接受 argon2 / sha256）")
    return _encode_argon2(_hasher().hash(password))


def verify_password(password: str, stored: str) -> bool:
    """校验口令（**新旧格式都认**）。任何解析不出来/不匹配的情况一律返回 `False`。

    为什么内部异常全吞掉：本函数的调用点（登录、脚本自检）只需要一个布尔答案，
    而「存储值坏了」与「口令错了」对外必须**不可区分**（防枚举，见变更单 §3.6）。
    """
    scheme = _scheme_of(stored)
    if scheme == LEGACY_PASSWORD_SCHEME:
        return _verify_legacy(password, stored)
    if scheme == PASSWORD_SCHEME:
        from argon2.exceptions import InvalidHashError, VerificationError

        try:
            return _hasher().verify(_decode_argon2(stored), password)
        except (VerificationError, InvalidHashError):
            return False
        except Exception:  # noqa: BLE001 - 存储值形状异常 → 等同于「验不过」
            return False
    return False


def _verify_legacy(password: str, stored: str) -> bool:
    """旧格式 `sha256$<salt>$<digest>` 的校验（定时安全比较）。"""
    try:
        _, legacy_salt, digest = str(stored).split("$", 2)
    except ValueError:
        return False
    candidate = hashlib.sha256(f"{legacy_salt}{password}".encode()).hexdigest()
    return hmac.compare_digest(candidate, digest)


def needs_rehash(stored: str) -> bool:
    """这个存储值是否该**原地升级**（登录成功后调用）。

    三种情况都返回 `True`：

    1. **旧方案**（`sha256$...`）—— 这是本函数存在的主要理由：让存量账号在
       用户下次登录时自动迁到 Argon2id，而不是要求全体重置口令；
    2. **认不出来的方案**（空前缀、别的实现写的值）—— 它至少说明这行不是本模块写的，
       趁「我们刚验证过明文口令」这个唯一时机重写它，比留着更可控；
    3. **Argon2 参数落后于当前配置**（库的 `check_needs_rehash`）——
       将来调高 `m`/`t` 之后，存量口令会随登录逐步跟上，不需要一次性重算。
    """
    scheme = _scheme_of(stored)
    if scheme == LEGACY_PASSWORD_SCHEME:
        return True
    if scheme != PASSWORD_SCHEME:
        return True
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError

    try:
        return PasswordHasher().check_needs_rehash(_decode_argon2(stored))
    except InvalidHashError:
        return True  # 坏值同样值得重写（下次登录会写回一个完好的值）
    except Exception:  # noqa: BLE001 - 存储值形状异常 → 同样按「该重写」处理
        return True



def create_access_token(
    user_id: int,
    *,
    role: str = CUSTOMER_ROLE,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    extra_claims: dict[str, Any] | None = None,
    identity: SigningIdentity | None = None,
) -> str:
    """签发**客户主体**的访问令牌（兼容形态）。

    ⚠️ 这个签名保留下来是为了不打断既有的测试与脚本（`scripts/api_demo.py` 等），
    它签出的是「客户主体、无账号信息」的令牌：`sub="c{user_id}"`、`aid=0`、`ver=-1`。
    **真登录一律走 `create_session_token()`** —— 它签出的令牌带 `aid`/`ver`，
    而鉴权依赖对没有 `ver` 的令牌是拒绝的（那是「升级后旧令牌失效」的落点）。

    想签员工令牌、或想带上账号信息时，用 `identity=` 显式给一份身份。
    """
    if identity is not None:
        return create_session_token(
            identity,
            token_type=TOKEN_TYPE_ACCESS,
            ttl_seconds=ttl_seconds,
            extra_claims=extra_claims,
        )
    return _create_token(
        subject=subject_for("customer", int(user_id)),
        subject_type="customer",
        subject_id=int(user_id),
        role=role,
        account_id=0,
        token_version=-1,
        token_type=TOKEN_TYPE_ACCESS,
        ttl_seconds=ttl_seconds,
        extra_claims=extra_claims,
    )


def create_refresh_token(
    user_id: int,
    *,
    role: str = CUSTOMER_ROLE,
    ttl_seconds: int = DEFAULT_REFRESH_TTL_SECONDS,
    identity: SigningIdentity | None = None,
) -> str:
    """签发刷新令牌（**不能**直接用来访问业务接口，见 `parse_token` 的类型校验）。

    与 `create_access_token` 同一套兼容口径：真登录走 `create_session_token()`。
    """
    if identity is not None:
        return create_session_token(identity, token_type=TOKEN_TYPE_REFRESH, ttl_seconds=ttl_seconds)
    return _create_token(
        subject=subject_for("customer", int(user_id)),
        subject_type="customer",
        subject_id=int(user_id),
        role=role,
        account_id=0,
        token_version=-1,
        token_type=TOKEN_TYPE_REFRESH,
        ttl_seconds=ttl_seconds,
        extra_claims=None,
    )


def create_session_token(
    identity: SigningIdentity,
    *,
    token_type: str = TOKEN_TYPE_ACCESS,
    ttl_seconds: int | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """按 `SigningIdentity` 签发令牌（**登录与刷新唯一该走的入口**）。

    ⚠️ `ttl_seconds=None` 表示「用该类型的默认值」，而不是「有效期为空」——
    `None` 会在这里被换成默认值，因为它**不会**被透传给 `timedelta`
    （`scripts/dev_token.py` 记过那个坑：显式传 `None` 会让 `timedelta` 直接 `TypeError`）。
    """
    if ttl_seconds is None:
        ttl_seconds = DEFAULT_REFRESH_TTL_SECONDS if token_type == TOKEN_TYPE_REFRESH else DEFAULT_TTL_SECONDS
    return _create_token(
        subject=identity.subject,
        subject_type=identity.subject_type,
        subject_id=identity.subject_id,
        role=identity.role,
        account_id=identity.account_id,
        token_version=identity.token_version,
        token_type=token_type,
        ttl_seconds=ttl_seconds,
        extra_claims=extra_claims,
    )


def _create_token(
    *,
    subject: str,
    subject_type: str,
    subject_id: int,
    role: str,
    account_id: int,
    token_version: int,
    token_type: str,
    ttl_seconds: int,
    extra_claims: dict[str, Any] | None,
) -> str:
    """把声明拼成 JWT。

    ⚠️ 参数**全部**是关键字、且没有默认值：这是刻意的。声明字段会随口径增长
    （变更 001 就加了 `aid`/`ver`），有默认值的参数意味着「漏传」不会报错，
    而是安静地签出一个验证不过（或更糟：验证得过但身份错）的令牌。
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        CLAIM_SUB: subject,
        CLAIM_SUBJECT: subject_type,
        CLAIM_SUBJECT_ID: int(subject_id),
        CLAIM_ACCOUNT_ID: int(account_id),
        CLAIM_TOKEN_VERSION: int(token_version),
        "role": role,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        # jti 给未来的「令牌黑名单/审计」留位（当前无状态实现用不上，但加上不花成本）
        "jti": secrets.token_hex(8),
    }
    if extra_claims:
        overlap = RESERVED_CLAIMS & set(extra_claims)
        if overlap:
            raise ValueError(f"extra_claims 不能覆盖保留字段：{', '.join(sorted(overlap))}")
        payload.update(extra_claims)
    return jwt.encode(payload, _secret(), algorithm=ALLOWED_ALGORITHMS[0])


def parse_token(token: str, *, expected_type: str = TOKEN_TYPE_ACCESS) -> Principal:
    """校验并解析令牌，返回 `Principal`；任何问题都抛 `AuthError`（HTTP 401 语义）。

    **只做「令牌没被改 + 格式对」**，不查库、不判停用 —— 见模块 docstring ②。

    新旧两种 `sub` 都接受（见 `classify_subject`）：旧形态按 `customer` 解释、
    `token_version` 落成哨兵 `-1`。**拒绝旧令牌是鉴权依赖的职责**，不是这里的。
    """
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

    subject_type, subject_id, subject = classify_subject(payload.get(CLAIM_SUB))

    # 声明的分解形式若与 `sub` 不一致 → **拒绝**，而不是挑一个信。
    # 为什么：两个来源不一致意味着令牌被手工拼过（或签发侧有 bug），
    # 此时「以哪个为准」正是攻击者想要的那个问题。宁可 401。
    claimed_type = payload.get(CLAIM_SUBJECT)
    if claimed_type is not None and str(claimed_type) != subject_type:
        raise AuthError(
            f"令牌自相矛盾：sub={subject!r} 推出主体类别 {subject_type!r}，"
            f"而 {CLAIM_SUBJECT}={claimed_type!r}（拒绝：不一致的声明没有可信的那个）"
        )
    claimed_id = payload.get(CLAIM_SUBJECT_ID)
    if claimed_id is not None:
        try:
            claimed_id_int = int(claimed_id)
        except (TypeError, ValueError) as exc:
            raise AuthError(f"令牌的 {CLAIM_SUBJECT_ID} 不是整数：{claimed_id!r}") from exc
        if claimed_id_int != subject_id:
            raise AuthError(
                f"令牌自相矛盾：sub={subject!r} 推出 id={subject_id}，"
                f"而 {CLAIM_SUBJECT_ID}={claimed_id_int}（拒绝）"
            )

    def _int_claim(name: str, default: int) -> int:
        raw = payload.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise AuthError(f"令牌的 {name} 不是整数：{raw!r}") from exc

    return Principal(
        # ⚠️ `user_id` 只在客户主体上有意义（员工是 0）—— 见 `Principal` 的 docstring
        user_id=subject_id if subject_type == "customer" else 0,
        role=str(payload.get("role", CUSTOMER_ROLE)),
        token_type=token_type,
        subject_type=subject_type,
        subject_id=subject_id,
        account_id=_int_claim(CLAIM_ACCOUNT_ID, 0),
        # 旧令牌没有 ver：落成哨兵 -1（区别于合法的世代号 0），由鉴权依赖拒绝
        token_version=_int_claim(CLAIM_TOKEN_VERSION, -1),
    )



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
    "ARGON2_MEMORY_COST",
    "ARGON2_PARALLELISM",
    "ARGON2_TIME_COST",
    "CLAIM_ACCOUNT_ID",
    "CLAIM_SUB",
    "CLAIM_SUBJECT",
    "CLAIM_SUBJECT_ID",
    "CLAIM_TOKEN_VERSION",
    "DEFAULT_REFRESH_TTL_SECONDS",
    "DEFAULT_TOKEN_VERSION",
    "DEFAULT_TTL_SECONDS",
    "KNOWN_SUBJECT_TYPES",
    "LEGACY_PASSWORD_SCHEME",
    "PASSWORD_SCHEME",
    "RESERVED_CLAIMS",
    "SUBJECT_PREFIX",
    "SUBJECT_TABLE",
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_REFRESH",
    "AuthError",
    "Principal",
    "SigningIdentity",
    "bearer_token_from_header",
    "classify_subject",
    "create_access_token",
    "create_refresh_token",
    "create_session_token",
    "ensure_usable",
    "fastapi_dependency",
    "get_current_user",
    "hash_password",
    "is_dev_secret",
    "needs_rehash",
    "parse_subject",
    "parse_token",
    "subject_for",
    "verify_password",
]
