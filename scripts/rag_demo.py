r"""RAG 检索层验收工具（`10.rag.md` 第 7/8 节的七个场景）。

用法（仓库根执行）：

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uv run python -m scripts.rag_demo --scenario all          # 全部场景
uv run python -m scripts.rag_demo --scenario retrieve      # 只跑检索
uv run python -m scripts.rag_demo --scenario hybrid        # 混合 vs 纯向量（F1/E2 的证据）
uv run python -m scripts.rag_demo --scenario rerank        # rerank 的 token 曲线（E2）
uv run python -m scripts.rag_demo --scenario incremental   # 增量更新与可见延迟（F1）
```

七个场景：

| # | 场景 | 证明什么 |
|---|---|---|
| 1 | `health` | 向量表在、维度与配置一致、语料规模 |
| 2 | `retrieve` | 知识库可检索且**每条都带溯源**（source/chunk_id） |
| 3 | `hybrid` | 混合检索 > 纯向量（对精确条款号的查询） |
| 4 | `clause` | 精确编号/条款号的命中（关键词腿的主要价值） |
| 5 | `incremental` | 增量更新：只重嵌变化的片段、旧片段失效、新内容可见延迟 |
| 6 | `rerank` | rerank 后 top-k 更精简（token 下降）且正确条款仍在 |
| 7 | `tools` | `app/tools/retriever.retrieve_docs()` 的溯源契约（上层适配层） |

⚠️ 本脚本会**写库**（场景 5 会建一份临时文档并在结束时删除）。默认只读的场景不会改数据。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from app.rag.retriever import Retriever, vector_only_search
from app.rag.settings import RagSettings
from app.rag.store import RagStore
from app.rag.textkit import approx_tokens

logger = logging.getLogger("scripts.rag_demo")

PROBE_SOURCE = "docs/kb/_rag_demo_临时政策.md"

DEMO_QUERIES: tuple[tuple[str, str], ...] = (
    ("退货政策 7天无理由退货", "退货政策.md"),
    ("第 2 条 无理由退货的运费谁承担", "退货政策.md"),
    ("第 7 条 生鲜商品能退吗", "退货政策.md"),
    ("如何修改收货地址", "常见问题.md"),
    ("能否开具增值税专用发票", "常见问题.md"),
    ("积分有效期多久", "会员积分.md"),
    ("保修期是多久", "产品保修.md"),
    ("包裹破损怎么理赔", "物流政策.md"),
    ("客服会索要验证码吗", "账户安全与隐私.md"),
    ("企业采购的账期是多久", "企业采购说明.txt"),
    # ↓ 这两条是**场景 3 的对照用例**：实测「纯向量会在多个文档间挑错条款，混合能挑对」。
    #   为什么它们有区分度：`第 9 条` 在 4 份文档里都存在，语义信号完全一样（裸编号没有语义），
    #   向量腿只能按全局语义近邻挑一个 —— 实测它挑成了《产品保修条款》的第 9 条；
    #   而关键词腿按词元精确匹配，能看出问的是哪一份文档里的第 9 条。
    ("第 9 条", "退货政策.md"),
    ("第 7 条", "退货政策.md"),
)


def _settings() -> RagSettings:
    return RagSettings.from_settings()


def _retriever(settings: RagSettings) -> Retriever:
    return Retriever(settings=settings, store=RagStore(dimension=settings.vector_dim))


def _tokens(hits) -> int:
    return sum(approx_tokens(hit.text) for hit in hits)


# ── 场景 1：自检 ─────────────────────────────────────────────────────────────
def scenario_health(settings: RagSettings) -> None:
    store = RagStore(dimension=settings.vector_dim)
    health = store.health()
    corpus = store.corpus_stats()
    print("【场景 1】向量库自检")
    print(f"  表 {health['table']}：存在={health['exists']} 建表维度={health['dim']} 配置维度={health['expected_dim']} 一致={health['dim_ok']}")
    print(f"  语料：{corpus['docs']} 份文档 / {corpus['chunks']} 个活跃片段 / 约 {corpus['total_tokens']} token")
    print(f"  检索参数：candidates={settings.candidates} rrf_k={settings.rrf_k} "
          f"keyword_min_ratio={settings.keyword_min_ratio} rerank={settings.rerank}({settings.rerank_model})")
    if not health["exists"]:
        print("  ✗ 表不存在：先跑 `uv run alembic upgrade head` 与 `uv run python -m scripts.load_knowledge_base`")
    elif corpus["chunks"] == 0:
        print("  ✗ 库里没有片段：先跑 `uv run python -m scripts.load_knowledge_base`")


# ── 场景 2：可检索 + 溯源 ────────────────────────────────────────────────────
def scenario_retrieve(settings: RagSettings, retriever: Retriever, queries) -> None:
    print("\n【场景 2】知识库可检索且带溯源（source/chunk_id）")
    misses = 0
    for query, expect in queries:
        started = time.perf_counter()
        result = asyncio.run(retriever.search(query, top_k=3))
        elapsed = (time.perf_counter() - started) * 1000
        hit_sources = [hit.source for hit in result.hits]
        top_hit = result.hits[0] if result.hits else None
        flag = "✅" if top_hit and expect in top_hit.source else "❌"
        if flag == "❌":
            misses += 1
        print(
            f"  {flag} {query!r:32s} → {elapsed:6.0f}ms（向量腿 {result.vector_hits} / 关键词腿 {result.keyword_hits}）"
            f" top1={top_hit.source if top_hit else '-'} {top_hit.section if top_hit else ''}"
        )
        print(f"      期望命中 {expect}；返回来源 {hit_sources}")
        if top_hit:
            print(f"      溯源：{top_hit.citation()}（rerank={top_hit.rerank_score}）")
    print(f"  → top-1 命中期望来源：{len(queries) - misses}/{len(queries)}")


# ── 场景 3：混合 vs 纯向量 ──────────────────────────────────────────────────
def scenario_hybrid(settings: RagSettings, retriever: Retriever, queries) -> None:
    print("\n【场景 3】混合检索 vs 纯向量（对含精确条款号/专名的查询）")
    print(f"  {'查询':34s} {'纯向量 top3 来源':30s} {'混合 top3 来源':30s}")
    hybrid_better = 0
    for query, expect in queries:
        vector = vector_only_search(query, top_k=3, retriever=retriever)
        hybrid = asyncio.run(retriever.search(query, top_k=3))
        v_ok = any(expect in hit.source for hit in vector.hits)
        h_ok = any(expect in hit.source for hit in hybrid.hits)
        if h_ok and not v_ok:
            hybrid_better += 1
        v_sources = ",".join(Path(hit.source).name for hit in vector.hits)
        h_sources = ",".join(Path(hit.source).name for hit in hybrid.hits)
        print(f"  {query!r:34s} {v_sources:30s} {h_sources:30s}")
        print(f"      纯向量含期望={v_ok} 混合含期望={h_ok}")
    print(f"  → 纯向量漏、混合补上的查询数：{hybrid_better}/{len(queries)}")


# ── 场景 4：精确条款号 ──────────────────────────────────────────────────────
def scenario_clause(retriever: Retriever) -> None:
    print("\n【场景 4】精确条款号命中（关键词腿的主场）")
    cases = ("第 7 条", "第 3 条 不予受理", "第 2 条 运费", "1.1 配送范围")
    for query in cases:
        result = asyncio.run(retriever.search(query, top_k=2))
        print(f"  {query!r}")
        for hit in result.hits:
            print(
                f"      k_rank={hit.keyword_rank} v_rank={hit.vector_rank} "
                f"[{hit.section}] {hit.text[:46]!r}"
            )


# ── 场景 5：增量更新 ────────────────────────────────────────────────────────
def scenario_incremental(settings: RagSettings, retriever: Retriever) -> None:
    from app.rag.ingest import delete_document, ingest_document

    print("\n【场景 5】增量更新（只重嵌变化的片段 + 旧片段失效 + 可见延迟）")
    store = RagStore(dimension=settings.vector_dim)
    body = """# 临时演示政策

