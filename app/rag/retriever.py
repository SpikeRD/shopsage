"""混合检索：**向量腿 + 关键词腿 → RRF 融合 → rerank → 溯源的 top-k**（本步的门面）。

```
query ──┬─→ embed()  ──→ pgvector ANN（HNSW）      ─┐
        │        （语义相近）                        ├─→ RRF 融合 ─→ rerank（交叉编码器） ─→ top-k（带溯源）
        └─→ n-gram 切词 ─→ BM25（SQL 内手算）        ─┘        （1/(k+rank)）   （只留精华，省 token：E2）
                          （精确词/条款号/编号）
```

为什么要两条腿（`10.rag.md` 第 4 节第 1 条）：向量检索对「换个说法」很好，
对「**精确词/编号/政策条款号**」很差 —— 「第 7 条」与「第 3 条」在向量空间里几乎一样近
（实测：bge-m3 下两句的余弦相似度 0.97+），但业务上它们是两条**完全不同**的政策。
关键词腿正是为这类「一个字都不能错」的查询准备的；两条腿互补，融合后才稳定。

为什么用 RRF 而不是「把两路的分数加权求和」：两路的分数**量纲不同**
（余弦相似度 ∈ [-1,1]，BM25 是无界正数），加权求和必须先归一化，
而归一化会让「绝对分数」消失 —— 一条 BM25 = 12 的命中与一条 = 1.5 的命中被压到 0.98 / 0.12，
再乘权重，权重就变成了「尺度的函数」。RRF 只用**名次**（`1/(k + rank)`），
天然对两路的尺度免疫 —— 这是它被广泛采用的唯一理由，且它不需要调参（k=60 是经验值）。
代价是丢掉了「分差」信息（第一名 0.99 与第二名 0.20 的差距不再体现），
这个代价由 **rerank** 补回来：精排是在候选集内部做逐对打分，本来就不依赖初召回的分差。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.llm import embed as default_embed
from app.rag.rerank import RerankItem, RerankUnavailable, Reranker, build_reranker
from app.rag.settings import RagSettings
from app.rag.store import KeywordHit, RagStore, VectorHit
from app.rag.textkit import approx_tokens

logger = logging.getLogger(__name__)

# 查询侧前缀（`5.llm-runbook.md` §7.7）：**bge 系必须加**，且只在「查询侧」加。
# 为什么只加在查询侧：bge 的训练方式是「查询带指令、文档不带」，
# 两边都加等于把指令当成文档内容的一部分，实测会掉分（而且掉得很隐蔽：检索还能用，只是变差）。
# 不匹配的模型（openai/local 非 bge）不加前缀，避免给 API 模型塞无用 token（那要花钱）。
QUERY_PREFIXES: Final[dict[str, str]] = {
    "bge-m3": "为这个句子生成表示以用于检索相关文章：",
    "bge-large-zh": "为这个句子生成表示以用于检索相关文章：",
    "bge-base-zh": "为这个句子生成表示以用于检索相关文章：",
    "bge-small-zh": "为这个句子生成表示以用于检索相关文章：",
}

# 融合时进池的条数上限 = candidates（初召回）。keyword 与 vector 各取这么多。
FUSION_LEG_LIMIT: Final[int] = 50


@dataclass(frozen=True)
class RetrievalHit:
    """一条带溯源的检索结果（`10.rag.md` 第 2 节约定的四个字段 + 更多 metadata）。

    实现成 `dataclass` 而不是 dict：`app/tools/retriever.py` 的适配层**两种都能吃**
    （它按字段名取），但本项目内部用它更安全 —— 打错字会当场 AttributeError，
    而 dict 打错字只会得到 `None`，然后一路静默传到回答里。
    """

    text: str
    source: str
    chunk_id: str
    score: float
    section: str | None = None
    page: int | None = None
    # 融合与各路名次（可解释性：回答「它为什么排在第一条」时看这里）
    fusion_score: float = 0.0
    vector_rank: int | None = None
    keyword_rank: int | None = None
    rerank_score: float | None = None
    reranked: bool = False
    category: str = "general"
    updated_at: Any = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def citation(self) -> str:
        """人可读出处（与 `tools.retriever.RetrievedDoc.citation()` 同口径）。"""
        parts = [self.source]
        if self.page is not None:
            parts.append(f"p.{self.page}")
        if self.chunk_id:
            parts.append(f"#{self.chunk_id}")
        return " ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """dict 形态（给不依赖本模块类型的调用方 / 日志 / 前端）。"""
        return {
            "text": self.text,
            "source": self.source,
            "chunk_id": self.chunk_id,
            "score": self.score,
            "section": self.section,
            "page": self.page,
            "fusion_score": self.fusion_score,
            "vector_rank": self.vector_rank,
            "keyword_rank": self.keyword_rank,
            "rerank_score": self.rerank_score,
            "reranked": self.reranked,
            "category": self.category,
            "updated_at": self.updated_at,
            **dict(self.extra),
        }


@dataclass(frozen=True)
class RetrievalResult:
    """一次检索的完整结果（含分诊信息：命中数、耗时、是否降级）。

    返回 `list[RetrievalHit]` 之外还带这些，是因为「检索质量出问题」时的第一手信息
    全在这里：候选池多大、关键词腿召回了没有、rerank 有没有降级。
    只返回命中列表的话，排查要重跑一遍并加日志（而重跑的结果可能不同）。
    """

    query: str
    hits: list[RetrievalHit]
    candidates: int = 0
    vector_hits: int = 0
    keyword_hits: int = 0
    rerank_mode: str = "off"
    rerank_degraded: bool = False
    token_estimate: int = 0
    elapsed_ms: float = 0.0

    def __iter__(self):
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)

    def __getitem__(self, index: int) -> RetrievalHit:
        return self.hits[index]

    @property
    def sources(self) -> list[str]:
        return [hit.source for hit in self.hits]

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "hits": [hit.as_dict() for hit in self.hits],
            "candidates": self.candidates,
            "vector_hits": self.vector_hits,
            "keyword_hits": self.keyword_hits,
            "rerank_mode": self.rerank_mode,
            "rerank_degraded": self.rerank_degraded,
            "token_estimate": self.token_estimate,
            "elapsed_ms": self.elapsed_ms,
        }


# 依赖注入点：embedding 与 rerank 都是「可换的实现」，类型标出来便于阅读
Embedder = Callable[..., Awaitable[list[list[float]]]]


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[int]],
    *,
    k: int = 60,
) -> dict[int, float]:
    """RRF：`score(d) = Σ_lists 1/(k + rank(d))`（rank 从 1 开始）。

    取 `ranked_lists` 而不是「带分数的命中列表」：RRF 的定义里**只有名次**，
    接受分数只会诱导调用方去用分数（那就不是 RRF 了）。
    同一个 id 在多个列表里出现时**累加** —— 这正是「两条腿都认为它相关」的表达。
    """
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for position, doc_id in enumerate(ranked, start=1):
            scores[int(doc_id)] = scores.get(int(doc_id), 0.0) + 1.0 / (k + position)
    return scores


def _order(scores: Mapping[int, float]) -> list[int]:
    """按融合分降序；同分时按 id 升序（**稳定性**：同分乱序会让测试随机红）。"""
    return sorted(scores, key=lambda doc_id: (-scores[doc_id], doc_id))


def _query_prefix(model: str) -> str:
    """按模型名挑查询前缀（bge 系要加，见 QUERY_PREFIXES 的说明）。"""
    lowered = model.lower()
    for key, prefix in QUERY_PREFIXES.items():
        if key in lowered:
            return prefix
    return ""


def _leg_ids(hits: Sequence[VectorHit | KeywordHit]) -> list[int]:
    """一路命中的 id 名次序列（融合只吃这个）。"""
    return [int(hit.id) for hit in hits]


class Retriever:
    """把 store / embedding / rerank 组装成一条检索链路（所有依赖都可注入）。

    为什么是一个类而不是一堆模块级函数：检索有**三份可换件**（库、embedding、rerank）
    加上一组参数，模块级函数只能靠 `get_settings()` + 全局单例，测试就没法固定它们。
    对外仍然只暴露一个函数式入口（`hybrid_search`），类本身是实现细节。
    """

    def __init__(
        self,
        *,
        store: RagStore | None = None,
        settings: RagSettings | None = None,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        embedding_model: str | None = None,
    ) -> None:
        self.settings = settings or RagSettings.from_settings()
        self.store = store or RagStore(dimension=self.settings.vector_dim)
        self._embedder = embedder
        self._reranker = reranker
        self.embedding_model = embedding_model
        self._prefix = ""

    # ── 三份可换件的懒加载（都要能被测试替换）──────────────────────────────
    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = default_embed
        return self._embedder

    def _resolve_model(self) -> str:
        if self.embedding_model:
            return self.embedding_model
        from app.config import get_settings

        return getattr(get_settings(), "embedding_model", "") or ""

    def reranker(self) -> Reranker:
        if self._reranker is None:
            self._reranker = build_reranker(
                self.settings.rerank,
                model=self.settings.rerank_model,
                api_base=self.settings.rerank_api_base,
                api_key=self.settings.rerank_api_key,
            )
        return self._reranker

    # ── 单腿（都返回带 id 的命中，供融合使用）───────────────────────────────
    async def embed_query(self, query: str) -> list[float]:
        """查询向量化（**带 bge 查询前缀**，见 QUERY_PREFIXES）。"""
        model = self._resolve_model()
        prefix = _query_prefix(model)
        vectors = await self.embedder([f"{prefix}{query}"])
        if not vectors:
            raise RuntimeError("embedding 返回空结果（查询侧）：检查 EMBEDDING_BACKEND/EMBEDDING_MODEL 配置")
        return list(vectors[0])

    async def search_vector(self, query: str, *, top_k: int) -> list[VectorHit]:
        """向量腿。"""
        embedding = await self.embed_query(query)
        return await asyncio.to_thread(self.store.search_vector, embedding, top_k=top_k)

    async def search_keyword(self, query: str, *, top_k: int) -> list[KeywordHit]:
        """关键词腿（SQL 里手算 BM25，见 `store.search_keyword`）。"""
        return await asyncio.to_thread(
            self.store.search_keyword,
            query,
            top_k=top_k,
            min_ratio=self.settings.keyword_min_ratio,
            k1=self.settings.bm25_k1,
            b=self.settings.bm25_b,
        )

    # ── 融合 ────────────────────────────────────────────────────────────────
    def fuse(
        self,
        vector_hits: Sequence[VectorHit],
        keyword_hits: Sequence[KeywordHit],
    ) -> tuple[list[int], dict[int, float], dict[int, int], dict[int, int]]:
        """RRF 融合，返回 `(顺序, 融合分, 向量名次, 关键词名次)`。"""
        vector_order = _leg_ids(vector_hits)
        keyword_order = _leg_ids(keyword_hits)
        scores = reciprocal_rank_fusion([vector_order, keyword_order], k=self.settings.rrf_k)
        order = _order(scores)
        vector_rank = {doc_id: rank for rank, doc_id in enumerate(vector_order, start=1)}
        keyword_rank = {doc_id: rank for rank, doc_id in enumerate(keyword_order, start=1)}
        return order, scores, vector_rank, keyword_rank

    # ── 精排 ────────────────────────────────────────────────────────────────
    async def rerank(
        self,
        query: str,
        order: Sequence[int],
        texts: Mapping[int, str],
        *,
        top_k: int,
    ) -> tuple[list[int], dict[int, float], bool, bool]:
        """对融合顺序里的前 `top_k * 2` 条做精排。

        为什么只精排一部分：rerank 是 N 次前向（CPU 上每次 ~0.1s），
        全池精排会让检索比 LLM 还慢。取 `top_k * 2` 是因为「需要 rerank 救回来的」
        只会是靠近边界的那几条（例如向量腿第 21 名、关键词腿第 1 名的那条）。
        返回 `(顺序, 精排分, 是否降级, 是否真的精排过)`。
        """
        reranker = self.reranker()
        if reranker.name == "off":
            return list(order), {}, False, False

        limit = min(len(order), max(top_k * 2, top_k))
        head = list(order[:limit])
        tail = list(order[limit:])
        if not head:
            return [], {}, False, False

        items = [RerankItem(key=doc_id, text=texts.get(doc_id, "")) for doc_id in head]

        def _run() -> list[float]:
            return reranker.score(query, items)

        try:
            scores = await asyncio.to_thread(_run)
        except RerankUnavailable as exc:
            # 显式降级：检索不能因为「精排不可用」而整体失败（见 rerank.py 的设计说明）
            logger.warning("rerank 不可用，已退回 RRF 融合排序：%s", exc)
            return list(order), {}, True, False

        if len(scores) != len(head):
            logger.warning(
                "rerank 返回分数条数不符（期望 %d，实际 %d）：本次退回 RRF 排序",
                len(head),
                len(scores),
            )
            return list(order), {}, True, False

        rerank_scores = dict(zip(head, scores, strict=True))
        # 精排顺序：分数降序；同分回落到融合顺序（保持稳定）
        fused_order_index = {doc_id: index for index, doc_id in enumerate(order)}
        head.sort(key=lambda doc_id: (-rerank_scores[doc_id], fused_order_index[doc_id]))
        return head + tail, rerank_scores, False, True

    # ── 主入口 ──────────────────────────────────────────────────────────────
    async def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        min_score: float | None = None,
        with_rerank: bool | None = None,
    ) -> RetrievalResult:
        """混合检索（异步）。`with_rerank=False` 是**对照实验**用的（见 `rag_demo.py` 场景 6）。"""
        import time

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空（检索必须带问题文本）")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise ValueError(f"top_k 必须是 >= 1 的整数，收到 {top_k!r}")

        started = time.perf_counter()
        effective_top_k = min(top_k, self.settings.max_top_k)
        candidates = max(self.settings.candidates, effective_top_k, 10)

        vector_hits, keyword_hits = await asyncio.gather(
            self.search_vector(query, top_k=candidates),
            self.search_keyword(query, top_k=candidates),
        )

        order, fusion_scores, vector_rank, keyword_rank = self.fuse(vector_hits, keyword_hits)
        text_index = {**{int(hit.id): hit.text for hit in vector_hits}, **{int(hit.id): hit.text for hit in keyword_hits}}

        use_rerank = self.settings.rerank != "off" if with_rerank is None else with_rerank
        rerank_scores: dict[int, float] = {}
        degraded = False
        reranked = False
        if use_rerank:
            order, rerank_scores, degraded, reranked = await self.rerank(
                query, order, text_index, top_k=effective_top_k
            )

        hits: list[RetrievalHit] = []
        for doc_id in order[:effective_top_k]:
            origin = _origin(doc_id, vector_hits, keyword_hits)
            if origin is None:  # pragma: no cover - 融合顺序只来自两路命中，取不到即内部不一致
                continue
            text_value = origin.text
            if min_score is not None and fusion_scores.get(doc_id, 0.0) < float(min_score):
                # `min_score` 的语义是「融合分下限」：两路都没进前列的条目分数自然低。
                # 精排分不做硬过滤（它是否可比跨查询不确定），只进 metadata 供上层调。
                continue
            hits.append(
                RetrievalHit(
                    text=text_value,
                    source=origin.source,
                    chunk_id=origin.chunk_id,
                    score=rerank_scores.get(doc_id, fusion_scores.get(doc_id, 0.0)),
                    section=origin.section,
                    page=origin.page,
                    fusion_score=fusion_scores.get(doc_id, 0.0),
                    vector_rank=vector_rank.get(doc_id),
                    keyword_rank=keyword_rank.get(doc_id),
                    rerank_score=rerank_scores.get(doc_id),
                    reranked=reranked and doc_id in rerank_scores,
                    category=getattr(origin, "category", "general"),
                    updated_at=origin.updated_at,
                    extra={
                        "chars": len(text_value),
                        "tokens": approx_tokens(text_value),
                        "strategy": self.settings.rerank if reranked else "rrf",
                    },
                )
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        result = RetrievalResult(
            query=query,
            hits=hits,
            candidates=candidates,
            vector_hits=len(vector_hits),
            keyword_hits=len(keyword_hits),
            rerank_mode=self.settings.rerank if use_rerank else "off",
            rerank_degraded=degraded,
            token_estimate=sum(approx_tokens(hit.text) for hit in hits),
            elapsed_ms=elapsed_ms,
        )
        logger.debug(
            "混合检索：query=%r 向量腿=%d 关键词腿=%d 融合=%d 返回=%d rerank=%s(降级=%s) %.1fms",
            query[:30],
            len(vector_hits),
            len(keyword_hits),
            len(order),
            len(hits),
            result.rerank_mode,
            degraded,
            elapsed_ms,
        )
        return result

    def search_sync(self, query: str, top_k: int = 5, **kwargs: Any) -> RetrievalResult:
        """同步包装（给工具/脚本/同步调用方；内部 `asyncio.run`）。

        ⚠️ 不能在**已经在跑事件循环**的线程里调用（API 路由就是这种场景）——
        那时请用 `await Retriever.search(...)`。这里显式检查并给出可照做的报错，
        而不是让 `asyncio.run` 抛一句 `RuntimeError: This event loop is already running`。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.search(query, top_k, **kwargs))
        raise RuntimeError(
            "search_sync() 不能在事件循环内调用（如 FastAPI 路由）：请改用 "
            "`await retriever.search(query, top_k)`；或把同步调用放进 `asyncio.to_thread`。"
        )


