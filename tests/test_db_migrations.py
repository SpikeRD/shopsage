"""迁移（Alembic）的测试。

分两层：
- **本地**：迁移仓库自身的一致性（有且只有一个 head、没有分叉）——不需要数据库；
- **集成**：库里的表 == 模型定义的表；以及最有价值的那条 —— **`downgrade base` + `upgrade head`
  跑两遍都能过**（`3.db.md` §8 第一条验收：迁移可重复执行）。

⚠️ 回环用例会把 8 张业务表删掉再建回来（末尾重新灌种子数据）。
在共享的演示库上跑时，确认没有别人正在用；它只影响 `public` schema 里的本项目的表。
"""

from __future__ import annotations

import inspect
import io
import textwrap
from typing import TextIO

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db.base import Base
from app.db.session import engine
from app.db.urls import to_sqlalchemy_url
from scripts.db_check import BUSINESS_TABLES, snapshot

# 必须显式 import 模型模块：`Base.metadata.tables` 在被测模型 import 之前是空的。
# 这一行同时也是「为什么 alembic/env.py 里必须 import app.db.models」的现场演示
# —— 少了它，下面的 MODEL_TABLES 会是空集合，而 autogenerate 会静默生成空迁移。
import app.db.models  # noqa: E402,F401

ALEMBIC_INI = "alembic.ini"

# 模型定义的表（不含 alembic 自己的版本表）
MODEL_TABLES = frozenset(Base.metadata.tables)


def _alembic_config(output_buffer: TextIO | None = None) -> Config:
    """带 `script_location` 的 Config（等价于命令行 `-c alembic.ini`）。

    `output_buffer` 只用于离线模式：MigrationContext 把生成的 SQL 写到
    `config.output_buffer`（默认 `sys.stdout`），由 `alembic/env.py` 的
    `run_migrations_offline` 透传。
    """
    cfg = Config(ALEMBIC_INI, output_buffer=output_buffer)
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", to_sqlalchemy_url(get_settings().database_url))
    return cfg


# ── 本地：迁移仓库自身 ────────────────────────────────────────────────────────


