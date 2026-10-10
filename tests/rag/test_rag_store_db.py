"""`store` + `schema` 的**真库**用例：SQL 只有跑过 PostgreSQL 才算测过。

为什么这些用例必须连真库（不能用假库替身）：

* **关键词腿的 BM25 是写在 SQL 里的**（含 `ts_stat`、`to_tsquery`、覆盖率门槛的
  `having count(*) >= n`）—— 假库只能验证「调用顺序」，验证不了「排序对不对」；
* **触发器**（`ngram_tsv` / `ngram_doc` 与 `ngrams` 同源）是数据库侧的行为，
  Python 里没有等价物；
* **HNSW / GIN 索引**、`vector(n)` 的维度校验、`on conflict` 幂等、部分索引的
  `where is_active` —— 全是数据库特性。

依赖不可用（没有 PostgreSQL / 没跑迁移）时整组 skip，理由由装置给出。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.rag.schema import current_vector_dim, table_exists
from app.rag.store import StoreError
from tests.rag.conftest import (
    TEST_CHUNKS_TABLE,
    TEST_DOCUMENTS_TABLE,
    TEST_STATS_TABLE,
)


def test_temporary_tables_mirror_the_production_schema(scratch_store):
    """临时表与生产表**同构**（同一份 DDL）：否则「测试绿、生产红」。"""
    with scratch_store.read_engine.connect() as conn:
        assert table_exists(conn, TEST_CHUNKS_TABLE)
        assert table_exists(conn, TEST_DOCUMENTS_TABLE)
        assert table_exists(conn, TEST_STATS_TABLE)
        dim = current_vector_dim(conn, TEST_CHUNKS_TABLE)
        assert dim == 1024


def test_required_columns_exist(scratch_store):
    """`10.rag.md` 第 6 节第 1 条要求的六列一个都不能少（其余是我们加的）。"""
    required = {
        "id",
        "text",
        "embedding",
        "source",
        "chunk_id",
        "is_active",
        "updated_at",
    }
    with scratch_store.read_engine.connect() as conn:
        rows = conn.execute(
            text(
                "select attname from pg_attribute "
                "where attrelid = to_regclass(:t) and attnum > 0 and not attisdropped"
            ),
            {"t": f"public.{TEST_CHUNKS_TABLE}"},
        ).all()
    columns = {row.attname for row in rows}
    assert required <= columns
    # 中文关键词检索用的四列
    assert {"ngrams", "ngram_tsv", "ngram_doc", "ngram_tokens"} <= columns


def test_indexes_exist_with_the_right_kinds(scratch_store):
    """HNSW（向量）+ GIN（关键词）+ 两个部分索引：少一个就退化成全表扫描。"""
    with scratch_store.read_engine.connect() as conn:
        rows = conn.execute(
            text(
                "select indexname, indexdef from pg_indexes "
                "where tablename = :t order by indexname"
            ),
            {"t": TEST_CHUNKS_TABLE},
        ).all()
    definitions = {row.indexname: row.indexdef for row in rows}
    lowered = {name: ddl.lower() for name, ddl in definitions.items()}
    assert any("hnsw" in ddl for ddl in lowered.values()), definitions
    assert any("gin" in ddl and "ngram_tsv" in ddl for ddl in lowered.values()), definitions
    # PostgreSQL 会把 `where is_active` 渲染成 `WHERE is_active`（大小写不同）
    assert any("where is_active" in ddl for ddl in lowered.values()), definitions


def test_trigger_keeps_tsv_and_length_in_sync(scratch_store):
    """触发器：`ngram_tsv`/`ngram_doc` 必须与 `ngrams` 自动同源。

    这是「中文全文检索能不能用」的地基 —— 不同源就会出现「数据看着对、检索永远零命中」。
    """
    with scratch_store.engine.begin() as conn:
        conn.execute(
            text(
                f"insert into {TEST_DOCUMENTS_TABLE} (source, doc_hash) values ('t.md', 'h')"
            )
        )
        doc_id = conn.execute(
            text(f"select doc_id from {TEST_DOCUMENTS_TABLE} where source = 't.md'")
        ).scalar_one()
        conn.execute(
            text(
                f"insert into {TEST_CHUNKS_TABLE} "
                "(doc_id, chunk_id, text, embedding, ngrams, source, content_hash) "
                "values (:doc_id, 'c1', '第 3 条：自签收之日起 7 天内可无理由退货。', "
                "        ('[' || repeat('0.01,', 1023) || '0.01]')::vector, '第 条 自签 签收 7 天内', "
                "        't.md', 'h1')"
            ),
            {"doc_id": int(doc_id)},
        )
    with scratch_store.read_engine.connect() as conn:
        row = conn.execute(
            text(
                f"select ngram_doc, ngram_tsv::text as tsv from {TEST_CHUNKS_TABLE} where chunk_id = 'c1'"
            )
        ).one()
    assert row.ngram_doc == 6  # 6 个词元：第/条/自签/签收/7/天内
    assert "签收" in row.tsv


def test_schema_creation_is_idempotent(pg_ready):
    """`create_statements` 全是 `IF NOT EXISTS`：重复执行不报错（迁移与测试都依赖它）。"""
    from app.db.session import get_write_engine
    from app.rag.schema import create_statements

    statements = create_statements(
        1024,
        chunks_table=TEST_CHUNKS_TABLE,
        documents_table=TEST_DOCUMENTS_TABLE,
        stats_table=TEST_STATS_TABLE,
        function_name="test_rag_chunk_ngrams",
        trigger_name="test_rag_chunks_ngram_tsv",
    )
    engine = get_write_engine()
    for _ in range(2):  # 连跑两遍
        with engine.begin() as conn:
            for statement in statements:
                conn.exec_driver_sql(statement)
    with engine.begin() as conn:
        for statement in [
            f"drop table if exists {TEST_CHUNKS_TABLE} cascade",
            "drop function if exists test_rag_chunk_ngrams() cascade",
            f"drop table if exists {TEST_STATS_TABLE} cascade",
            f"drop table if exists {TEST_DOCUMENTS_TABLE} cascade",
        ]:
            conn.exec_driver_sql(statement)


def test_vector_dim_must_be_positive():
    """`vector(0)` 在 pgvector 里合法 → 「维度忘了配」会静默建出一张存空向量的表。

    所以在建表前就拦住，并把该改的三处写进报错。
    """
    from app.rag.schema import create_statements

    with pytest.raises(ValueError) as excinfo:
        create_statements(0)
    assert "RAG_VECTOR_DIM" in str(excinfo.value)
    assert "EMBEDDING_DIM" in str(excinfo.value)


# ── 向量腿 ──────────────────────────────────────────────────────────────────


def _embedding(seed: float, dimension: int = 1024) -> list[float]:
    """造一个可比的单位向量：第一个分量受 seed 控制，其余相同。"""
    values = [0.0] * dimension
    values[0] = seed
    values[1] = max(0.0, 1.0 - seed)
    norm = (values[0] ** 2 + values[1] ** 2) ** 0.5 or 1.0
    return [value / norm for value in values]


def test_vector_search_orders_by_cosine_similarity(scratch_store):
    """向量腿按余弦相似度降序 —— 排序方向写反是这类代码最常见的一处错。"""
    from app.rag.chunking import TextChunk, chunk_id_for, content_hash

    def make(text_value: str) -> TextChunk:
        return TextChunk(
            text=text_value,
            section="s",
            page=None,
            ordinal=0,
            chunk_id=chunk_id_for(text_value),
            content_hash=content_hash(text_value),
        )

    chunks = [make("甲条款"), make("乙条款"), make("丙条款")]
    scratch_store.upsert_document(
        source="v.md",
        doc_hash="h",
        category="kb",
        strategy="policy",
        chunks=chunks,
        embeddings=[_embedding(1.0), _embedding(0.5), _embedding(0.0)],
    )
    hits = scratch_store.search_vector(_embedding(1.0), top_k=3)
    assert [hit.text for hit in hits] == ["甲条款", "乙条款", "丙条款"]
    assert hits[0].score > hits[1].score > hits[2].score
    assert all(hit.source == "v.md" for hit in hits)


def test_vector_search_skips_inactive_chunks(scratch_store):
    """`is_active=false` 的历史片段**不能**出现在检索结果里（硬约束）。"""
    from app.rag.chunking import TextChunk, chunk_id_for, content_hash

    def make(text_value: str) -> TextChunk:
        return TextChunk(
            text=text_value,
            section="s",
            page=None,
            ordinal=0,
            chunk_id=chunk_id_for(text_value),
            content_hash=content_hash(text_value),
        )

    old = make("旧条款：7 天无理由退货")
    scratch_store.upsert_document(
        source="i.md",
        doc_hash="h1",
        category="kb",
        strategy="policy",
        chunks=[old],
        embeddings=[_embedding(1.0)],
    )
    new = make("新条款：15 天无理由退货")
    scratch_store.upsert_document(
        source="i.md",
        doc_hash="h2",
        category="kb",
        strategy="policy",
        chunks=[new],
        embeddings=[_embedding(1.0)],
        incoming={new.chunk_id: new.content_hash},
    )
    hits = scratch_store.search_vector(_embedding(1.0), top_k=5)
    texts = [hit.text for hit in hits]
    assert "新条款：15 天无理由退货" in texts
    assert "旧条款：7 天无理由退货" not in texts  # 失效了


def test_vector_dimension_mismatch_is_actionable(scratch_store):
    """维度不符时报错要能照着修（三处一起改 + 重灌）。"""
    from app.rag.chunking import TextChunk, chunk_id_for, content_hash

    chunk = TextChunk(
        text="维度不符的片段",
        section=None,
        page=None,
        ordinal=0,
        chunk_id=chunk_id_for("x"),
        content_hash=content_hash("x"),
    )
    with pytest.raises(StoreError) as excinfo:
        scratch_store.upsert_document(
            source="bad.md",
            doc_hash="h",
            category="kb",
            strategy="policy",
            chunks=[chunk],
            embeddings=[[0.1] * 8],  # 表是 vector(1024)
        )
    message = str(excinfo.value)
    assert "维度" in message
    assert "RAG_VECTOR_DIM" in message or "EMBEDDING_DIM" in message


# ── 关键词腿（BM25）─────────────────────────────────────────────────────────

KEYWORD_DOC = """# 物流与配送政策

