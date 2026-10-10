"""「不是本仓 ORM 模型、必须排除在迁移比对之外」的表清单（**唯一真相**）。

为什么单独一个模块（而不是写在 `alembic/env.py` 里）：

1. **`alembic/env.py` 不能被测试 import**：那个模块在 import 期就执行
   `config = context.config`，而 `alembic.context` 只有在 alembic 自己跑起来时才有
   `config` 属性 —— 直接 import 会得到
   `AttributeError: module 'alembic.context' has no attribute 'config'`。
   于是「测试怎么拿到同一份排除口径」就成了问题：抄一份到测试里必然漂移，
   而漂移的后果是「测试报库里多了三张表，人手工 autogenerate 生成的是删表」。
2. **这份清单本身是事实，不是配置**：它描述的是「哪些表的结构不由我们的模型定义」，
   与「怎么跑迁移」无关，所以放一个不依赖 alembic 运行时的模块最合适。

两类表：

* `CHECKPOINT_TABLES`（第 8 步）：LangGraph checkpointer 的四张表，由
  `AsyncPostgresSaver.setup()` 自建，但**同时被注册进了 SQLAlchemy 的全局 metadata**
  （那是 `langgraph-checkpoint-postgres` 的实现方式）。
* `RAG_TABLES`（第 10 步）：向量库三张表，**纯 DDL 建的**（`vector(n)` / `tsvector` /
  HNSW / GIN / PL/pgSQL 触发器），刻意没有等价的 SQLAlchemy 模型 ——
  用 ORM 类型重写一遍 pgvector 与 tsvector 只会多一份可能漂移的「第二真相」。

不排除的后果对两类都一样：`alembic revision --autogenerate` 会生成一串
`op.drop_table(...)`，它在评审里长得完全像一次正常的清理，跑下去就是
**把会话状态与整个知识库删掉**。`tests/test_db_migrations.py` 有一组用例钉住这条。
"""

from __future__ import annotations

from typing import Any, Final

from app.graph.checkpointer import CHECKPOINT_TABLES
from app.rag.schema import RAG_TABLES

EXCLUDED_TABLES: Final[tuple[str, ...]] = tuple(CHECKPOINT_TABLES) + tuple(RAG_TABLES)


def is_excluded_from_autogenerate(object_: Any, name: str | None = None, type_: str | None = None) -> bool:
    """这个对象（表/索引/约束/触发器）是否该被排除在 autogenerate 与结构比对之外。

    判据四选一（表名、`name`、所属表、所属表名）——因为 alembic 在不同阶段传进来的
    对象形态不同：`Table` 自身有 `name`；`Index`/`Constraint` 有 `table`；
    某些反射对象只有 `table_name`。少判一种就会出现「表排除了、它的索引还在比」，
    于是 diff 里冒出一堆 `remove_index`（看起来像真漂移，其实是口径没对齐）。
    """
    del type_  # 目前不按类型区分（表与它的从属对象用同一份清单）
    if name in EXCLUDED_TABLES:
        return True
    if getattr(object_, "name", None) in EXCLUDED_TABLES:
        return True
    table = getattr(object_, "table", None)
    if table is not None and getattr(table, "name", None) in EXCLUDED_TABLES:
        return True
    parent = getattr(object_, "table_name", None)
    return isinstance(parent, str) and parent in EXCLUDED_TABLES


def apply_reflection_adapters() -> bool:
    """让 SQLAlchemy 认识 `vector` 类型（**返回值 = 是否注册成功**）。

    为什么需要：`vector(n)` 是 pgvector 装进数据库的类型，SQLAlchemy **默认不认识**。
    反射到它时会打一条
    `SAWarning: Did not recognize type 'vector' of column 'embedding'`
    并把列类型当成 NULL 类型。后果不是报错，而是**噪音**：
    `alembic revision --autogenerate` 与 `tests/test_db_migrations.py` 的
    `compare_metadata` 都会在「本仓没有该表的模型」之外再多出一条难解释的告警
    （而告警多了之后，真告警就没人看了）。

    为什么要从 `pgvector.sqlalchemy` 取而不是自己写一个 `UserDefinedType`：
    自己写的那份不会带上 `vector_cosine_ops` 之类的算子类，将来谁要用 ORM 建向量列
    就会得到一个「看起来对、索引建不出来」的类型。取不到（pgvector 的 Python 包没装，
    本仓就没装 —— DDL 是手写的，不需要它的类型）就返回 False：
    「镜像里有扩展、Python 包里没类型」是合法组合，不该让迁移直接失败。

    实测提醒：本仓**没有**安装 `pgvector` 这个 Python 包（只用了数据库侧的扩展），
    所以这个适配器默认是空操作 —— 反射 `rag_chunks.embedding` 时仍会打一条
    `SAWarning`。它的处置方式是「精确抑制那一条告警 + 注释指向这里」
    （见 `tests/test_db_migrations.py`），而不是为了消一条告警引入一个依赖。
    """
    try:
        from pgvector.sqlalchemy import Vector
    except (ImportError, ModuleNotFoundError):
        return False

    from sqlalchemy.dialects.postgresql.base import PGTypeCompiler, ischema_names

    ischema_names.setdefault("vector", Vector)
    PGTypeCompiler.visit_vector = PGTypeCompiler.visit_VECTOR = (  # type: ignore[attr-defined]
        lambda self, type_, **kw: f"vector({type_.dim})" if getattr(type_, "dim", None) else "vector"
    )
    return True


__all__ = ["EXCLUDED_TABLES", "apply_reflection_adapters", "is_excluded_from_autogenerate"]