def _origin(doc_id: int, vector_hits: Sequence[VectorHit], keyword_hits: Sequence[KeywordHit]) -> Any:
    """优先取向量的原文（两条腿存的是同一行，文本一致；向量腿还带 category）。"""
    for hit in vector_hits:
        if int(hit.id) == doc_id:
            return hit
    for hit in keyword_hits:
        if int(hit.id) == doc_id:
            return hit
    return None


def _resolve_top_k(top_k: Any) -> int:
    """给模块级入口用的 `top_k` 归一化（工具层已经夹过一次，这里兜住直接调用者）。"""
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError(f"top_k 必须是 >= 1 的整数，收到 {top_k!r}")
    return top_k


# ── 模块级门面：`10.rag.md` 第 2 节的 hybrid_search ──────────────────────────
#
# 惰性单例：`app/tools/retriever.py` 会在**工具调用时**才 import 本模块，
# 而 Retriever 的构造会读配置、建 store。做成模块级单例的原因：
# ① reranker 与 embedding 模型加载都贵（十几秒），每请求新建等于每请求加载；
# ② 单例只在「默认依赖」这条路径上生效 —— 测试与需要换依赖的调用方直接 new Retriever(...)。
_DEFAULT: Retriever | None = None


def default_retriever() -> Retriever:
    """进程内默认检索器（懒加载单例）。"""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Retriever()
    return _DEFAULT


