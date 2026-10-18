# JWT + Argon2id（身份与口令）

## 本项目用它解决什么

一套登录服务客户与员工两侧（靠 `subject_type` 区分）；令牌带"世代号"实现**立即失效**；口令用它自己的存储格式，支持旧值原地升级。

## 实际用法（代码片段 + 文件:行号）

### 1）令牌声明：主体 + 世代号

```python
# app/security/auth.py:71-84
CLAIM_SUB: Final[str] = "sub"
CLAIM_SUBJECT: Final[str] = "stype"
CLAIM_SUBJECT_ID: Final[str] = "sid"
CLAIM_ACCOUNT_ID: Final[str] = "aid"
CLAIM_TOKEN_VERSION: Final[str] = "ver"
SUBJECT_PREFIX: Final[dict[str, str]] = {"customer": "c", "employee": "e"}
SUBJECT_TABLE: Final[dict[str, str]] = {"customer": "users.user_id", "employee": "employees.emp_id"}
```

```python
# app/security/auth.py:105-109
DEFAULT_TTL_SECONDS: Final[int] = 30 * 60           # access 30 分钟
DEFAULT_REFRESH_TTL_SECONDS: Final[int] = 14 * 24 * 3600   # refresh 14 天
ALLOWED_ALGORITHMS: Final[tuple[str, ...]] = ("HS256",)
```

### 2）每请求校验世代号（刻意不缓存）

```python
# app/api/deps.py:291
def principal_with_account(principal: Principal, db: Any) -> Principal:
    """查一次 accounts.token_version；不相等说明令牌已被作废。"""
```

### 3）口令：Argon2id + 自描述存储值 + 原地升级

```python
# app/security/auth.py:284-287
PASSWORD_SCHEME: Final[str] = "argon2"
LEGACY_PASSWORD_SCHEME: Final[str] = "sha256"

# app/security/auth.py:341 / :362 / :393
def hash_password(password: str, *, salt: str | None = None, scheme: str = PASSWORD_SCHEME) -> str: ...
def verify_password(password: str, stored: str) -> bool: ...
def needs_rehash(stored: str) -> bool: ...      # 登录成功后据此原地升级
```

### 4）防枚举：未知账号也付一次散列

```python
# app/security/accounts.py:157
_DUMMY_HASH: Final[str] = hash_password("dummy-password-for-timing-equalization")
```

### 5）启动自检 + 口令强度

```python
# app/security/auth.py:265
def ensure_usable() -> None:
    """"prod 缺密钥 / 用开发默认密钥 / 密钥 <16 字符 → 启动即失败。"""

# app/security/accounts.py:95 / :113
MIN_PASSWORD_LENGTH: Final[int] = 12
def validate_password_strength(password: str, *, identifier: str = "") -> None: ...
```

## 最小原理

1. **JWT = header.payload.signature**：payload 是 **base64 明文**（不是加密），所以里面绝不能放敏感数据；签名只是"没被改过"的证明。
2. **HS256 = HMAC-SHA256 对称签名**：服务端与验证端共用一把密钥。换非对称（RS256）才能把验证权分出去。
3. **Argon2id 是内存硬（memory-hard）的密钥派生函数**：`m=65536`(KB)、`t=3`(迭代)、`p=4`(并行度)。内存硬的意义在于让**暴力破解的边际成本随内存增长**，GPU 优势被削弱。
4. **定时安全比较**：逐字节比较会在第一个不同的字节提前返回，泄露"前几位对了"。Argon2 自带恒定时间比较；旧方案用 `hmac.compare_digest`。
5. **世代号（token_version）**：把"令牌有效性"从"过期时间"扩展成"过期时间 + 账号上的一个整数"。改密/停用/改角色 → 整数 +1 → 所有旧令牌在下一次请求时失效。**这是用一次主键查询换"立即生效"的取舍**。

## 常见坑

| 坑 | 症状 | 依据 |
|---|---|---|
| 把 `user_id` 当唯一主体 | 员工 `emp_id=7` 与客户 `user_id=7` 落进**同一个会话桶** | `app/api/deps.py:176-188`（变更 001 改的） |
| 前端把 JWT payload 当"可信数据" | payload 可被任意解码（不是加密） | `app/security/auth.py:87-101` |
| 缓存 token_version | "立即失效"退化成"延迟生效" | `app/security/policy` 与 `app/api/deps.py:291` 的设计说明 |
| 用错失败文案 | "账号不存在" vs "口令错误" 可区分 → 账号枚举 | `app/security/accounts.py:350` 与 `_DUMMY_HASH` |
| prod 用开发默认密钥 | 谁都能签令牌 | `app/security/auth.py:112`（`_DEV_SECRET`）+ `ensure_usable()` |
| Argon2 存储值双 `$` | 验证永远失败 | 变更 001 §7.5 坑 1（已修） |

## 深入关键词

`JWT HS256` · `sub` / `exp` / `iat` claim · refresh token rotation · `token_version` 失效机制 · `Argon2id`（memory-hard KDF）· `argon2-cffi PasswordHasher` · `hmac.compare_digest` · timing attack · account enumeration · `SecretStr`
