"""`retriever` 的离线用例：两条腿的编排、RRF 融合、rerank、降级、契约。

为什么这些用例用**假 store** 而不是临时真表：
它们验证的是「**怎么融合、怎么重排、怎么降级**」——纯算法与流程，
用假 store 可以把「哪一路排第几」钉死；用真库反而要经过 embedding 与 BM25 的间接影响，
失败时看不出是算法错还是数据错。真库路径由 `test_rag_integration_db.py` 覆盖。

⚠️ 本文件**从头到尾不加载模型**（`rerank="off"` 或注入假 reranker）：
默认的本地 CrossEncoder 加载一次要十几秒，把它放进单元测试会让整个套件变慢一个数量级
—— 加载真模型的验证放在 `test_rag_live.py`。
"""

from __future__ import annotations

import asyncio

import pytest

from app.rag.rerank import NoopReranker, RerankItem, RerankUnavailable, build_reranker
from app.rag.retriever import (
    RetrievalHit,
    Retriever,
    reciprocal_rank_fusion,
    reset_default_retriever,
)
from app.rag.settings import RagSettings
from app.rag.store import KeywordHit, VectorHit
from tests.rag.conftest import make_fake_embedder


def vector_hit(doc_id: int, text: str, score: float = 0.9, source: str = "doc.md") -> VectorHit:
    return VectorHit(
        id=doc_id,
        source=source,
        chunk_id=f"c{doc_id}",
        text=text,
        section=f"s{doc_id}",
        page=None,
        score=score,
    )


def keyword_hit(doc_id: int, text: str, score: float = 5.0) -> KeywordHit:
    return KeywordHit(
        id=doc_id,
        source="doc.md",
        chunk_id=f"c{doc_id}",
        text=text,
        section=f"s{doc_id}",
        page=None,
        score=score,
        matched=2,
    )


class FakeStore:
    """可控的假 store：向量腿与关键词腿各自返回什么，由用例指定。

    它还记录**调用参数**（`calls`）——「候选池大小有没有传对」这类问题
    只能靠参数断言（返回值看不出）。这与 `tests/test_tools_retriever.py` 里
    假检索后端记录 `top_k` 是同一个手法。
    """

    def __init__(self, vector=None, keyword=None, dimension: int = 1024) -> None:
        self._vector = list(vector or [])
        self._keyword = list(keyword or [])
        self.dimension = dimension
        self.calls: list[dict] = []

    def search_vector(self, embedding, *, top_k):
        self.calls.append({"leg": "vector", "top_k": top_k, "dim": len(embedding)})
        return self._vector[:top_k]

    def search_keyword(self, query, *, top_k, min_ratio, k1, b):
        self.calls.append({"leg": "keyword", "top_k": top_k, "min_ratio": min_ratio, "q": query})
        return self._keyword[:top_k]


class EchoEmbedder:
    """按文本返回向量的假 embedder；记录收到的（带前缀的）查询。"""

    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension
        self.seen: list[str] = []
        self._inner = make_fake_embedder(dimension)

    async def __call__(self, texts, **kwargs):
        self.seen.extend(texts)
        return await self._inner(texts)


def build(
    *,
    vector=None,
    keyword=None,
    reranker=None,
    settings: RagSettings | None = None,
    embedder=None,
) -> tuple[Retriever, FakeStore]:
    store = FakeStore(vector=vector, keyword=keyword)
    resolved = settings or RagSettings(
        vector_dim=1024, candidates=15, rerank="off" if reranker is None else "local", max_top_k=20
    )
    retriever = Retriever(
        store=store,  # type: ignore[arg-type]
        settings=resolved,
        embedder=embedder or EchoEmbedder(),
        reranker=reranker or NoopReranker(),
    )
    return retriever, store


# ── RRF 本身 ────────────────────────────────────────────────────────────────


def test_rrf_formula_adds_reciprocal_ranks():
    """`1/(k + rank)`，rank 从 1 开始；两路都命中时**累加**。"""
    scores = reciprocal_rank_fusion([[7, 9], [9, 11]], k=60)
    assert scores[7] == pytest.approx(1 / 61)
    assert scores[9] == pytest.approx(1 / 62 + 1 / 61)
    assert scores[11] == pytest.approx(1 / 62)
    assert scores[9] > scores[7]  # 两路都命中 → 排第一