第 1 条：这是一段用于演示增量入库的占位条款，内容与真实业务无关。

第 2 条：第二段占位条款，用来保证文档至少有两个片段。

第 3 条：第三段占位条款，保证修改第 1 条时其余片段可复用。
"""
    asyncio.run(delete_document(PROBE_SOURCE, hard=True, store=store, settings=settings))

    first = asyncio.run(
        ingest_document(PROBE_SOURCE, content=body, store=store, settings=settings)
    )
    print(f"  首次入库：action={first.action} 片段={first.chunks_total} 重嵌={first.embedded} 耗时={first.elapsed_ms:.0f}ms")

    same = asyncio.run(
        ingest_document(PROBE_SOURCE, content=body, store=store, settings=settings)
    )
    print(f"  原样重跑：action={same.action} 未变={same.unchanged} **重嵌={same.embedded}** 耗时={same.elapsed_ms:.0f}ms"
          "（增量生效的证据：0 次 embedding）")

    changed_body = body.replace("第 1 条：这是一段用于演示增量入库的占位条款", "第 1 条：这条被改过了，应该触发重新嵌入与旧片段失效")
    changed = asyncio.run(
        ingest_document(PROBE_SOURCE, content=changed_body, store=store, settings=settings)
    )
    print(f"  改第 1 条：action={changed.action} 新增={changed.added} 更新={changed.updated} "
          f"未变={changed.unchanged} 失效={changed.deactivated} 重嵌={changed.embedded} 版本={changed.version}")
    print(f"  可见延迟：{changed.elapsed_ms:.0f}ms（要求 < 60s，F1）")

    started = time.perf_counter()
    result = asyncio.run(retriever.search("这条被改过了", top_k=2))
    elapsed = (time.perf_counter() - started) * 1000
    top = result.hits[0] if result.hits else None
    visible = bool(top and "被改过" in top.text)
    print(f"  改后检索：{'✅ 新内容已可见' if visible else '❌ 检索到的还是旧内容'}（{elapsed:.0f}ms）")
    if top:
        print(f"      top1 [{top.section}] {top.text[:40]!r}")

    store_snapshot = asyncio.run(asyncio.to_thread(_count_versions, store, PROBE_SOURCE))
    print(f"  库内该文档：活跃 {store_snapshot['active']} 条 / 失效 {store_snapshot['inactive']} 条"
          "（旧片段保留可查、检索不可见）")

    cleanup = asyncio.run(delete_document(PROBE_SOURCE, hard=True, store=store, settings=settings))
    print(f"  清理：{cleanup.action}（删除 {cleanup.deactivated} 条片段）")


def _count_versions(store: RagStore, source: str) -> dict[str, int]:
    from sqlalchemy import text

    with store.read_engine.connect() as conn:
        row = conn.execute(
            text(
                f"select count(*) filter (where is_active) as active, "
                f"count(*) filter (where not is_active) as inactive "
                f"from {store.chunks_table} where source = :source"
            ),
            {"source": source},
        ).one()
    return {"active": int(row.active), "inactive": int(row.inactive)}


# ── 场景 6：rerank 的收益（E2） ─────────────────────────────────────────────
def _rank_of(hits, expect: str) -> int:
    """期望来源第一次出现在第几名（0 = 没出现）。"""
    for index, hit in enumerate(hits, start=1):
        if expect in hit.source:
            return index
    return 0


def scenario_rerank(settings: RagSettings, retriever: Retriever, queries) -> None:
    print("\n【场景 6】rerank 的收益：命中位置前移 + 送进 LLM 的上下文更短（E2）")
    print("  口径：候选池 = 初召回的全部片段（不进 LLM）；top-5 = 真正送进 LLM 的上下文。")
    print(f"  {'查询':30s} {'粗排 rank':>9s} {'精排 rank':>9s} {'候选池 token':>12s} {'top5 token':>10s}")
    pool_tokens = top_tokens = 0
    before_ranks: list[int] = []
    after_ranks: list[int] = []
    for query, expect in queries:
        plain = asyncio.run(retriever.search(query, top_k=5, with_rerank=False))
        ranked = asyncio.run(retriever.search(query, top_k=5, with_rerank=True))
        pool = asyncio.run(
            retriever.search(query, top_k=settings.candidates, with_rerank=False)
        )
        pool_token = _tokens(pool.hits)
        top_token = _tokens(ranked.hits)
        pool_tokens += pool_token
        top_tokens += top_token
        before = _rank_of(plain.hits, expect)
        after = _rank_of(ranked.hits, expect)
        before_ranks.append(before or 99)
        after_ranks.append(after or 99)
        print(
            f"  {query!r:30s} {before:9d} {after:9d} {pool_token:12d} {top_token:10d}"
        )
    mrr_before = sum(1 / rank for rank in before_ranks) / len(before_ranks)
    mrr_after = sum(1 / rank for rank in after_ranks) / len(after_ranks)
    print(
        f"  → 上下文压缩：候选池 {pool_tokens} token → top-5 {top_tokens} token"
        f"（{top_tokens / pool_tokens - 1:+.0%}，这就是 E2 说的「只把精华送进 LLM」）"
    )
    print(
        f"  → 质量：top-1 命中 {before_ranks.count(1)}/{len(queries)} → {after_ranks.count(1)}/{len(queries)}；"
        f"MRR {mrr_before:.3f} → {mrr_after:.3f}（越高越好）"
    )
    print(
        "  ⚠️ 「rank」是**期望来源**在 top-5 里的名次：精排把对的条款往前挪，"
        "而在小语料上粗排本来也不差 —— 所以真正的收益是「上下文少了 7 成」而不是「分数更好看」。"
    )


# ── 场景 7：工具层契约 ──────────────────────────────────────────────────────
def scenario_tools(settings: RagSettings, queries) -> None:
    print("\n【场景 7】`app/tools/retriever.retrieve_docs()` 的溯源契约（上层适配层）")
    from app.config import get_settings

    from app.rag.retriever import reset_default_retriever

    reset_default_retriever()
    from app.tools.retriever import retrieve_docs

    for query, expect in queries[:3]:
        docs = retrieve_docs(query, top_k=2, caller="scripts.rag_demo")
        print(f"  {query!r} → {len(docs)} 条")
        for doc in docs:
            print(f"      citation={doc.citation()!r} score={doc.score:.4f} text={doc.text[:30]!r}")
        print(f"      全部带 source：{all(doc.source for doc in docs)}（无源的命中会被适配层丢弃并 WARNING）")
    print(f"  当前 embedding 后端：{get_settings().embedding_backend} / {get_settings().embedding_model}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAG 检索层验收工具（七个场景）")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=("all", "health", "retrieve", "hybrid", "clause", "incremental", "rerank", "tools"),
    )
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    settings = _settings()
    print(f"RAG 配置：{settings}\n")
    retriever = _retriever(settings)
    queries = DEMO_QUERIES

    if args.scenario in ("all", "health"):
        scenario_health(settings)
    if args.scenario in ("all", "retrieve"):
        scenario_retrieve(settings, retriever, queries)
    if args.scenario in ("all", "hybrid"):
        scenario_hybrid(settings, retriever, queries)
    if args.scenario in ("all", "clause"):
        scenario_clause(retriever)
    if args.scenario in ("all", "incremental"):
        scenario_incremental(settings, retriever)
    if args.scenario in ("all", "rerank"):
        scenario_rerank(settings, retriever, queries)
    if args.scenario in ("all", "tools"):
        scenario_tools(settings, queries)

    print("\n提示：向量库会被场景 5 修改（临时文档已删除）；其余场景只读。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
