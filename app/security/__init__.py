"""安全模块（第 4 步）：执行前的 SQL 校验/鉴权，执行后的脱敏。

对外接口按 `4.security.md` 第 2 节暴露，子模块职责：

| 子模块 | 职责 | 关键入口 |
|---|---|---|
| `policy` | 策略常量唯一真相（表白名单/高敏列/角色等级） | `ALLOWED_TABLES`、`ROLE_MAX_SENSITIVITY` |
| `sql_guard` | AST 校验：只读 + 白名单 + WHERE + LIMIT | `validate_sql`、`SQLSecurityError` |
| `masking` | 结果脱敏（按字段规则 + 角色等级） | `mask_pii`、`mask_phone`、`mask_address` |
| `rbac` | 越权拦截：对客行级过滤校验 / 对内 RBAC | `require_role`、`row_filter_for_customer` |
| `auth` | JWT 签发与校验 | `create_access_token`、`get_current_user` |
| `guardrail` | 提示词注入检测（D2 的第一道减速带） | `assert_no_injection` |

⚠️ 只做「拦截」，不做「生成 SQL」；行级过滤的唯一真相仍只有 `app/db/queries.py` 一处
（见 [`docs/runbook/3.db-runbook.md`](../../docs/runbook/3.db-runbook.md) §5.3 决策记录）。
"""

from app.security.auth import (
    AuthError,
    Principal,
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    parse_token,
    verify_password,
)
from app.security.guardrail import (
    PromptInjectionError,
    assert_no_injection,
    assert_safe_llm_output,
    scan,
)
from app.security.masking import mask_address, mask_phone, mask_pii
from app.security.rbac import (
    CustomerIdentity,
    PermissionDenied,
    require_permission,
    require_role,
    row_filter_for_customer,
)
from app.security.sql_guard import SQLSecurityError, ValidationResult, validate_sql

__all__ = [
    "AuthError",
    "CustomerIdentity",
    "PermissionDenied",
    "Principal",
    "PromptInjectionError",
    "SQLSecurityError",
    "ValidationResult",
    "assert_no_injection",
    "assert_safe_llm_output",
    "create_access_token",
    "create_refresh_token",
    "get_current_user",
    "hash_password",
    "mask_address",
    "mask_phone",
    "mask_pii",
    "parse_token",
    "require_permission",
    "require_role",
    "row_filter_for_customer",
    "scan",
    "validate_sql",
    "verify_password",
]
