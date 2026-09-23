"""SQLAlchemy 声明式基类 + 命名约定。

只放「所有模型共用」的东西：DeclarativeBase、约束命名规范、时间戳类型别名。
字段与业务表在 `models.py`，连接与池在 `session.py`。
"""

from __future__ import annotations

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase

# 约束命名约定：让 Alembic 生成的迁移里，索引/约束有**稳定、可预测**的名字。
# 不设它时 PostgreSQL 会自己起名（如 orders_user_id_fkey），后续 downgrade 或
# 改约束时就得靠猜；设了之后迁移脚本双向可读、可重复执行（3.db.md §8 第一条验收）。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# 统一存「有时区的时间戳」：库里永远是 UTC，展示层再按需转换。
# 用 timezone=True 的理由：naive datetime 在跨主机（Windows 写入 / 容器读取）场景下
# 会因两侧时区不同而出现「同一行数据看到两个时间」的假故障。
TimestampTZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    """全项目唯一的声明式基类（`alembic/env.py` 的 `target_metadata` 就是它的 metadata）。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        """`<Order order_id=1>`：排障时够用，且不打印整行数据。"""
        mapper = getattr(type(self), "__mapper__", None)
        if mapper is not None and mapper.primary_key:
            key = mapper.primary_key[0].key
            return f"<{type(self).__name__} {key}={getattr(self, key)!r}>"
        return f"<{type(self).__name__}>"


__all__ = ["Base", "NAMING_CONVENTION", "TimestampTZ"]