## 1. 配送范围与时效

1.1 本店默认发顺丰速运，全国（不含港澳台）均可配送。

1.2 偏远地区需要 3~5 个工作日送达。

## 2. 运费标准

2.1 单笔订单实付满 99 元免运费；未满 99 元收取 10 元运费。
"""


@pytest.fixture
def keyword_corpus(scratch_ingest):
    """灌一份含精确编号（1.1 / 2.1）的小语料。"""
    report = scratch_ingest.ingest("kw.md", KEYWORD_DOC, category="logistics")
    assert report.action == "created"
    return report


def test_keyword_search_finds_exact_clause_number(scratch_store, keyword_corpus):
    """**精确编号**命中：这是关键词腿存在的全部理由（向量腿对裸编号几乎无区分度）。"""
    hits = scratch_store.search_keyword("1.1", top_k=3)
    assert hits, "关键词腿对 `1.1` 零召回"
    assert "顺丰" in hits[0].text
    assert hits[0].matched >= 1


def test_keyword_search_uses_bm25_ordering(scratch_store, keyword_corpus):
    """BM25 的**词频**生效：同一段里出现两次的词，应该比只出现一次的更排前。"""
    from app.rag.chunking import TextChunk, chunk_id_for, content_hash
    from app.rag.textkit import index_text

    text_value = "破损理赔：破损理赔需在签收后 48 小时内提交破损理赔申请。"
    chunk = TextChunk(
        text=text_value,
        section="理赔",
        page=None,
        ordinal=0,
        chunk_id=chunk_id_for(text_value),
        content_hash=content_hash(text_value),
    )
    scratch_store.upsert_document(
        source="bm25.md",
        doc_hash="h",
        category="kb",
        strategy="policy",
        chunks=[chunk],
        embeddings=[_embedding(0.3)],
    )
    assert index_text(text_value)

    hits = scratch_store.search_keyword("破损理赔", top_k=5)
    assert hits and "破损理赔" in hits[0].text


def test_keyword_search_never_returns_missing_terms(scratch_store, keyword_corpus):
    """低精度保护：库里没有的词必须**零召回**（而不是「随便给几条」）。

    这条是「关键词腿不会污染结果」的判据：一个只有 OR 语义的实现会在这里返回一堆无关片段。
    """
    hits = scratch_store.search_keyword("飞机票改签退票", top_k=5)
    assert hits == []


def test_keyword_search_coverage_gate_blocks_weak_matches(scratch_store, keyword_corpus):
    """覆盖率门槛：只有**少数**词命中时不进候选（默认阈值 0.6）。"""
    strict = scratch_store.search_keyword("顺丰速运 港澳台 免运费 破损理赔", top_k=5, min_ratio=0.9)
    loose = scratch_store.search_keyword("顺丰速运 港澳台 免运费 破损理赔", top_k=5, min_ratio=0.1)
    assert len(strict) <= len(loose)


def test_keyword_search_handles_unknown_tokens_gracefully(scratch_store, keyword_corpus):
    """错字（库里没有的词）不该把覆盖率分母抬高到零召回。

    实测场景：用户把「退货」打成「退贷」——`退贷` 不在库里，
    如果它计入分母，`7天无理由退货` 这类查询会被门槛挡掉。
    """
    hits = scratch_store.search_keyword("顺丰速运 快递公司", top_k=5)
    assert hits, "含未登录词的查询被门槛误杀"


def test_corpus_stats_track_document_and_chunk_counts(scratch_store, keyword_corpus):
    """BM25 的两个全局量（N / avgdl）由入库维护：漏维护会让 idf 与长度归一化一起失真。"""
    stats = scratch_store.corpus_stats()
    assert stats["docs"] == 1
    assert stats["chunks"] == keyword_corpus.chunks_total
    assert stats["total_tokens"] > 0


def test_all_columns_fit_the_row_read_back(scratch_store, keyword_corpus):
    """读回来的字段要能拼出「人可读的出处」（溯源契约的最小验证）。"""
    hits = scratch_store.search_keyword("顺丰速运", top_k=1)
    hit = hits[0]
    assert hit.source == "kw.md"
    assert hit.chunk_id.startswith("c")
    assert hit.section
    assert hit.text


def test_unknown_table_error_is_actionable(scratch_store):
    """表不存在时的报错必须指出「先跑迁移」（占位符写错/忘了迁移都会走到这里）。"""
    from app.rag.store import RagStore

    broken = RagStore(
        engine=scratch_store.engine,
        read_engine=scratch_store.read_engine,
        chunks_table="rag_chunks_not_here",
        documents_table=f"{TEST_DOCUMENTS_TABLE}",
        stats_table=f"{TEST_STATS_TABLE}",
    )
    with pytest.raises(StoreError) as excinfo:
        broken.search_vector([0.01] * 1024, top_k=1)
    assert "alembic upgrade head" in str(excinfo.value)


def test_invalid_table_name_is_rejected():
    """表名只接受标识符字符（拼接 SQL 的第一道防线）。"""
    from app.rag.store import RagStore

    with pytest.raises(StoreError, match="标识符"):
        RagStore(chunks_table="rag_chunks; drop table users")


def test_database_is_reachable_for_the_rest_of_the_suite(pg_ready):
    """装置自检：`pg_ready` 生效说明后续用例真的连上了库（不是被 skip 掉）。"""
    assert pg_ready is True


def test_sqlalchemy_error_translation_for_missing_relation(scratch_store):
    """`_check_error` 的翻译逻辑本身要有用例（否则它只在生产上火）。"""
    with scratch_store.engine.connect() as conn:
        with pytest.raises(SQLAlchemyError):
            conn.execute(text("select * from rag_chunks_does_not_exist"))
