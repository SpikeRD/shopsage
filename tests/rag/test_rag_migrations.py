"""迁移与 DDL 的对账用例：**防止「迁移文件自己写一份」带来的漂移**。

背景（`alembic/versions/20261010_a1f0c3d5b7e9_add_rag_vector_tables.py` 的注释）：
迁移**刻意不 import `app.rag.schema`**（历史上执行过的迁移不该随应用代码变化），
所以 DDL 有两份：`schema.py`（应用/测试建表用）与迁移文件（版本化建表用）。
两份必然有漂移风险 —— 这组用例就是那个风险的对手：读文件、比对关键事实。

比的是**事实**（维度、表名、索引种类、函数名），而不是逐字比对 SQL：
逐字比对会让「格式化一下迁移」这种事变红，而它对正确性没有影响。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.rag import schema

MIGRATION_FILE = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "20261010_a1f0c3d5b7e9_add_rag_vector_tables.py"
)


@pytest.fixture(scope="module")
def migration_source() -> str:
    assert MIGRATION_FILE.exists(), f"迁移文件不见了：{MIGRATION_FILE}"
    return MIGRATION_FILE.read_text(encoding="utf-8")


def test_migration_does_not_import_app_code(migration_source: str):
    """迁移不许 import 应用代码（否则「改 schema.py」会改写历史迁移）。

    这条比「DDL 是否一致」更重要：一旦迁移 import 了应用模块，
    它执行的 SQL 就会随应用代码变化 —— 同一个 revision ID 在不同时间跑出不同结构，
    这会让「迁移历史」这件事本身失去意义。
    """
    forbidden = re.findall(r"^\s*(?:from|import)\s+(app\.[\w.]+)", migration_source, re.MULTILINE)
    assert not forbidden, f"迁移文件 import 了应用模块：{forbidden}"


def test_migration_vector_dim_matches_schema(migration_source: str):
    """`vector(n)` 的 n 必须与 `schema.VECTOR_DIM` 一致（改一处必须改两处）。"""
    declared = re.search(r"^VECTOR_DIM\s*=\s*(\d+)", migration_source, re.MULTILINE)
    assert declared, "迁移里找不到 VECTOR_DIM 常量"
    assert int(declared.group(1)) == schema.VECTOR_DIM, (
        f"迁移维度 {declared.group(1)} 与 schema.VECTOR_DIM {schema.VECTOR_DIM} 不一致："
        "两处必须同时改，并重建索引 + 重灌向量"
    )
    assert f"vector({{VECTOR_DIM}})" in migration_source or "vector({VECTOR_DIM})" in migration_source


def test_migration_table_and_index_names_match_schema(migration_source: str):
    """表名/索引名/函数名必须成套一致（否则测试建的临时表与生产不是一回事）。"""
    for expected in (
        schema.DOCUMENTS_TABLE,
        schema.CHUNKS_TABLE,
        schema.STATS_TABLE,
        schema.NGRAM_FUNCTION,
        schema.NGRAM_TRIGGER,
    ):
        assert expected in migration_source, f"迁移里缺少 {expected}"


def test_migration_has_hnsw_and_gin_indexes(migration_source: str):
    """索引种类是「性能是否站得住」的关键：HNSW（向量）+ GIN（关键词）。"""
    assert "using hnsw" in migration_source
    assert "vector_cosine_ops" in migration_source
    assert "using gin" in migration_source
    assert "where is_active" in migration_source  # 部分索引


def test_schema_ddl_has_the_same_index_kinds():
    ddl = "\n".join(schema.create_statements(1024))
    assert "using hnsw" in ddl and "using gin" in ddl and "where is_active" in ddl


def test_migration_downgrade_drops_without_dropping_the_extension(migration_source: str):
    """`downgrade` 只删表与函数，**不**删 vector 扩展（它是集群级对象）。

    删扩展会误伤同实例里别的库/表（扩展是集群级的），而「回滚到第 9 步」只需要
    `rag_*` 消失。这里比对 `DOWNGRADE_DDL` 常量（执行体只遍历它），
    并且用**常量名**比对（迁移里写的是 f-string `{CHUNKS_TABLE}`，
    逐字比对表名会红在一个没有意义的地方 —— 表名一致性由上面那条用例保证）。
    """
    downgrade_ddl = migration_source.split("DOWNGRADE_DDL = [", 1)[1].split("]", 1)[0]
    assert "drop extension" not in downgrade_ddl
    assert "drop table if exists {CHUNKS_TABLE}" in downgrade_ddl
    # `drop table` 不会带走触发器用的函数 —— 漏掉会留下悬空函数
    assert "drop function if exists {NGRAM_FUNCTION}" in downgrade_ddl
    assert "drop table if exists {STATS_TABLE}" in downgrade_ddl
    assert "drop table if exists {DOCUMENTS_TABLE}" in downgrade_ddl
    # 三张表都必须被删（漏一张会让下次 upgrade 撞上「表已存在但结构旧」）
    assert downgrade_ddl.count("drop table if exists") == 3


def test_alembic_env_excludes_rag_tables_from_autogenerate():
    """`alembic/env.py` 必须把 rag 三张表排除在 autogenerate 的比对之外。

    不排除的后果（真实且灾难性）：`alembic revision --autogenerate` 会生成一串
    `op.drop_table('rag_chunks')` —— 在评审里它长得完全像一次正常的清理，
    跑下去就是把整个知识库删掉。这条用例把「有没有排除」变成可判定的。

    ⚠️ 排除清单**不在 env.py 里**（第 10 步的重构）：`alembic/env.py` 在 import 期就执行
    `config = context.config`，测试根本 import 不了它（`AttributeError: module
    'alembic.context' has no attribute 'config'`）。所以清单被抽到
    `app.db.excluded`，与 `tests/test_db_migrations.py` 共用同一份 —— 抄一份必然漂移。
    """
    from app.db.excluded import EXCLUDED_TABLES, is_excluded_from_autogenerate

    for table in schema.RAG_TABLES:
        assert table in EXCLUDED_TABLES, f"{table} 不在排除清单里"

    env_source = (
        Path(__file__).resolve().parents[2] / "alembic" / "env.py"
    ).read_text(encoding="utf-8")
    assert "is_excluded_from_autogenerate" in env_source
    include_body = env_source.split("def _include_object", 1)[1].split("\ndef ", 1)[0]
    assert "is_excluded_from_autogenerate" in include_body

    # 行为自检：拿真的对象问一遍（而不是只看文本里有这个词）
    class FakeIndex:
        def __init__(self, name: str, table: str) -> None:
            self.name = name
            self.table = type("T", (), {"name": table})()

    assert is_excluded_from_autogenerate(None, schema.CHUNKS_TABLE, "table") is True
    assert is_excluded_from_autogenerate(FakeIndex("idx", schema.CHUNKS_TABLE), "idx", "index") is True
    assert is_excluded_from_autogenerate(None, "orders", "table") is False
    assert is_excluded_from_autogenerate(FakeIndex("idx", "orders"), "idx", "index") is False


def test_split_statements_is_dollar_quote_aware():
    """`split_statements` 必须无视 `$tag$ ... $tag$` 块内部的分号。

    这就是「迁移文件为什么把触发器包成一个 DO 块」的同一个问题：
    天真地按分号切会把 PL/pgSQL 函数体切碎，症状是「迁移跑到一半报语法错误」。
    """
    script = """
    create table t (id int);
    do $rag$
    begin
        execute $fn$ create function f() returns trigger as $body$ begin return new; end $body$ $fn$;
    end
    $rag$;
    create index i on t (id);
    """
    statements = schema.split_statements(script)
    assert len(statements) == 3
    assert statements[0].startswith("create table")
    assert statements[1].startswith("do $rag$")
    assert statements[1].rstrip().endswith("$rag$")
    assert statements[2].startswith("create index")


def test_every_ddl_statement_is_a_single_statement():
    """每条 DDL 只描述一件事：`op.execute()` 一次只吃一条语句。"""
    for statement in schema.create_statements(1024):
        # 去掉 dollar-quote 块之后不该还有分号
        masked = re.sub(r"\$[A-Za-z_]*\$.*?\$[A-Za-z_]*\$", "BLOCK", statement, flags=re.DOTALL)
        assert ";" not in masked.strip().rstrip(";"), statement


def test_create_statements_are_idempotent_by_construction():
    """每条建表/建索引语句都必须带 `if not exists`（迁移与测试都会重复执行）。"""
    for statement in schema.create_statements(1024):
        head = " ".join(statement.split())[:40].lower()
        assert head.startswith(("create table if not exists", "create index if not exists",
                                "create extension if not exists", "insert into", "drop trigger if exists",
                                "create trigger", "do ")), head