def test_migration_history_has_a_single_head():
    """只有一个 head 且无分叉：多人并行开发时最容易出现两个 head（要靠 alembic merge 收口）。"""
    script = ScriptDirectory.from_config(_alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, f"迁移有 {len(heads)} 个 head（分叉了）: {heads}"


def test_every_revision_has_downgrade_body():
    """每个迁移都要写得出 downgrade：「upgrade 能跑、downgrade 是 pass」属于半成品迁移。"""
    script = ScriptDirectory.from_config(_alembic_config())
    empty: list[str] = []
    for revision in script.walk_revisions():
        source = textwrap.dedent(inspect.getsource(revision.module.downgrade))
        body = [ln.strip() for ln in source.splitlines()[1:] if ln.strip() and not ln.strip().startswith("#")]
        if body == ["pass"] or not body:
            empty.append(revision.revision)
    assert not empty, f"这些迁移的 downgrade 是空的（回滚即残库）: {empty}"


def test_initial_migration_creates_all_eight_tables():
    """首版迁移文件里必须出现 8 张业务表的 create_table。

    这条专门防「`env.py` 里漏 import 模型 → autogenerate 静默生成空迁移」：
    首版迁移的 upgrade 体里应当逐一 op.create_table('users') / ('orders') / ...
    """
    script = ScriptDirectory.from_config(_alembic_config())
    revisions = list(script.walk_revisions())  # 从新到旧
    assert revisions, "alembic/versions 里没有任何迁移（先跑 alembic revision --autogenerate）"
    first = revisions[-1]  # 最早的那条 = 首版
    source = inspect.getsource(first.module.upgrade)
    missing = [t for t in BUSINESS_TABLES if f"'{t}'" not in source and f'"{t}"' not in source]
    assert not missing, f"首版迁移 {first.revision} 里没有建这些表: {missing}（空迁移？检查 env.py 有没有 import 模型）"
    assert len(MODEL_TABLES) == 8, f"模型里有 {len(MODEL_TABLES)} 张表，应为 8 张业务表"


# ── 集成：库与模型是否一致 ────────────────────────────────────────────────────


def test_all_model_tables_exist_in_database():
    snap = snapshot()
    assert not snap.missing_tables, f"库里缺少业务表: {snap.missing_tables}（先跑 alembic upgrade head）"
    for table in BUSINESS_TABLES:
        assert table in snap.tables


def test_alembic_version_is_recorded():
    """必须由迁移建表：`alembic_version` 有值（用 create_all 建的库这里会是空/缺表）。"""
    snap = snapshot()
    assert "alembic_version" in snap.tables
    assert snap.revision, "alembic_version 为空：库不是由迁移建起来的"


def test_no_pending_migration_against_models():
    """库结构与模型一致：`compare_metadata` 没有差异 = 没有「改了模型没生成迁移」。

    这条是防「忘记 revision --autogenerate」的那道闸（也是 3.db-runbook §3.4 的自动化版）。
    """
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": True})
        diffs = compare_metadata(ctx, Base.metadata)
    # 只关心「表/列/约束」级别的差异；把差异打印出来便于排障
    assert not diffs, "库与模型不一致（模型改了但没生成迁移？）：\n" + "\n".join(str(d) for d in diffs)


def test_naming_convention_applied_in_database():
    """约束名按 Base.metadata 的命名约定生成（迁移里双向可读、可重复执行的前提）。"""
    inspector = sa_inspect(engine)
    names: list[str] = []
    for table in BUSINESS_TABLES:
        names += [fk["name"] for fk in inspector.get_foreign_keys(table)]
        names.append(inspector.get_pk_constraint(table)["name"])
    assert names, "没读到任何约束名"
    assert all(n and n.startswith(("fk_", "pk_")) for n in names), f"有约束名不符合约定: {names}"


def test_migration_round_trip_is_repeatable():
    """`downgrade base` → `upgrade head` 跑**两遍**都成功，且表集合回到 9 张。

    末尾重新灌种子数据，避免把演示库留在空库状态。
    """
    cfg = _alembic_config()

    def table_set() -> set[str]:
        with engine.connect() as conn:
            return {
                r[0]
                for r in conn.execute(
                    text("select table_name from information_schema.tables where table_schema = 'public'")
                )
            }

    for round_no in (1, 2):
        command.downgrade(cfg, "base")
        after_downgrade = table_set()
        assert BUSINESS_TABLES and not (set(BUSINESS_TABLES) & after_downgrade), (
            f"第 {round_no} 轮回滚后仍残留业务表: {set(BUSINESS_TABLES) & after_downgrade}"
        )

        command.upgrade(cfg, "head")
        after_upgrade = table_set()
        assert set(BUSINESS_TABLES) <= after_upgrade, (
            f"第 {round_no} 轮升级后缺表: {set(BUSINESS_TABLES) - after_upgrade}"
        )

    # 收尾：把种子数据灌回来，别让演示库空着
    from scripts.seed import seed

    counts = seed()
    assert all(counts[t] >= 5 for t in BUSINESS_TABLES), counts


def test_offline_sql_generation_does_not_need_database():
    """离线模式（`alembic upgrade head --sql`）要能在**不连库**的前提下产出建表 SQL。

    URL 故意指向一个不存在的端口：只要还能产出 SQL，就证明离线分支没有偷偷连库。
    """
    buffer = io.StringIO()
    # 离线 SQL 走 MigrationContext 的 output_buffer（不是 stdout）——
    # env.py 的 run_migrations_offline 会把它透传下去
    cfg = _alembic_config(output_buffer=buffer)
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://u:p@127.0.0.1:1/none")

    command.upgrade(cfg, "head", sql=True)

    ddl = buffer.getvalue()
    assert ddl, "离线模式没有产出任何 SQL"
    assert "CREATE TABLE" in ddl.upper()
    for table in BUSINESS_TABLES:
        assert f"CREATE TABLE {table}" in ddl, f"离线 SQL 里没有 {table} 的建表语句"


def test_database_is_reachable_via_app_settings():
    """连接串从 Settings 来（不是硬编码）：实际连上的库名与配置里的库名一致。"""
    url = to_sqlalchemy_url(get_settings().database_url)
    with create_engine(url).connect() as conn:
        actual = conn.execute(text("select current_database()")).scalar_one()
    assert actual == make_url(url).database, f"连上的是 {actual}，配置里写的是 {make_url(url).database}"
