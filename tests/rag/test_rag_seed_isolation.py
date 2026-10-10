"""「灌种子数据」与「向量库」必须互不相干（第 10 步实测踩到的一个连带事故）。

事故经过：`scripts/seed.py` 用
`TRUNCATE TABLE 8 张业务表 ... CASCADE` 清空演示库。第 10 步加了向量表之后，
`rag_chunks.doc_id → rag_documents.doc_id ON DELETE CASCADE` 这条外键让
**CASCADE 顺着删掉了整个向量库**。后果：

* `tests/conftest.py` 的 `seeded` 装置（需要种子数据的用例声明它）会让
  「跑一次全量测试」= 「清空知识库」；
* 随后 `tests/rag/test_rag_live.py` 以「向量库是空的」而 skip ——
  现象看起来完全像「检索坏了」，而真正的原因是种子脚本。

处置：`_truncate_business_tables()` 显式只清业务表（向量库与它没有依赖关系）。
本文件把这条钉死 —— 否则下一个人会顺手把它"统一"回去。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

PROBE_SOURCE = "docs/kb/_seed_isolation_临时.md"
PROBE_BODY = """# 种子隔离探针

第 1 条：这段内容用来验证「灌种子数据不会清空向量库」，所以它需要足够长以避免被当成碎片合并。

第 2 条：第二段占位内容，同样写得长一些，让分块结果稳定可预期。
"""


def test_seed_does_not_truncate_the_vector_store(scratch_ingest):
    """灌一次种子数据 → 向量库里刚灌的片段**仍在**（且可检索）。

    用临时表（`scratch_store`）而不是生产向量表：
    这条验证的是「seed 的 TRUNCATE 语句里没有向量表」，
    与「片段存在哪张表」无关，而临时表让用例不影响开发库里的真实语料。
    """
    from scripts.seed import seed

    scratch_ingest.ingest(PROBE_SOURCE, PROBE_BODY, category="kb")
    before = scratch_ingest.store.corpus_stats()
    assert before["chunks"] >= 1

    # 真正执行一次「清业务表 + 灌种子」（幂等，末尾会把演示数据灌回去）
    counts = seed()
    assert counts, "seed() 应当返回各业务表行数"

    after = scratch_ingest.store.corpus_stats()
    assert after["chunks"] == before["chunks"], (
        f"灌种子数据把向量库清掉了：{before} → {after}。"
        "检查 scripts/seed.py::_truncate_business_tables 的 TRUNCATE 列表里是不是混进了 rag_*。"
    )


def test_business_tables_have_no_foreign_key_to_rag_tables(scratch_store):
    """结构层理由：业务表与向量表**没有任何外键关系**，所以清业务表不该牵连向量表。

    这条是上面那条用例的「为什么可以这么断言」——如果将来有人给业务表加了指向
    `rag_chunks` 的外键，这条会先红（那时的处置是「重新想清楚这两个域的边界」，
    而不是"顺手让 seed 把向量库也清了"）。
    """
    from app.rag.schema import CHUNKS_TABLE, DOCUMENTS_TABLE, STATS_TABLE
    from scripts.db_check import BUSINESS_TABLES

    with scratch_store.read_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                select tc.table_name as child, ccu.table_name as parent
                from information_schema.table_constraints tc
                join information_schema.constraint_column_usage ccu
                  on ccu.constraint_name = tc.constraint_name
                where tc.constraint_type = 'FOREIGN KEY'
                  and tc.table_schema = 'public'
                """
            )
        ).all()

    rag_tables = {CHUNKS_TABLE, DOCUMENTS_TABLE, STATS_TABLE}
    business = set(BUSINESS_TABLES)
    cross = [
        (row.child, row.parent)
        for row in rows
        if (row.child in business and row.parent in rag_tables)
        or (row.child in rag_tables and row.parent in business)
    ]
    assert not cross, f"业务表与向量表之间出现了外键：{cross}"


def test_seed_module_does_not_truncate_rag_tables():
    """源码层自检：`seed.py` 的**可执行代码**里不该出现任何 `rag_*` 表名。

    只看 AST 里参与运行的字符串常量（`ast.Constant`），跳过 docstring 与注释 ——
    否则「解释为什么不动向量库」的那几句注释会把这条例行检查弄红，
    而那种红是最没意义的一种红（它惩罚的正是"写清理由"这个好习惯）。
    """
    import ast
    import pathlib

    source = pathlib.Path("scripts/seed.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(id(doc))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node.value) in docstrings:
            continue
        if "rag_" in node.value:
            offenders.append(node.value)
    assert not offenders, (
        f"seed.py 的可执行代码里出现了向量表名：{offenders}。"
        "灌种子数据不该触碰向量库（理由见 _truncate_business_tables 的 docstring）。"
    )
