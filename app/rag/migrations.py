"""Alembic 迁移共用的执行辅助（第 10 步 rag 用）。

为什么单独一个模块：迁移脚本要执行 **DO 块 / 触发器 / 扩展** 这类 SQLAlchemy
`op.create_table` 表达不了的 DDL，而这里有两个真实的坑：

1. **`op.execute()` 一次只吃一条语句** → DDL 必须先切成单条（切分规则见
   `schema.split_statements`，它的坑比看上去多）。
2. **不能走 SQLAlchemy 的 `text()` 参数绑定**：DDL 里有 `$$`、`\\s+` 这类文本，
   任何被误当成绑定参数或转义的字符都会让迁移在别人机器上才失败。

所以统一走驱动层的 `exec_driver_sql()`：**不解析、不绑定、原样发给数据库**。
"""

from __future__ import annotations

import logging
from typing import Any

from app.rag.schema import split_statements

logger = logging.getLogger(__name__)


def execute_ddl(connection: Any, statements: list[str], *, label: str = "") -> None:
    """按顺序执行一批 DDL（每条一个 `exec_driver_sql`）。

    逐条执行而不是拼成一大条：出问题时 PostgreSQL 的报错会指到**具体那一条**，
    拼起来就只能看到「某处语法错误」，而 rag 的 DDL 有十来条。
    """
    for index, statement in enumerate(statements, start=1):
        preview = " ".join(statement.split())[:90]
        logger.debug("DDL[%s %d/%d] %s", label or "rag", index, len(statements), preview)
        connection.exec_driver_sql(statement)


def execute_sql_script(connection: Any, script: str, *, label: str = "") -> None:
    """把一段多语句 SQL 切成单条后执行。"""
    execute_ddl(connection, split_statements(script), label=label)


__all__ = ["execute_ddl", "execute_sql_script"]