def reset_default_retriever() -> None:
    """丢掉单例（测试用：换掉配置/依赖后要能重建）。"""
    global _DEFAULT
    _DEFAULT = None


def hybrid_search(
    query: str,
    top_k: int = 5,
    *,
    min_score: float | None = None,
    retriever: Retriever | None = None,
) -> RetrievalResult:
    """混合检索入口（同步；`10.rag.md` 第 2 节的 `hybrid_search`）。

    返回 `RetrievalResult`（可迭代、可按索引取，`hits` 里每条都带 `source/chunk_id`）。

    ⚠️ 同步版本不能在事件循环里调用（见 `Retriever.search_sync`）。
    异步调用方请用 `await (retriever or default_retriever()).search(...)`。
    """
    active = retriever or default_retriever()
    return active.search_sync(query, _resolve_top_k(top_k), min_score=min_score)


async def hybrid_search_async(
    query: str,
    top_k: int = 5,
    *,
    min_score: float | None = None,
    retriever: Retriever | None = None,
) -> RetrievalResult:
    """混合检索的异步版本（API 路由 / 图节点用）。"""
    active = retriever or default_retriever()
    return await active.search(query, _resolve_top_k(top_k), min_score=min_score)


def vector_only_search(
    query: str,
    top_k: int = 5,
    *,
    retriever: Retriever | None = None,
) -> RetrievalResult:
    """**只走向量腿**（对照用：证明「混合 > 纯向量」的两个实验组之一）。

    刻意做成公开函数而不是「给 hybrid_search 传个开关」：它的用途是**实验对照**，
    一旦混进主路径，就会有人为了「快一点」在生产上用纯向量（然后精确条款号查不到）。
    单独一个函数名，让 review 时一眼能看出「这里绕过了混合检索」。
    """
    active = retriever or default_retriever()

    async def _run() -> RetrievalResult:
        import time

        started = time.perf_counter()
        candidates = max(active.settings.candidates, top_k, 10)
        hits = await active.search_vector(query, top_k=candidates)
        tail = hits[:top_k]
        return RetrievalResult(
            query=query,
            hits=[
                RetrievalHit(
                    text=hit.text,
                    source=hit.source,
                    chunk_id=hit.chunk_id,
                    score=hit.score,
                    section=hit.section,
                    page=hit.page,
                    vector_rank=rank,
                    category=hit.category,
                    updated_at=hit.updated_at,
                    extra={"chars": len(hit.text), "tokens": approx_tokens(hit.text), "strategy": "vector_only"},
                )
                for rank, hit in enumerate(tail, start=1)
            ],
            candidates=candidates,
            vector_hits=len(hits),
            keyword_hits=0,
            rerank_mode="off",
            token_estimate=sum(approx_tokens(hit.text) for hit in tail),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())
    raise RuntimeError(
        "vector_only_search() 不能在事件循环内调用：请用 `await retriever.search_vector(...)`。"
    )


__all__ = [
    "FUSION_LEG_LIMIT",
    "QUERY_PREFIXES",
    "RetrievalHit",
    "RetrievalResult",
    "Retriever",
    "default_retriever",
    "hybrid_search",
    "hybrid_search_async",
    "reciprocal_rank_fusion",
    "reset_default_retriever",
    "vector_only_search",
]
