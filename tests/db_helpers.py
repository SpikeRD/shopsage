"""db 模块测试的公共帮助函数（**不是**测试文件，也不放 pytest 装置 —— 装置在 `conftest.py`）。

这里只放「显式调用才生效」的普通函数：可用性探测与种子前置检查。
测试文件里一眼能看到自己依赖了什么，不靠 pytest 的隐式机制。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import engine
from scripts.db_check import snapshot


def database_available() -> bool:
    """PostgreSQL 是否可达（跨主机连不上时用于 skip，而不是让整个测试套变红）。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return True
    except SQLAlchemyError:
        return False


def tables_exist() -> bool:
    """8 张业务表是否都建好了（没跑过 `alembic upgrade head` 就 skip 集成用例）。"""
    try:
        return not snapshot().missing_tables
    except SQLAlchemyError:
        return False
