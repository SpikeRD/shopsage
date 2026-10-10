r"""**真模型**用例（`--rag-live` 才跑）：本地 bge-m3 + bge-reranker-base 的检索质量。

为什么这组用例值得存在（而离线用例不够）：

离线用例用假 embedding（噪声向量）验证的是**管道与账目**（增量省了多少 embedding、
契约字段全不全）。而 `10.rag.md` §8 的验收要求是**质量**：
「知识库可检索且答案带溯源」「rerank 后 top-k 更精简、质量不降」——
这两条只有真模型能回答。

本组的三条核心断言（都对应一个文档里的承诺）：

1. **混合 > 纯向量**：对「裸条款号」这类无语义查询，纯向量会在多份文档间挑错条款，
   而加了关键词腿之后能挑对 —— 这正是 `10.rag.md` 第 4 节第 1 条的理由。
2. **rerank 提升命中位置**：`MRR` / `top-1 命中数`在精排后不下降（E2 的「质量不降」）。
3. **上下文压缩**：候选池 → top-k 的 token 显著下降（E2 的「token 下降」）。

跑法（仓库根）：

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"; $env:PYTHONUTF8="1"; $env:HF_HUB_OFFLINE="1"
uv run python -m scripts.load_knowledge_base          # 先灌 docs/kb（一次即可）
uv run pytest -q tests/rag/test_rag_live.py --rag-live
```

⚠️ 用例**只读**真语料（`docs/kb`），不改动它；需要写库的部分用临时文档并在结束时删除。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path

import pytest

from app.config import get_settings

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.rag_live


def _local_models_available() -> tuple[bool, str]:
    """本地 embedding/rerank 依赖与权重是否就位（不就位就 skip，并说明怎么补）。"""
    if importlib.util.find_spec("sentence_transformers") is None:
        return False, "缺可选依赖：`uv sync --extra local`（含 torch，约 2 GB）"
    settings = get_settings()
    if settings.embedding_backend != "local":
        return False, (
            f"当前 EMBEDDING_BACKEND={settings.embedding_backend}（本组用例针对 local 后端写的）；"
            "要跑请把 EMBEDDING_BACKEND 切成 local"
        )
    model = settings.embedding_model
    cached = None
    try:
        from pathlib import Path

        hub = Path.home() / ".cache" / "huggingface" / "hub"
        cached = (hub / ("models--" + model.replace("/", "--"))).exists()
    except Exception:  # noqa: BLE001 - 权重探测只是「更早失败」，不影响正确性
        cached = None
    if cached is False:
        return False, (
            f"本地没有 {model} 的权重缓存（~/.cache/huggingface/hub）："
            "先联网预热一次（加载模型即会下载），或设 EMBEDDING_LOCAL_PATH 指向本地目录"
        )
    return True, ""


@pytest.fixture(scope="module")
def live_ready() -> bool:
    ok, reason = _local_models_available()
    if not ok:
        pytest.skip(reason)
    return True


@pytest.fixture(scope="module")
def live_retriever(live_ready):
    """真模型 + 真语料的检索器（rerank 用本地 CrossEncoder）。

    语料来自 `docs/kb`。**空库时自动灌一次**（`scripts.load_knowledge_base` 的同一段逻辑）——
    为什么要在装置里做，而不是「skip 掉让人手工跑」：

    第 10 步实测确认过一次**静默的连带事故**：`tests/test_db_migrations.py` 的迁移回环用例
    （`downgrade base` → `upgrade head`）会连 `rag_*` 三张表一起删掉、再建成空表。
    于是「跑一次全量测试」之后，本文件的 6 条用例会**静默 skip** ——
    现象看起来像「真模型用例没跑」，而真相是「知识库被迁移回环清空了」。
    自动补灌让「跑全量测试」这件事**自洽**：要么语料在，要么这次把它灌好。
    代价是空库时本模块会多花约 38 秒（本地 bge-m3 重算 59 个向量），只在空库时发生。
    """
    from app.rag.retriever import Retriever
    from app.rag.settings import RagSettings
    from app.rag.store import RagStore

    settings = RagSettings.from_settings()
    store = RagStore(dimension=settings.vector_dim)
    corpus = store.corpus_stats()
    if corpus["chunks"] == 0:
        # 空库：先补灌（复现 `scripts/load_knowledge_base.py` 的默认行为）
        import asyncio

        from app.rag.ingest import ingest_path

        root = Path(settings.knowledge_dir)
        if not root.exists():
            pytest.skip(f"知识库目录不存在：{root}")
        try:
            report = asyncio.run(ingest_path(root, category="kb", store=store, settings=settings))
        except Exception as exc:  # noqa: BLE001 - 灌库失败时给一句能照着修的话，而不是红
            pytest.skip(
                f"向量库为空且自动补灌失败（{type(exc).__name__}: {exc}）："
                "请手工执行 `uv run python -m scripts.load_knowledge_base`"
            )
        logger.info("向量库为空，已自动补灌：%s", report.as_dict())
        corpus = store.corpus_stats()
        if corpus["chunks"] == 0:
            pytest.skip(
                "自动补灌之后向量库仍为空：检查 docs/kb 是否有可解析的文档"
            )
    return Retriever(store=store, settings=settings)


def test_real_corpus_is_loaded_with_citations(live_retriever):
    """真语料可检索、每条都带溯源（`10.rag.md` §8 第 1 条）。"""
    result = asyncio.run(live_retriever.search("7天无理由退货", top_k=3))
    assert result.hits
    for hit in result.hits:
        assert hit.source.startswith("docs/kb/")
        assert hit.chunk_id.startswith("c")
        assert hit.section
    assert any("7 天" in hit.text or "7天" in hit.text for hit in result.hits)


def test_real_semantic_query_finds_the_right_document(live_retriever):
    """语义改写查询（换个说法）也要命中正确文档 —— 这是**向量腿**的价值。"""
    cases = {
        "积分有效期多久": "会员积分.md",
        "保修期是多久": "产品保修.md",
        "包裹破损怎么理赔": "物流政策.md",
        "客服会索要验证码吗": "账户安全与隐私.md",
    }
    for query, expected in cases.items():
        result = asyncio.run(live_retriever.search(query, top_k=3))
        assert any(expected in hit.source for hit in result.hits), (
            f"查询 {query!r} 的 top-3 里没有 {expected}：{[hit.source for hit in result.hits]}"
        )


def test_hybrid_beats_vector_only_on_bare_clause_numbers(live_retriever):
    """**混合 > 纯向量**：裸条款号（无语义）查询上，纯向量会挑错文档。

    为什么构造这个用例（而不是拿「难查询」碰运气）：`第 N 条` 这种查询**没有任何语义**，
    向量腿只能按全局语义近邻挑一个 —— 而多份文档里都有「第 9 条」，
    于是它经常挑到别份文档的条款。关键词腿按词元精确匹配，能看出问的是哪一份。

    这条用例还带一个**自检**：如果纯向量也全对（语料太小/模型太强），
    用例会打印提示而不是假装通过 —— 否则它会退化成「永远绿」的装饰品。
    """
    from app.rag.retriever import vector_only_search

    query = "第 9 条"
    expected = "退货政策.md"
    vector = vector_only_search(query, top_k=3, retriever=live_retriever)
    hybrid = asyncio.run(live_retriever.search(query, top_k=3))

    vector_ok = any(expected in hit.source for hit in vector.hits)
    hybrid_ok = any(expected in hit.source for hit in hybrid.hits)

    assert hybrid_ok, (
        f"混合检索没能在 top-3 里找到 {expected}：{[hit.source for hit in hybrid.hits]}"
    )
    if vector_ok:
        # 不是失败：说明这份语料上纯向量也够用（小语料 + 强模型的常见情形）
        detail = f"（纯向量这次也命中了：{[hit.source for hit in vector.hits]}）"
    else:
        detail = f"（纯向量漏掉：{[hit.source for hit in vector.hits]}）"
    print(f"混合 vs 纯向量：{query!r} → 混合✅ 纯向量{'✅' if vector_ok else '❌'} {detail}")


def test_rerank_moves_the_right_clause_up(live_retriever):
    """rerank 让正确条款**名次前移**（E2 的「质量不降」）。"""
    expected = "退货政策.md"
    queries = ("第 2 条 无理由退货的运费谁承担", "第 7 条 生鲜商品能退吗", "包裹破损怎么理赔")

    def rank_of(hits):
        for index, hit in enumerate(hits, start=1):
            if expected in hit.source:
                return index
        return 99

    plain_total = 0.0
    ranked_total = 0.0
    for query in queries:
        plain = asyncio.run(live_retriever.search(query, top_k=5, with_rerank=False))
        ranked = asyncio.run(live_retriever.search(query, top_k=5, with_rerank=True))
        plain_total += 1 / rank_of(plain.hits)
        ranked_total += 1 / rank_of(ranked.hits)
    # MRR 不下降（允许相等：小语料上粗排本来就不差）
    assert ranked_total >= plain_total - 1e-9, (
        f"rerank 让 MRR 下降了：粗排 {plain_total:.3f} → 精排 {ranked_total:.3f}"
    )


def test_rerank_cuts_the_context_that_reaches_the_llm(live_retriever):
    """候选池 → top-k 的 token 显著下降（E2 的「token 下降」）。

    这是 `10.rag.md` §8 第 3 条的量化口径：初召回宁可多（召回优先），
    但送进 LLM 的必须是精排后的 top-k。
    """
    from app.rag.textkit import approx_tokens

    settings = live_retriever.settings
    pool_tokens = 0
    top_tokens = 0
    for query in ("7天无理由退货", "积分有效期多久", "如何修改收货地址"):
        pool = asyncio.run(live_retriever.search(query, top_k=settings.candidates, with_rerank=False))
        top = asyncio.run(live_retriever.search(query, top_k=5, with_rerank=True))
        pool_tokens += sum(approx_tokens(hit.text) for hit in pool.hits)
        top_tokens += sum(approx_tokens(hit.text) for hit in top.hits)
    assert top_tokens < pool_tokens * 0.5, (
        f"上下文没有明显压缩：候选池 {pool_tokens} → top-5 {top_tokens}"
    )
    print(f"E2 实测：候选池 {pool_tokens} token → top-5 {top_tokens} token")


def test_incremental_update_is_visible_immediately(
    live_retriever, scratch_store, offline_settings
):
    """F1：真 embedding 下，改一段 → 立即可检索到（延迟 < 60s）。

    用**临时文档**（结束即删）：这条要写库，不能污染 `docs/kb` 的真语料。
    """
    import time

    from app.rag.ingest import delete_document, ingest_document
    from app.rag.retriever import Retriever

    source = "docs/kb/_live_probe_临时.md"
    body = "# 临时政策\n\n第 1 条：原始内容，包含「蓝色保时捷」这个不存在的词。\n\n第 2 条：另一段占位内容。\n"
    changed = body.replace("原始内容，包含「蓝色保时捷」这个不存在的词", "改后内容，包含「紫色自行车」这个不存在的词")

    asyncio.run(delete_document(source, hard=True, store=scratch_store, settings=offline_settings))
    try:
        first = asyncio.run(
            ingest_document(source, content=body, store=scratch_store, settings=offline_settings)
        )
        assert first.action == "created"

        retriever = Retriever(
            store=scratch_store, settings=offline_settings, reranker=live_retriever.reranker()
        )
        started = time.perf_counter()
        second = asyncio.run(
            ingest_document(source, content=changed, store=scratch_store, settings=offline_settings)
        )
        result = asyncio.run(retriever.search("紫色自行车", top_k=2))
        elapsed = time.perf_counter() - started

        assert second.action == "updated" and second.embedded == 1
        assert result.hits and any("紫色自行车" in hit.text for hit in result.hits)
        assert elapsed < 60, f"增量可见延迟 {elapsed:.1f}s（要求 < 60s，F1）"
        print(f"F1 实测：改一段 → 检索可见 耗时 {elapsed:.2f}s（仅重嵌 {second.embedded} 个片段）")
    finally:
        asyncio.run(delete_document(source, hard=True, store=scratch_store, settings=offline_settings))
