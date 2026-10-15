"""SQL 白名单的**负面**断言（变更 001 / T13）。

`ALLOWED_TABLES` 是给 **LLM 自由生成的 SQL** 用的表白名单（`4.security.md` 第 4 节第 3 条）。
它默认拒绝：表不在名单里 → 拒绝。

## 为什么这个文件是「负面」的

既有的 `tests/security/` 用例验的是「白名单**允许**的那些表能用」。
本文件验的是反面：**某些表永远不许进去**。

它防的是一次非常具体的误操作：管理后台要展示账号，有人发现
「Text-to-SQL 读不到 `accounts`，得把它加进白名单」
—— 而 `accounts` 里存的是 `password_hash`（凭据本身）。
`NEVER_IN_SQL_WHITELIST` 那份清单就是那条防线的机器判据。
"""

from __future__ import annotations

from app.security.policy import (
    ALLOWED_TABLES,
    NEVER_IN_SQL_WHITELIST,
    PROTECTED_COLUMNS,
    ROLE_MAX_SENSITIVITY,
)
from app.security.rbac import ROLE_PERMISSIONS


def test_credential_tables_are_never_in_the_sql_whitelist() -> None:
    """**凭据表永远不许进 SQL 白名单**。

    加进去的后果不是「多读一张表」，而是：任何能问出正确问题的用户
    （或任何能让模型生成合适 SQL 的提示）都能把 `password_hash` 读出来。
    口令散列即使不可逆，也是**离线爆破**的输入 —— 而它的读者本该只有登录路径。
    """
    leaked = NEVER_IN_SQL_WHITELIST & ALLOWED_TABLES
    assert not leaked, (
        f"这些表绝不能出现在 SQL 白名单里：{sorted(leaked)}。"
        "它们是凭据/身份数据，只能由应用代码读写（见 app/security/policy.py 的注释）"
    )


def test_whitelist_is_the_eight_business_tables() -> None:
    """白名单就是那 8 张业务表 —— 多一张都要在这里显式说明理由。"""
    assert len(ALLOWED_TABLES) == 8, sorted(ALLOWED_TABLES)
    assert "users" in ALLOWED_TABLES
    assert "accounts" not in ALLOWED_TABLES


def test_accounts_table_has_no_protected_columns_entry() -> None:
    """`accounts` 也不该出现在 `PROTECTED_COLUMNS` 里。

    那张表是「**允许查、但这几列要拦**」的列黑名单（`users.phone` 等）。
    `accounts` 整张表都不在白名单里，所以它不该在这里出现 ——
    出现了就意味着有人把它当成了「可以查的表」，只是加了几个受保护列。
    """
    assert "accounts" not in PROTECTED_COLUMNS


def test_every_role_has_a_sensitivity_ceiling() -> None:
    """**每个能登录的角色都要有敏感等级**（变更 001 补的 bug 的机器判据）。

    这张表的口径是「表里没有的角色 = -1，什么都看不到」（默认拒绝）。
    变更 001 之前 `admin` 不在表里，于是管理员**看不到任何字段** ——
    它没暴露只是因为此前没人调用 `visible_sensitivity_ceiling`。
    这条用例把「授权角色」与「敏感等级」两份表钉在一起：
    新增一个角色时若忘了给等级，它会红，而不是等到演示时才发现「管理员看不到手机号」。
    """
    missing = set(ROLE_PERMISSIONS) - set(ROLE_MAX_SENSITIVITY)
    assert not missing, (
        f"这些角色没有敏感等级（按默认拒绝 = 什么都看不到）：{sorted(missing)}。"
        "在 app/security/policy.ROLE_MAX_SENSITIVITY 里给它们各加一行"
    )


def test_admin_ceiling_is_financial_not_unlimited() -> None:
    """管理员的敏感等级是**财务级**，不是「无上限」。

    为什么专门钉这一条：很容易觉得「管理员应该看得到一切」而给它加一个新等级。
    但敏感等级管的是「眼睛看多清楚」（PII 明文），而管理员需要的是**动作权限**。
    真正该防的是「管理员账号被盗后能直接导出所有人的手机号」，
    而不是「管理员看不到手机号所以没法干活」—— 后者用管理接口解决，不用抬高等级。
    """
    from app.security.policy import SENSITIVITY_FINANCIAL

    assert ROLE_MAX_SENSITIVITY["admin"] == SENSITIVITY_FINANCIAL
    assert ROLE_MAX_SENSITIVITY["admin"] < 99, "不该出现「无上限」这种等级"