def test_rrf_is_scale_invariant():
    """RRF 只看名次：BM25 分数从 5 变成 5000（量纲完全不同）不改变融合结果。

    这正是选 RRF 而不是「加权求和」的理由（加权求和要先归一化，
    而归一化会把绝对分差抹平，权重就变成尺度的函数）。
    """
    first = reciprocal_rank_fusion([[1, 2], [2, 3]])
    second = reciprocal_rank_fusion([[100, 200], [200, 300]])  # 只有 id 变了
    assert list(first.values()) == pytest.approx(list(second.values()))


def test_rrf_orders_ties_by_id_for_stability():
    """同分按 id 升序：同分乱序会让测试随机红、也让「为什么它第一」无法解释。"""
    retriever, _ = build()
    order = retriever.fuse([], [])[0]
    assert order == []
    order, scores, _, _ = retriever.fuse(
        [vector_hit(5, "a"), vector_hit(3, "b")],
        [keyword_hit(3, "b"), keyword_hit(5, "a")],
    )
    # 两条都被两路命中，且名次相同 → 融合分相同 → 按 id 升序
    assert order == [3, 5]
    assert scores[3] == scores[5]


# ── 两条腿的编排 ────────────────────────────────────────────────────────────


def test_search_queries_both_legs_with_the_same_candidate_pool():
    """两条腿用**同一个候选池大小**（`RAG_CANDIDATES`）——不对称会让某一路被截断。"""
    retriever, store = build(vector=[vector_hit(1, "甲")], keyword=[keyword_hit(1, "甲")])
    asyncio.run(retriever.search("退货", top_k=5))
    legs = {call["leg"]: call for call in store.calls}
    assert legs["vector"]["top_k"] == 15
    assert legs["keyword"]["top_k"] == 15
    assert legs["vector"]["dim"] == 1024  # 查询向量维度传对了


def test_top_k_is_clamped_to_max_top_k():
    """`top_k` 夹到 `RAG_MAX_TOP_K`（LLM 要「Top 500」时由它兜底，E2）。"""
    settings = RagSettings(vector_dim=1024, candidates=30, rerank="off", max_top_k=3)
    retriever, _ = build(
        vector=[vector_hit(index, f"片段{index}") for index in range(1, 11)],
        settings=settings,
    )
    result = asyncio.run(retriever.search("退货", top_k=500))
    assert len(result.hits) == 3


def test_invalid_arguments_raise_value_error():
    retriever, _ = build()
    with pytest.raises(ValueError, match="query"):
        asyncio.run(retriever.search("   ", top_k=3))
    with pytest.raises(ValueError, match="top_k"):
        asyncio.run(retriever.search("退货", top_k=0))
    with pytest.raises(ValueError, match="top_k"):
        asyncio.run(retriever.search("退货", top_k=True))


def test_empty_corpus_returns_empty_result_not_error():
    """空知识库返回空结果（上层据此回答「没有可引用的资料」），不是抛错。"""
    retriever, _ = build()
    result = asyncio.run(retriever.search("退货", top_k=5))
    assert result.hits == []
    assert result.vector_hits == 0 and result.keyword_hits == 0


def test_every_hit_carries_source_and_chunk_id():
    """**溯源是硬约束**：没有 `source` 的命中不许出现在结果里。"""
    retriever, _ = build(
        vector=[vector_hit(1, "第 1 条：7 天无理由退货。"), vector_hit(2, "第 2 条：运费由买家承担。")],
        keyword=[keyword_hit(1, "第 1 条：7 天无理由退货。")],
    )
    result = asyncio.run(retriever.search("7天无理由退货", top_k=5))
    assert result.hits
    assert all(hit.source for hit in result.hits)
    assert all(hit.chunk_id for hit in result.hits)
    assert result.hits[0].citation().startswith("doc.md")


