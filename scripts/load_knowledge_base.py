r"""把知识库目录灌进向量库（`10.rag.md` 第 6 节第 5 条）。

用法（仓库根执行）：

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uv run python -m scripts.load_knowledge_base                 # 默认 docs/kb，增量
uv run python -m scripts.load_knowledge_base --force         # 强制重解析（片段级增量仍生效）
uv run python -m scripts.load_knowledge_base --prune         # 顺便下线「盘上已删除」的文档
uv run python -m scripts.load_knowledge_base --dry-run       # 只解析 + 分块，不写库、不算向量
uv run python -m scripts.load_knowledge_base --source docs/kb/退货政策.md   # 只灌一份
```

**为什么是脚本而不是启动时自动灌库**：灌库要花 embedding 的钱（API 后端）
和几十秒的模型加载（本地后端），把它挂在启动路径上会让「重启一次服务」= 「重算一遍向量」。
知识库的更新频率是「天」，服务的重启频率是「天/次」—— 两者必须解耦。

**为什么默认增量而不是全量重建**：`10.rag.md` 第 4 节第 3 条要求增量。
本脚本把「增量」做在**片段级**：文档没变则整篇跳过（一次 SELECT + 一次文件解析），
文档变了则只对内容变化的片段重嵌（实测：10 段改 1 段 → 只嵌 1 段）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.rag.ingest import ingest_path
from app.rag.parse import ParseError, parse_text
from app.rag.settings import RagSettings
from app.rag.store import RagStore
from app.rag.textkit import approx_tokens

logger = logging.getLogger("scripts.load_knowledge_base")

DEFAULT_ROOT = "docs/kb"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def dry_run(root: Path, *, strategy: str | None = None) -> int:
    """只跑「解析 + 分块」，打印每份文档的策略与片段数（**不写库、不算向量**）。

    这是改分块参数（`RAG_CHUNK_*`）时的第一手工具：分块错了先在这里看出来，
    而不是灌进库之后靠检索结果反推 —— 后者的反馈链太长，而且每次都要花钱算向量。
    """
    files = sorted(
        item for item in root.rglob("*") if item.is_file() and item.suffix.lower() in (".md", ".markdown", ".txt", ".html", ".htm", ".pdf")
    )
    if not files:
        print(f"目录里没有可解析的文档：{root}", file=sys.stderr)
        return 1

    total_chunks = 0
    total_tokens = 0
    for file in files:
        try:
            parsed = parse_text(file)
        except ParseError as exc:
            print(f"  ✗ {file.as_posix()}: {exc}", file=sys.stderr)
            continue
        from app.rag.chunking import ChunkingConfig, chunk_document, detect_strategy

        config = ChunkingConfig(strategy=strategy or "auto")
        chunks = chunk_document(parsed, config)
        tokens = sum(approx_tokens(chunk.text) for chunk in chunks)
        total_chunks += len(chunks)
        total_tokens += tokens
        detected = detect_strategy(parsed)
        print(
            f"  {file.as_posix():44s} kind={parsed.kind:8s} 自动判={detected:6s} "
            f"片段={len(chunks):3d} 约 {tokens:5d} token"
        )
        for chunk in chunks[:3]:
            head = chunk.text.replace("\n", " ")[:52]
            print(f"      · [{chunk.section or '-'}] p.{chunk.page} {head}")
        if len(chunks) > 3:
            print(f"      · …（还有 {len(chunks) - 3} 段）")

    print(
        f"\n合计：{len(files)} 份文档，{total_chunks} 个片段，约 {total_tokens} token"
        f"（token 是近似口径，见 app/rag/textkit.approx_tokens）"
    )
    return 0


async def load(args: argparse.Namespace) -> int:
    """真正灌库（增量）。"""
    settings = RagSettings.from_settings()
    root = Path(args.source or args.root)

    print("向量配置：", end="")
    from app.config import get_settings

    config = get_settings()
    print(
        f"backend={config.embedding_backend} model={config.embedding_model} "
        f"dim={settings.vector_dim or config.embedding_dim} "
        f"rerank={settings.rerank}({settings.rerank_model})"
    )
    print(
        f"分块参数：max_chars={settings.chunk_max_chars} overlap={settings.chunk_overlap} "
        f"candidates={settings.candidates} rrf_k={settings.rrf_k} "
        f"keyword_min_ratio={settings.keyword_min_ratio}"
    )

    store = RagStore(dimension=settings.vector_dim)
    health = store.health()
    print(f"向量表自检：{health}")
    if not health["exists"]:
        print(
            "向量表不存在：先执行 `uv run alembic upgrade head`（第 10 步的迁移）",
            file=sys.stderr,
        )
        return 2
    if health["expected_dim"] and health["dim"] and not health["dim_ok"]:
        print(
            f"⚠️ 维度不一致：表上是 vector({health['dim']})，配置要 {health['expected_dim']}。"
            "先统一 RAG_VECTOR_DIM / EMBEDDING_DIM / 迁移三处，再灌库。",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        return dry_run(root, strategy=args.strategy)

    # ── 第 13 步：知识库一变，语义缓存整批失效（issue 010 的修复点）──────────
    # ⚠️ 这一行曾经**不存在**，后果是：`corpus_revision` 永远停在 1，
    #    而缓存条目也全是 revision=1 → `is_fresh()` 判「新鲜」→
    #    「语料已经重新灌好了，缓存还在讲『没有找到相关知识库片段』」。
    #    政策类条目的 TTL 是 7 天，所以这**能持续一整周**。
    #
    # 为什么用 `make_ingest_callback()` 而不是直接 `bump_after_ingest`：
    # 那里已经把「revisions=None 时解析成进程默认的**那一个**实例」处理好了，
    # 绕开它就会撞上「推了版本但缓存没失效」的老坑（见该函数的 docstring）。
    # `--no-invalidate` 给「只想重算向量、不想清缓存」的排障场景留一个出口。
    on_change = None
    if not args.no_invalidate:
        from app.cache.invalidate import make_ingest_callback

        on_change = make_ingest_callback()
        print("缓存失效：知识库有变更时会推进 corpus_revision，让语义缓存整批失效")

    if args.limit and not Path(root).is_file():
        # 只灌前 N 份：给「先小规模验证一次再全量」用的（本地 embedding 下前几次调用最慢）
        files = sorted(item for item in Path(root).rglob("*") if item.is_file())
        supported = [item for item in files if item.suffix.lower() in (".md", ".markdown", ".txt", ".html", ".htm", ".pdf")][: args.limit]
        total = {"documents": 0, "created": 0, "updated": 0, "unchanged": 0, "embedded": 0, "chunks": 0}
        for file in supported:
            from app.rag.ingest import ingest_document

            report = await ingest_document(
                file,
                category=args.category,
                store=store,
                settings=settings,
                force=args.force,
                on_change=on_change,
            )
            total["documents"] += 1
            total["chunks"] += report.chunks_total
            total["embedded"] += report.embedded
            total[report.action if report.action in ("created", "updated", "unchanged") else "unchanged"] += 1
        summary = total
    else:
        report = await ingest_path(
            root,
            category=args.category,
            store=store,
            settings=settings,
            force=args.force,
            on_change=on_change,
        )
        summary = report.as_dict()

    print("\n入库汇总：")
    for key in ("documents", "created", "updated", "unchanged", "chunks", "embedded"):
        print(f"  {key:10s} {summary.get(key)}")
    print(f"  耗时       {summary.get('elapsed_ms')} ms")

    if args.prune:
        from app.rag.sync import RagSyncWorker

        # ⚠️ `on_change=` 必须一起传（issue 010）：下线文档同样是知识库变更，
        # 缓存里「引用这份文档」的答案要跟着失效 —— 否则会出现「文档已下线，
        # 缓存还在照它回答」。漏传的症状与灌库漏传完全一样，都是 revision 不动。
        worker = RagSyncWorker(
            root if Path(root).is_dir() else Path(root).parent,
            store=store,
            settings=settings,
            on_change=on_change,
        )
        sync_report = await worker.sync_once(prune=True, force=args.force)
        if sync_report.pruned:
            print(f"已下线（盘上已删除）：{', '.join(sync_report.pruned)}")
        else:
            print("需要下线的文档：无")

    corpus = store.corpus_stats()
    print(f"\n语料现状：{corpus['docs']} 份文档 / {corpus['chunks']} 个活跃片段 / 约 {corpus['total_tokens']} token")
    print("下一步：`uv run python -m scripts.rag_demo --scenario all` 验证检索质量")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把知识库目录灌进 pgvector（增量）")
    parser.add_argument("--root", default=DEFAULT_ROOT, help=f"知识库目录（默认 {DEFAULT_ROOT}）")
    parser.add_argument("--source", default="", help="只灌这一份文件（覆盖 --root）")
    parser.add_argument("--category", default="kb", help="文档类目（默认 kb）")
    parser.add_argument("--strategy", default=None, help="强制分块策略：policy/manual/faq（默认自动判断）")
    parser.add_argument("--force", action="store_true", help="忽略文档指纹，强制重新解析与比对")
    parser.add_argument("--prune", action="store_true", help="顺便下线「盘上已删除」的文档")
    parser.add_argument("--limit", type=int, default=0, help="只灌前 N 份（先小规模验证时用）")
    parser.add_argument("--dry-run", action="store_true", help="只解析 + 分块，不写库、不算向量")
    parser.add_argument(
        "--no-invalidate",
        action="store_true",
        help="不清语义缓存（默认会清：知识库有变更就推进 corpus_revision）。"
        "只在「重算向量但故意要保留缓存」的排障场景用",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 DEBUG 日志")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    return asyncio.run(load(args))


if __name__ == "__main__":
    raise SystemExit(main())
