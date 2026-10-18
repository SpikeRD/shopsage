# 模块 04 · security（安全，旗舰模块）

## 目的

四个正交的边界，任何一条失效都是数据泄漏或越权：

1. **身份**（我是谁）—— JWT + 账号表；
2. **RBAC**（这个角色能做什么）—— 权限点；
3. **脱敏**（同一行数据能看多清楚）—— 敏感等级；
4. **SQL 守卫**（这条 SQL 允不允许跑）—— sqlglot AST。

## 关键结论

### 4.1 身份与令牌（变更 001）

- 令牌是「**主体 + 世代号**」：`sub` = `c{user_id}` / `e{emp_id}`（`app/security/auth.py:79 SUBJECT_PREFIX`），并带 `stype`/`sid`/`aid`/`ver` 声明（`app/security/auth.py:71-75`）。
- 算法仅 `HS256`（`app/security/auth.py:109 ALLOWED_ALGORITHMS`）；access 30 分钟、refresh 14 天（`:105-106`）。
- **停用/改密/改角色立即生效**：每个受保护请求比一次 `token_version`（`app/api/deps.py:291 principal_with_account`），**刻意不缓存**。
- **登录失败不可区分**：未知账号也付一次等价耗时的散列（`app/security/accounts.py:157 _DUMMY_HASH`）。
- 口令：**Argon2id**（m=65536, t=3, p=4，参数写死在代码里），旧的 `sha256$` 值仍可验并在登录成功那一刻原地升级（`app/security/auth.py:284-393`）。最小长度 **12**（`app/security/accounts.py:95`）。

### 4.2 RBAC（6 角色 / 8 权限点）

`app/security/rbac.py:54-111`（**以代码为准**；`docs/runbook/7.agents-runbook.md` §2.4 的旧实测输出已过时，见 `../../02-异常清单.md` E-08）：

| 角色 | 权限点 | 数据清晰度上限 | 入口 |
|---|---|---|---|
| `customer` | `read:own` `write:ticket` `request:refund` | 1 脱敏 | 对客 |
| `customer_service` | `read:own` `write:ticket` `write:refund` `approve:refund` | 0 公开 | 对内（变更 003） |
| `internal_employee` | `read:own` `read:all_orders` `write:ticket` `write:refund` `approve:refund` | 1 | 对内 |
| `manager` | 同 `internal_employee` | 2 内部可见 | 对内 |
| `finance_auditor` | `read:own` `read:all_orders` `read:finance` `write:ticket` `approve:refund` | 3 财务级 | 对内 |
| `admin` | 全部 8 个 | 3 财务级 | 管理台 |

三条容易看错的地方：

- `manager` 与 `internal_employee` **权限点完全相同**，差别只在敏感等级（`app/security/policy.py:96-115 ROLE_MAX_SENSITIVITY`）。
- `customer_service` 的可见度最低（公开级），比客户本人还低 —— 防社工/截图。
- `admin` 是"财务级"不是"无限"；它多出来的是**动作权限**（`admin` 权限点）。

**入口判据是能力不是角色名**：`app/security/rbac.py:139 can_enter_internal()` = 有 `read:all_orders` **或** `approve:refund`；`app/security/accounts.py:246 entry_for_role()` = 有 `admin` → 管理台，否则能进对内 → 对内，否则 → 对客。

⚠️ **不要混用**：`can_enter_internal`（能不能进界面）≠ `has_permission(role, PERM_READ_ALL_ORDERS)`（能不能查全站）。前端对应两个函数 `canUseInternalEntry` / `canUseInternal`（`web/src/lib/auth.ts:107-128`）。

**未登记角色 = 空权限集 + 敏感等级 -1**（默认拒绝），且 `agent.state.inject_identity` 会打 WARNING（`app/agents/state.py:270-276`）。

### 4.3 脱敏

`app/security/masking.py`：字段 → (掩码函数, 敏感等级) 两级映射（`FIELD_RULES` :127），值模式兜底扫描（`_VALUE_PATTERNS` :140），以及"永远打码"的键名（password/secret/token/api_key/salt，`_ALWAYS_MASK_HINTS` :149）。等级高于角色上限 → 整值 `***`。

⚠️ **已知未修缺陷**：`payment_method` 登记的是 `mask_bank_card`（面向 16~19 位数字），而真实值是 `alipay/wechat/card/cod`，结果**所有角色**（含财务级）都看到 `***`（`app/security/masking.py:136`，`issues/008`）。

### 4.4 SQL 守卫（D1 静态层）

`app/security/sql_guard.py:82 validate_sql()` 六道校验，**顺序本身是规则**：

1. 多语句 → 拦（`sqlglot.parse_one` 会静默丢后续语句，所以先用 `parse()`）；
2. 只读 → 必须是 `SELECT`/`UNION`，且不得含写节点（`SELECT ... INTO`、`WITH ... AS (DELETE ...)`）；
3. 表白名单 + 列白名单 + 拒绝 `SELECT *`（`policy.ALLOWED_TABLES`、`policy.PROTECTED_COLUMNS`）；
4. 危险函数（`pg_sleep`/`pg_read_file` 等，`policy.FORBIDDEN_FUNCTIONS`）；
5. WHERE / 全表扫描 / 恒真条件（**必须早于 6**，否则 `WHERE 1=1 OR user_id=5` 会被 LIMIT 豁免放过）；
6. LIMIT（上限 `policy.MAX_ROWS=200`，超了会被 clamp）。

行级过滤的**绑定值**由程序覆写，不采信模型：`app/agents/data.py:420 _bind_customer_id()`。

### 4.5 提示注入

`app/security/guardrail.py`：`scan()` 做归一化（去零宽字符/折叠空白）后用规则表匹配（`INJECTION_RULES` :30）；`assert_no_injection()` 入口拦截。输出侧另有 `app/agents/guardrail.py` 的 LLM 判定。

## 实际用法

```python
# app/security/rbac.py:164  路由级声明式鉴权
@require_permission(PERM_ADMIN)
def create_account(...): ...

# app/security/accounts.py:246
def entry_for_role(role: str) -> str:   # "admin" / "internal" / "customer"

# app/security/sql_guard.py:82
validate_sql("SELECT order_id FROM orders WHERE user_id = :uid LIMIT 20")
# → ValidationResult(sql=..., tables=('orders',), limit=20, ...)
```

## 待办 / 已知边界

- `issues/008`（`payment_method` 全员打码）**未修**，需要独立变更（产品要先决定"枚举值直出"还是"映射成中文名"）。
- `README.md:109-111` 说"越权按 404 返回"—— **已核验为真**：`app/api/errors.py:139-149` 把 `PermissionDenied` 映射成 `404` + `error.code="not_found_or_forbidden"`，理由见同文件 :25-38（用 403/404 的差异做订单号存在性探测是经典泄漏；对外 404、对内 WARNING 留痕）。

## 来源

- `docs/modules/4.security.md`（120 行，2026-10-15 17:41）、`docs/runbook/4.security-runbook.md`（683 行）
- `docs/changes/001-account-login-and-admin-console.md`、`docs/changes/003-refund-workflow-and-entry-permissions.md`
- `issues/003`（两个 P0 + 三个 P1）、`issues/008`
- 代码：`app/security/*.py`、`tests/security/*.py`