def test_ranks_are_recorded_for_explainability():
    """融合分与两路名次都要留在结果里：回答「它为什么排第一」靠这些字段。"""
    retriever, _ = build(
        vector=[vector_hit(1, "甲"), vector_hit(2, "乙")],
        keyword=[keyword_hit(2, "乙"), keyword_hit(1, "甲")],
    )
    result = asyncio.run(retriever.search("甲", top_k=2))
    first = result.hits[0]
    assert first.vector_rank == 1 and first.keyword_rank == 2  # 向量腿第 1、关键词腿第 2
    assert first.fusion_score > 0
    assert first.extra["tokens"] > 0
    assert first.as_dict()["fusion_score"] == first.fusion_score


def test_min_score_filters_by_fusion_score():
    """`min_score` 是**融合分**下限：两路都没进前列的条目分数自然低。"""
    retriever, _ = build(
        vector=[vector_hit(1, "高"), vector_hit(2, "低")],
        keyword=[keyword_hit(1, "高")],
    )
    result = asyncio.run(retriever.search("高", top_k=5, min_score=0.02))
    assert [hit.text for hit in result.hits] == ["高"]


# ── rerank 与降级 ───────────────────────────────────────────────────────────


class RecordingReranker:
    """按给定分数表重排的假 reranker（顺序无关：按文本查分）。"""

    name = "fake"

    def __init__(self, scores: dict[str, float], *, fail: bool = False) -> None:
        self.scores = scores
        self.fail = fail
        self.calls: list[list[str]] = []

    def score(self, query: str, items):
        self.calls.append([item.text for item in items])
        if self.fail:
            raise RerankUnavailable("模拟后端不可用")
        return [self.scores.get(item.text, 0.0) for item in items]


def test_rerank_reorders_by_cross_encoder_score():
    """精排按分数降序：把「向量腿第 2、关键词腿第 1」的那条推到第一位。"""
    reranker = RecordingReranker({"长条款": 0.1, "精确条款": 0.9})
    retriever, _ = build(
        vector=[vector_hit(1, "长条款"), vector_hit(2, "精确条款")],
        keyword=[keyword_hit(2, "精确条款")],
        reranker=reranker,
    )
    result = asyncio.run(retriever.search("精确条款", top_k=2))
    assert [hit.text for hit in result.hits] == ["精确条款", "长条款"]
    assert result.hits[0].reranked is True
    assert result.hits[0].rerank_score == pytest.approx(0.9)
    assert result.rerank_degraded is False


def test_rerank_only_scores_a_bounded_head():
    """精排只打「前 `top_k * 2` 条」：全池精排会让检索比 LLM 还慢。"""
    reranker = RecordingReranker({})
    retriever, _ = build(
        vector=[vector_hit(index, f"片段{index}") for index in range(1, 21)],
        reranker=reranker,
    )
    asyncio.run(retriever.search("片段", top_k=3))
    assert len(reranker.calls) == 1
    assert len(reranker.calls[0]) == 6  # 3 * 2


def test_rerank_failure_degrades_to_fusion_order_with_a_warning(caplog):
    """**显式降级**：rerank 不可用时退回融合排序 + WARNING（而不是让检索整体失败）。

    没有这条日志，「rerank 坏了三周没人发现」会成为评测掉分时最难查的一类问题。
    """
    reranker = RecordingReranker({}, fail=True)
    retriever, _ = build(
        vector=[vector_hit(1, "甲"), vector_hit(2, "乙")],
        keyword=[keyword_hit(2, "乙")],
        reranker=reranker,
    )
    with caplog.at_level("WARNING", logger="app.rag.retriever"):
        result = asyncio.run(retriever.search("乙", top_k=2))
    assert result.rerank_degraded is True
    assert result.hits  # 仍然有结果
    assert any("rerank 不可用" in record.getMessage() for record in caplog.records)
    assert all(hit.reranked is False for hit in result.hits)


def test_with_rerank_false_is_the_control_group():
    """`with_rerank=False` 用于对照实验（证明 rerank 真的有增益）。"""
    reranker = RecordingReranker({"甲": 0.0, "乙": 1.0})
    retriever, _ = build(
        vector=[vector_hit(1, "甲"), vector_hit(2, "乙")],
        keyword=[keyword_hit(1, "甲")],
        reranker=reranker,
    )
    plain = asyncio.run(retriever.search("甲", top_k=2, with_rerank=False))
    assert plain.rerank_mode == "off"
    assert reranker.calls == []  # 对照组不调用 reranker
    assert [hit.text for hit in plain.hits][0] == "甲"  # 按融合分


