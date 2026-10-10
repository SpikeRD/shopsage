"""端到端用例（真库 + 假 embedding）：入库 → 检索 → 增量 → 删除 → 同步 → 工具适配。

这一组覆盖 `10.rag.md` 第 7 节的四条测试用例，并把「假 embedding 也能测」这件事用足：
向量腿返回的是**噪声近邻**，所以凡是「答对了」的结论都只能来自关键词腿与 RRF 融合 ——
这恰好把「混合检索有没有把关键词腿当回事」变成一个可判定的问题。

真模型 + 真语料的那一条在 `test_rag_live.py`（带 `--rag-live` 才跑）。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

POLICY = """# 退货与换货政策

第 3 条：自签收之日起 7 天内，商品完好可申请无理由退货。

第 7 条：生鲜类商品不支持无理由退货，已拆封的食品不予退换。

第 9 条：无理由退货的运费由买家承担；质量问题由本店承担。
"""

FAQ = """# 常见问题

### 如何修改收货地址？

在订单详情页点「修改地址」，仅未发货订单可改。

### 能否开具增值税专用发票？

可以，请联系客服并提交开票信息。
"""

POLICY_SOURCE = "docs/kb/退货政策.md"
FAQ_SOURCE = "docs/kb/常见问题.md"


# ── 10.rag.md §7 用例 1：入库后能检索到，且带溯源 ───────────────────────────


def test_ingest_then_retrieve(scratch_ingest):
    """`ingest_document()` → `hybrid_search()` → 命中的 `source` 正确。

    ⚠️ 断言用 `top_k=1` 并查「第 3 条」：假 embedding 下的排序**完全由 BM25 决定**，
    而 BM25 会惩罚长片段（长度归一化），所以「哪一条排第一」在这份三条款小语料里
    本来就不是业务上「最相关」的那条。这里钉的是**契约**：
    能召回、带溯源、章节正确、命中文本来自被问的那一条。
    """
    report = scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    assert report.action == "created"
    assert report.chunks_total >= 3

    retriever = scratch_ingest.retriever()
    result = asyncio.run(retriever.search("第 3 条 无理由退货", top_k=2))
    assert result.hits
    top = result.hits[0]
    assert top.source == POLICY_SOURCE
    assert top.section == "退货与换货政策 > 3 条"
    assert "自签收之日起 7 天" in top.text
    assert top.chunk_id.startswith("c")
    assert top.keyword_rank == 1  # 关键词腿把它排在第一


def test_retrieval_carries_section_and_citation(scratch_ingest):
    scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    retriever = scratch_ingest.retriever()
    result = asyncio.run(retriever.search("第 9 条 运费", top_k=3))
    top = result.hits[0]
    assert "运费" in top.text
    assert top.section == "退货与换货政策 > 9 条"
    assert top.citation() == f"{POLICY_SOURCE} #{top.chunk_id}"


def test_citation_present_for_every_hit(scratch_ingest):
    """`10.rag.md` §7 用例 4：每条结果的 `source` 必须非空。"""
    scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    scratch_ingest.ingest(FAQ_SOURCE, FAQ, category="faq")
    retriever = scratch_ingest.retriever()
    result = asyncio.run(retriever.search("如何修改地址", top_k=5))
    assert result.hits
    assert all(hit.source for hit in result.hits)
    assert all(hit.chunk_id for hit in result.hits)


def test_faq_document_keeps_question_with_answer(scratch_ingest):
    """FAQ 策略：命中的片段里同时有「问」与「答」（否则等于没答）。"""
    scratch_ingest.ingest(FAQ_SOURCE, FAQ, category="faq")
    retriever = scratch_ingest.retriever()
    result = asyncio.run(retriever.search("修改收货地址", top_k=2))
    top = result.hits[0]
    assert "收货地址" in top.text and "订单详情页" in top.text


def test_keyword_leg_rescues_an_exact_clause_number(scratch_ingest):
    """假 embedding 下「答对」只能来自关键词腿：这是混合检索价值的**构造性证据**。"""
    scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    scratch_ingest.ingest(FAQ_SOURCE, FAQ, category="faq")
    retriever = scratch_ingest.retriever()
    result = asyncio.run(retriever.search("第 7 条", top_k=2))
    top = result.hits[0]
    assert top.keyword_rank == 1  # 关键词腿把它排在第一位
    assert "生鲜" in top.text


# ── 10.rag.md §7 用例 2：增量更新 ───────────────────────────────────────────


def test_incremental_update_marks_old_chunks_inactive(scratch_ingest):
    """`reindex_document()`：旧 chunk 标 `is_active=False`，新 chunk 可检索到。"""
    first = scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    assert first.action == "created"

    changed = POLICY.replace("自签收之日起 7 天内", "自签收之日起 15 天内")
    report = scratch_ingest.reindex(POLICY_SOURCE, changed)

    assert report.action == "updated"
    assert report.version == 2
    assert report.added == 1 and report.deactivated == 1
    assert report.unchanged >= 2  # 只有第 3 条变了
    assert report.embedded == 1  # **只重嵌变化的片段**

    retriever = scratch_ingest.retriever()
    result = asyncio.run(retriever.search("15 天内 无理由退货", top_k=3))
    assert result.hits
    # 新内容可检索到（可能在 top-2 而不是 top-1：假 embedding 下排序全看 BM25）
    assert any("15 天" in hit.text for hit in result.hits[:2])

    # 旧片段仍在库里但不可检索（保留可引用 —— 10.rag.md §5）
    stale = asyncio.run(retriever.search("7 天 无理由退货", top_k=5))
    assert all("自签收之日起 7 天" not in hit.text for hit in stale.hits)

    with scratch_ingest.store.read_engine.connect() as conn:
        inactive = conn.execute(
            text(
                f"select count(*) from {scratch_ingest.store.chunks_table} "
                "where source = :source and not is_active"
            ),
            {"source": POLICY_SOURCE},
        ).scalar_one()
    assert inactive == 1


def test_reingest_without_changes_does_zero_embeddings(scratch_ingest):
    """F1 + E2 的核心证据：内容没变时**一次 embedding 都不花**。"""
    scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    scratch_ingest.embedded_batches.clear()

    again = scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    assert again.action == "unchanged"
    assert again.embedded == 0
    assert scratch_ingest.embedded_batches == []


def test_changed_document_embeds_only_the_changed_chunks(scratch_ingest):
    """改一条 → 只嵌那一条（实测：多段文档改 1 段只嵌 1 段）。"""
    scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    scratch_ingest.embedded_batches.clear()

    changed = POLICY.replace("已拆封的食品不予退换", "已拆封的食品同样支持退换")
    report = scratch_ingest.ingest(POLICY_SOURCE, changed, category="policy")

    assert report.action == "updated"
    assert report.embedded == 1
    embedded = scratch_ingest.embedded_chunks
    assert len(embedded) == 1
    assert "同样支持退换" in embedded[0]


def test_new_chunk_is_added_when_document_grows(scratch_ingest):
    scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    grown = POLICY + "\n第 11 条：签订年度框架协议的客户享受专属账期。\n"
    report = scratch_ingest.ingest(POLICY_SOURCE, grown, category="policy")
    assert report.added == 1 and report.updated == 0 and report.deactivated == 0

    retriever = scratch_ingest.retriever()
    result = asyncio.run(retriever.search("年度框架协议 账期", top_k=2))
    assert any("账期" in hit.text for hit in result.hits)


def test_removed_chunk_is_deactivated(scratch_ingest):
    """文档里删掉一段 → 对应片段失效（检索不可见、行仍在）。"""
    scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    trimmed = POLICY.replace("第 9 条：无理由退货的运费由买家承担；质量问题由本店承担。", "")
    report = scratch_ingest.ingest(POLICY_SOURCE, trimmed, category="policy")
    assert report.deactivated == 1
    assert report.added == 0  # 删一段不该让别的片段重嵌

    retriever = scratch_ingest.retriever()
    result = asyncio.run(retriever.search("无理由退货的运费由买家承担", top_k=5))
    assert all("运费由买家承担" not in hit.text for hit in result.hits)


# ── 删除与下线 ──────────────────────────────────────────────────────────────


def test_soft_delete_hides_but_keeps_rows(scratch_ingest):
    scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    counts = scratch_ingest.delete(POLICY_SOURCE, hard=False)
    assert counts.action == "deleted" and counts.deactivated >= 3

    retriever = scratch_ingest.retriever()
    result = asyncio.run(retriever.search("7天无理由退货", top_k=5))
    assert result.hits == []

    with scratch_ingest.store.read_engine.connect() as conn:
        remaining = conn.execute(
            text(f"select count(*) from {scratch_ingest.store.chunks_table}")
        ).scalar_one()
    assert remaining >= 3  # 行还在（引用不断）


def test_hard_delete_removes_rows(scratch_ingest):
    scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    scratch_ingest.delete(POLICY_SOURCE, hard=True)
    with scratch_ingest.store.read_engine.connect() as conn:
        remaining = conn.execute(
            text(f"select count(*) from {scratch_ingest.store.chunks_table}")
        ).scalar_one()
    assert remaining == 0


def test_delete_missing_document_is_a_noop_with_a_warning(scratch_ingest):
    report = scratch_ingest.delete("docs/kb/不存在.md", hard=False)
    assert report.action == "unchanged"
    assert report.warnings and "没有这份文档" in report.warnings[0]


# ── 同步（F1 的可见延迟）────────────────────────────────────────────────────


def test_sync_is_idempotent_and_prunes_deleted_files(
    local_tmp, scratch_store, offline_settings, fake_embedder
):
    """`RagSyncWorker.sync_once()`：第一轮入库、第二轮零变更、`prune` 下线消失的文件。"""
    from app.rag.sync import RagSyncWorker

    (local_tmp / "退货政策.md").write_text(POLICY, encoding="utf-8")
    (local_tmp / "常见问题.md").write_text(FAQ, encoding="utf-8")
    (local_tmp / "忽略我.bin").write_bytes(b"\x00\x01")  # 不支持的后缀：应被掠过

    worker = RagSyncWorker(
        local_tmp,
        store=scratch_store,
        settings=offline_settings,
        embedder=fake_embedder,
    )
    first = asyncio.run(worker.sync_once())
    assert first.scanned == 2
    assert first.changed == 2 and first.embedded > 0

    second = asyncio.run(worker.sync_once())
    assert second.changed == 0
    assert second.embedded == 0
    assert second.skipped == 2  # 指纹未变 → 连解析都不做

    (local_tmp / "常见问题.md").unlink()
    third = asyncio.run(worker.sync_once(prune=True))
    assert len(third.pruned) == 1 and third.pruned[0].endswith("常见问题.md")
    assert third.missing == third.pruned
    # 剩下那份文档的片段仍然可检索
    assert scratch_store.count_active_chunks() > 0


def test_sync_survives_a_broken_file(local_tmp, scratch_store, offline_settings, fake_embedder):
    """单个文件坏掉不该中断整轮同步（否则「同步器挂了」要很久才被发现）。"""
    from app.rag.sync import RagSyncWorker

    (local_tmp / "好文件.md").write_text(POLICY, encoding="utf-8")
    # 后缀是 .pdf 但内容不是 PDF：解析会抛错 → 应被跳过，而不是让整轮同步失败
    (local_tmp / "坏文件.pdf").write_bytes(b"%PDF-1.4 not really a pdf")

    worker = RagSyncWorker(
        local_tmp, store=scratch_store, settings=offline_settings, embedder=fake_embedder
    )
    report = asyncio.run(worker.sync_once())
    assert report.scanned == 2
    assert report.changed == 1  # 好文件入库了
    assert report.skipped == 1  # 坏文件被跳过（记在 skipped 里）


def test_sync_scan_documents_filters_unsupported_suffixes(local_tmp):
    from app.rag.sync import scan_documents

    (local_tmp / "a.md").write_text("x", encoding="utf-8")
    (local_tmp / "b.txt").write_text("x", encoding="utf-8")
    (local_tmp / "c.exe").write_bytes(b"x")
    names = [path.name for path in scan_documents(local_tmp)]
    assert names == ["a.md", "b.txt"]


# ── 与 `app/tools/retriever.py` 的接线 ─────────────────────────────────────


def test_tools_adapter_uses_the_real_backend(scratch_ingest):
    """第 6 步的工具适配层现在能拿到**真后端**了（此前它抛 `ToolNotAvailableError`）。"""
    import app.rag.retriever as module
    from app.tools.retriever import retrieve_docs

    scratch_ingest.ingest(POLICY_SOURCE, POLICY, category="policy")
    module.reset_default_retriever()
    module._DEFAULT = scratch_ingest.retriever()
    try:
        docs = retrieve_docs("7天无理由退货", top_k=3, caller="tests")
    finally:
        module.reset_default_retriever()

    assert docs, "工具适配层没有拿到检索结果"
    assert all(doc.source for doc in docs)
    assert all(doc.chunk_id for doc in docs)
    assert all(doc.source == POLICY_SOURCE for doc in docs)
    assert any("自签收之日起 7 天" in doc.text for doc in docs)
    # 适配层按 score 降序（第 6 步的契约），且 citation 可读
    assert docs[0].citation().startswith(POLICY_SOURCE)


def test_tools_adapter_reports_dropped_sourceless_hits(scratch_ingest, caplog):
    """适配层的硬约束在真链路上仍然生效：无 source 的命中被丢弃并 WARNING。

    这里用假 store 注入「带一条无源命中」的结果集，验证**适配层**的行为
    （`app/rag` 自己不会产出无源命中 —— 那是上游数据质量问题，见 6.tools-runbook）。
    """
    from app.tools.retriever import retrieve_docs

    class SourcelessStore:
        dimension = 1024

        def search_vector(self, embedding, *, top_k):
            from app.rag.store import VectorHit

            return [
                VectorHit(id=1, source="", chunk_id="", text="没有来源的片段", section=None, page=None, score=0.9),
                VectorHit(id=2, source="ok.md", chunk_id="c2", text="有来源的片段", section="s", page=None, score=0.8),
            ]

        def search_keyword(self, query, *, top_k, min_ratio, k1, b):
            return []

    import app.rag.retriever as module
    from app.rag.rerank import NoopReranker
    from app.rag.retriever import Retriever
    from app.rag.settings import RagSettings
    from tests.rag.conftest import make_fake_embedder

    module.reset_default_retriever()
    module._DEFAULT = Retriever(
        store=SourcelessStore(),  # type: ignore[arg-type]
        settings=RagSettings(vector_dim=1024, candidates=5, rerank="off"),
        embedder=make_fake_embedder(1024),
        reranker=NoopReranker(),
    )
    try:
        with caplog.at_level("WARNING", logger="app.tools.retriever"):
            docs = retrieve_docs("随便问", top_k=5, caller="tests")
    finally:
        module.reset_default_retriever()

    assert [doc.text for doc in docs] == ["有来源的片段"]
    assert any("溯源" in record.getMessage() for record in caplog.records)
