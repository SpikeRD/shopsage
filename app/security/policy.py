"""安全策略的「唯一真相」：表白名单、列白名单、敏感字段等级、角色等级。

为什么把这些常量单独成模块：

1. `sql_guard`（拦 SQL）与 `rbac`（拦角色）必须查**同一张表**。表名白名单若各写一份，
   迟早漂移成「guard 允许、RBAC 不许」这种自相矛盾的状态。
2. 本模块**不 import 任何本项目模块**（也不碰 sqlglot / SQLAlchemy），
   所以它既能被 security 包内引用，也能被文档/脚本直接引用，不会引入循环依赖。
3. 白名单是**默认拒绝**：`4.security.md` 第 4 节第 3 条 —— 黑名单会被新语法绕过，
   真正兜底的是「只允许这里列出的表和列」。
"""

from __future__ import annotations

from typing import Final

# ── 表白名单（默认拒绝）─────────────────────────────────────────────────────
# 直接来自 `app/db/models.py` 的 8 张业务表（`3.db.md` 第 2 节）。PG 内部表
# （information_schema / pg_catalog）**不在**白名单里：Text-to-SQL 不该被允许去翻元数据。
ALLOWED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "users",
        "products",
        "orders",
        "order_items",
        "refunds",
        "stock_movements",
        "tickets",
        "employees",
    }
)

# ── 受保护列（按表分组的高敏列黑名单）────────────────────────────────────────
# 这里**不建议**把 8 张表 × 全部列抄一遍：那是一份必然漂移的复制品，还会让「新增列」
# 变成两处改动。所以列这一层与表那一层**方向相反**，理由如下：
#   · 表可以穷举（本仓就 8 张）→ 表用「默认拒绝」的白名单；
#   · 列随业务天天变，对列用默认拒绝会把正常查询全误杀 → 列用「只登记必须拦住的高敏列」，
#     其余放开（列名本身仍由数据库校验，不担心写错列名）。
# 于是：表不在 ALLOWED_TABLES 里 = 拒绝；列在本表里 = 拒绝；其余放行。
PROTECTED_COLUMNS: Final[dict[str, frozenset[str]]] = {
    # users 是全仓唯一带 PII 的表：手机号/邮箱/地址/证件号**不允许**被自由 SQL 直查，
    # 只能经 `app/db/queries.py` 的封装出口 + `masking` 脱敏后返回（4.security.md 第 4 节第 5 条）。
    "users": frozenset({"phone", "email", "address", "id_card"}),
    # 库存流水的 operator_id 能反查「哪个员工动的库存」，属内部审计信息
    "stock_movements": frozenset({"operator_id"}),
}

# 危险函数黑名单（函数名小写）：能读写文件、放大资源、或绕过白名单查别的表。
FORBIDDEN_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        # 文件读写
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "pg_write_file",
        "lo_import",
        "lo_export",
        # 外联 / 逃逸（dblink 能连到别的库，绕过本库的授权）
        "dblink",
        "dblink_connect",
        "pg_read_server_files",
        # 资源放大器（DoS）
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_terminate_backend",
        "pg_cancel_backend",
    }
)

# ── 敏感等级与角色等级 ───────────────────────────────────────────────────────
# 等级语义（数字越大越敏感）：
#   0 = 可对外（订单号、状态、时间）
#   1 = 需脱敏展示（手机号、地址、邮箱）
#   2 = 内部可见（真实姓名、客户主键级信息）
#   3 = 财务级（金额明细、支付方式）
#
# ⚠️ 等级是「字段属性」，与「谁在看」解耦：角色只声明自己能看到的**最高等级**，
# 于是新增字段时只需声明等级，不用回头改每个角色。这就是可扩展的「字段→规则」映射表。
SENSITIVITY_PUBLIC: Final[int] = 0
SENSITIVITY_MASKED: Final[int] = 1
SENSITIVITY_INTERNAL: Final[int] = 2
SENSITIVITY_FINANCIAL: Final[int] = 3

# 对客角色名。必须定义在 `ROLE_MAX_SENSITIVITY` **之前**（表里要用它当键）。
CUSTOMER_ROLE: Final[str] = "customer"

# 角色 → 可看到的最高敏感等级（字段等级 **高于** 上限 → 整值抹成 `***`；不高于 → 按该字段的
# 脱敏函数处理）。表里**没有**的角色 = -1，什么都看不到（默认拒绝）。
#
# ⚠️ 等级上限与「数据归属范围」是**两件正交的事**：
#    · 上限管「这个角色的眼睛能看多清楚」（同一行数据，客服看到 `***`，主管看到 `138****1234`）；
#    · 归属范围管「能看到谁的**行**」（D1 行级过滤，唯一真相在 `app/db/queries.py`）。
#   `customer` 就是「范围最窄、清晰度中等」的组合：只看得到自己的行，且自己那行的 PII 仍是脱敏值。
ROLE_MAX_SENSITIVITY: Final[dict[str, int]] = {
    CUSTOMER_ROLE: SENSITIVITY_MASKED,  # 客户：只看自己的行，PII 仍打码（连自己也不例外）
    "customer_service": SENSITIVITY_PUBLIC,  # 客诉一线：连脱敏都看不到（防社工/内鬼拍照）
    "internal_employee": SENSITIVITY_MASKED,  # 普通员工：脱敏后的手机号/地址/邮箱
    "manager": SENSITIVITY_INTERNAL,  # 主管：可见真实姓名（手机号/地址仍脱敏）
    "finance_auditor": SENSITIVITY_FINANCIAL,  # 财务审计：可见金额、支付方式，PII 仍脱敏
}

# 对客路径强制注入行级过滤时用的列名（D1 唯一真相在 `app/db/queries.py`，
# 这里只提供「注入什么列名」这一个常量，避免两处各写一个字符串字面量）
ROW_FILTER_COLUMN: Final[str] = "user_id"

# 单条查询返回行数上限（C1）。`app/db/queries.py::MAX_LIMIT` 是同口径的业务出口值；
# 这里给 SQL 守卫用，因为守卫拦的是「LLM 自由生成的 SQL」。
MAX_ROWS: Final[int] = 200

__all__ = [
    "ALLOWED_TABLES",
    "CUSTOMER_ROLE",
    "FORBIDDEN_FUNCTIONS",
    "MAX_ROWS",
    "PROTECTED_COLUMNS",
    "ROLE_MAX_SENSITIVITY",
    "ROW_FILTER_COLUMN",
    "SENSITIVITY_FINANCIAL",
    "SENSITIVITY_INTERNAL",
    "SENSITIVITY_MASKED",
    "SENSITIVITY_PUBLIC",
]