def test_noop_reranker_returns_zero_scores_not_input_order():
    """`off` 后端返回全 0：调用方要能区分「打了低分」与「压根没 rerank」。"""
    assert NoopReranker().score("q", [RerankItem(key=1, text="a")]) == [0.0]


def test_build_reranker_switches_backends():
    assert build_reranker("off", model="x").name == "off"
    assert build_reranker("", model="x").name == "off"
    assert build_reranker("nonsense", model="x").name == "off"  # 不认识 → 退回 off（并告警）
    local = build_reranker("local", model="BAAI/bge-reranker-base")
    assert local.name.startswith("local:")
    api = build_reranker("api", model="jina-reranker-v2", api_base="https://api.example.com/v1")
    assert api.name.startswith("api:")


def test_api_reranker_requires_a_base_url():
    with pytest.raises(RerankUnavailable, match="RAG_RERANK_API_BASE"):
        build_reranker("api", model="m", api_base="")


# ── bge 查询前缀（`5.llm-runbook.md` §7.7）─────────────────────────────────


def test_bge_query_prefix_is_added_on_the_query_side_only():
    """bge 系必须加查询前缀，且**只在查询侧**加（文档侧加了会掉分）。

    这里验证的是「查询侧确实带上了前缀」——文档侧不带前缀由
    `test_rag_integration_db.py` 与 `test_rag_live.py` 的入库路径保证
    （入库直接用 `embed(chunk.text)`，没有任何前缀逻辑）。
    """
    embedder = EchoEmbedder()
    retriever, _ = build(embedder=embedder)
    retriever.embedding_model = "BAAI/bge-m3"
    asyncio.run(retriever.embed_query("退货政策"))
    assert embedder.seen == ["为这个句子生成表示以用于检索相关文章：退货政策"]


def test_non_bge_model_gets_no_prefix():
    """非 bge 模型不加前缀：给 API 模型塞无用前缀是要花钱的。"""
    embedder = EchoEmbedder()
    retriever, _ = build(embedder=embedder)
    retriever.embedding_model = "openai/text-embedding-3-small"
    asyncio.run(retriever.embed_query("退货政策"))
    assert embedder.seen == ["退货政策"]


# ── 同步/异步入口与单例 ─────────────────────────────────────────────────────


def test_search_sync_works_outside_a_loop_but_refuses_inside():
    """同步入口不能在事件循环里调用 —— 报错要能照着修（而不是裸 RuntimeError）。"""
    from app.rag.retriever import hybrid_search

    retriever, _ = build(vector=[vector_hit(1, "甲")])
    reset_default_retriever()
    import app.rag.retriever as module

    module._DEFAULT = retriever
    result = hybrid_search("甲", top_k=1)
    assert len(result.hits) == 1

    async def _call_inside_loop():
        hybrid_search("甲", top_k=1)

    with pytest.raises(RuntimeError, match="事件循环"):
        asyncio.run(_call_inside_loop())
    reset_default_retriever()


def test_default_retriever_is_lazy_singleton():
    """默认检索器是惰性单例：rerank/embedding 的模型加载（十几秒）不能每请求做一次。"""
    import app.rag.retriever as module

    reset_default_retriever()
    first = module.default_retriever()
    second = module.default_retriever()
    assert first is second
    reset_default_retriever()
    assert module.default_retriever() is not first
    reset_default_retriever()


def test_result_supports_iteration_and_indexing():
    """`RetrievalResult` 能迭代、能按索引取、能取 `sources`（上层适配层的便利）。"""
    retriever, _ = build(vector=[vector_hit(1, "甲")], keyword=[keyword_hit(1, "甲")])
    result = asyncio.run(retriever.search("甲", top_k=1))
    assert len(result) == 1
    assert isinstance(result[0], RetrievalHit)
    assert [hit.text for hit in result] == ["甲"]
    assert result.sources == ["doc.md"]
    assert result.as_dict()["query"] == "甲"
