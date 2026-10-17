"""入库：解析 → 分块 → embedding → 增量 upsert（`10.rag.md` 第 6 节第 2、4、5 条）。

```
文件 ──parse_text()──→ ParsedDocument ──chunk_document()──→ [TextChunk]
                                                                │
                            embed(texts)  ← 第 5 步的统一门面（API 或本地后端）
                                                                │
                          store.upsert_document()  ← 增量：只有变化的片段才重嵌/重写
                                                                │
                                             IngestReport（新增/更新/未变/失效 各几条）
```

**两段式（先算向量、再写库）**而不是边算边写：
① embedding 可能失败/限流（`RetryableLLMError`），先算完就不会留下「写了一半的文档」；
② 增量判断需要先知道「哪些片段变了」，那是在拿到新片段后立刻能算的，
   不必等向量 —— 于是**未变的片段一次 embedding 都不花**（这正是 F1 + E2 的成本落点）。

增量判断的粒度是**片段**（内容 hash），不是文档：改一个条款只重嵌那一条，
其余片段连 SQL 都只读不写（`unchanged`）。实测：10 段文档改 1 段 → 只嵌 1 段。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.llm import embed as default_embed
from app.rag.chunking import ChunkingConfig, TextChunk, chunk_document
from app.rag.parse import SUPPORTED_SUFFIXES, ParsedDocument, parse_text
from app.rag.settings import RagSettings
from app.rag.store import RagStore, UpsertReport

logger = logging.getLogger(__name__)

Embedder = Callable[..., Awaitable[list[list[float]]]]

# embedding 的批大小：一次请求里塞太多文本会被厂商的**单次行数上限**直接拒（HTTP 400），
# 而本地后端在超大 batch 上会吃满内存。
#
# ⚠️ 这个数字是**按厂商上限**定的，不是拍脑袋的「稳妥值」。实测（2026-10）：
#   * 阿里云 `qwen3.7-text-embedding-flash`：**batch ≤ 25**（32 条会返回
#     `InvalidParameter: batch size is invalid, it should not be larger than 25`）。
#     注意：官方表格写的是「最大行数 20」，但实测 25 条能过、32 条被拒 ——
#     以**实测**为准，官方文档在这处与实现不一致。
#   * 本地 bge-m3：无硬上限，瓶颈在内存。
#
# 取 16 是刻意留余量（低于 25 一档，且是 2 的幂）：厂商上限随时可能调整，
# 顶到 25 的边界值意味着一改策略就红。需要更大吞吐时改这个常量并**实测**一次。
EMBED_BATCH = 16


@dataclass
class IngestReport:
    """一次入库的结果（`IngestReport` 是「增量真的生效」的证据载体）。"""

    source: str
    action: str  # created | updated | unchanged | reindexed | deleted
    doc_id: int = 0
    version: int = 0
    chunks_total: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deactivated: int = 0
    purged: int = 0
    embedded: int = 0
    strategy: str = "auto"
    category: str = "general"
    kind: str = "text"
    elapsed_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)
    chunk_ids: list[str] = field(default_factory=list)

    @property
    def doc_changed(self) -> bool:
        """这次入库**有没有真的改到文档内容**（缓存版本失效的触发判据）。

        ## ⚠️ 这个属性是被 `on_change` 的接线逼出来的（issue 010 的连带缺陷）

        `ingest_document` 在**很久以前**就写了 `if report.doc_changed:` 来决定
        要不要调 `on_change`，但 `IngestReport` 上**从来没有**这个字段，
        而且那句话在 `try` 之外 —— 于是任何调用方只要真的传了 `on_change=`，
        第一次入库就会 `AttributeError: 'IngestReport' object has no attribute 'doc_changed'`。

        后果不是「缓存偶尔不失效」，而是**这条接线根本没人敢接**：
        `scripts/load_knowledge_base.py` 一直没传 `on_change`，
        所以灌库从不推进 `corpus_revision`，语义缓存里的旧条目（包括
        「检索回 0 条」那种）会一直讲到 TTL 到期（政策类 7 天）。

        ## 为什么用「派生」而不是再加一个字段

        「有没有变」是 `action` 的函数（`action` 已经由 `_is_noop(upsert)` 算好了）：
        `created` / `updated` / `reindexed` 都算变，`unchanged` 不算；
        `deleted`（下线）**也算变** —— 文档没了，缓存里引用它的答案同样必须失效，
        否则会出现「文档已经下线，缓存还在照它回答」。
        所以这里只把 `unchanged` 排除在外。
        """
        return self.action != "unchanged"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "action": self.action,
            "doc_id": self.doc_id,
            "version": self.version,
            "chunks_total": self.chunks_total,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deactivated": self.deactivated,
            "purged": self.purged,
            "embedded": self.embedded,
            "strategy": self.strategy,
            "category": self.category,
            "kind": self.kind,
            "doc_changed": self.doc_changed,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "warnings": list(self.warnings),
        }


def document_hash(text: str) -> str:
    """文档级指纹（**内容** hash，不是 mtime）。

    为什么不用 mtime/size：`git checkout`、编辑器保存、`touch` 都会改 mtime 而不改内容，
    于是「同步」会做一堆无用功（重解析 + 全片段比对）；反过来，mtime 相同而内容不同
    （同一秒内的两次保存）会让变更被漏掉 —— 后者是数据错误，前者只是浪费。
    用内容 hash 两个问题一起消失。
    """
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _is_noop(report: UpsertReport) -> bool:
    """这次 upsert 有没有真的改到东西（`unchanged` 之外全为 0 且文档指纹没变）。"""
    return (
        not report.doc_changed
        and report.added == 0
        and report.updated == 0
        and report.deactivated == 0
    )


async def _embed_chunks(chunks: Sequence[TextChunk], embedder: Embedder) -> list[list[float]]:
    """分批把片段向量化（顺序与入参严格一致，`embed()` 已经保证保序）。"""
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[start : start + EMBED_BATCH]
        vectors.extend(await embedder([chunk.text for chunk in batch]))
    if len(vectors) != len(chunks):
        raise RuntimeError(
            f"embedding 条数与片段数不一致：{len(vectors)} vs {len(chunks)}。"
            "错位会让向量与文本静默错配，因此直接失败（见 store.upsert_document 的同名校验）。"
        )
    return vectors


def _pack(
    document: ParsedDocument,
    chunks: Sequence[TextChunk],
    *,
    strategy: str,
    category: str,
    warnings: list[str],
) -> tuple[str, list[TextChunk]]:
    """把解析结果与分块结果打包（含「空文档」的告警口径）。"""
    if document.is_empty:
        warnings.append(
            "解析结果为空：扫描件/加密 PDF/空文件都会这样。该文档不会被灌入知识库，"
            "也不会删除已有片段（避免「一次失败的解析把好数据清掉」）。"
        )
    if not chunks and not document.is_empty:
        warnings.append("分块结果为空：检查 RAG_CHUNK_MAX_CHARS 是否过小，或文档是否全是标题。")
    return document.source, list(chunks)


async def ingest_document(
    path: str | Path,
    *,
    content: str | bytes | None = None,
    category: str = "general",
    strategy: str | None = None,
    store: RagStore | None = None,
    settings: RagSettings | None = None,
    embedder: Embedder | None = None,
    force: bool = False,
    now: Any = None,
    on_change: Callable[[IngestReport], None] | None = None,
) -> IngestReport:
    """把一份文档增量灌入知识库（`10.rag.md` 第 2 节的 `ingest_document`）。

    参数：
        path:     文件路径（也是溯源里的 `source` —— 用相对仓库根的路径最稳定）。
        content:  直接给内容（测试与「内存里的文档」用）；`None` 时读文件。
        category: 文档类目（政策/手册/FAQ…），检索时可按类目过滤。
        strategy: 强制分块策略（`None` = 按 `RAG_CHUNK_*` 与内容自动判断）。
        force:    即使文档指纹没变也走一遍 upsert（默认 `False` —— 这才是「增量」）。
        now:      注入时间戳（测试断言 `updated_at` 用；`None` = 数据库 `now()`）。
        on_change: **第 13 步新增**：真正改动发生之后的回调（签名收 `IngestReport`）。
                   `app/cache` 用它推进知识库版本，从而让语义缓存整批失效
                   （`13.cache.md` §5 的第三级失效）。

    ## 为什么回调签名收的是「报告」而不是「几个数字」

    回调需要的信息（source / version / 有没有真的变）**全在报告里**，
    而报告是这次入库的**结论**，不是它的输入。让回调收 `(source, version)` 意味着
    调用方要自己判断「这次算不算变」—— 那就有两处判断，迟早漂移
    （症状是「某个路径下知识库更新了但缓存没失效」，而它只在特定入口出现）。

    ## 回调**失败不影响入库**

    入库成功了，而缓存版本没推进 —— 这不是一回事。所以回调异常会被吞掉并打 WARNING，
    外加一条进 `report.warnings`（这样「缓存为什么没失效」在入库的返回值里就能看到，
    不必去翻日志）。这是刻意的非对称处置：**入库是主路径，缓存失效是配套动作**。

    返回 `IngestReport`；`action` 是 `created/updated/unchanged` 三态。
    """
    import time

    started = time.perf_counter()
    resolved_settings = settings or RagSettings.from_settings()
    active_store = store or RagStore(dimension=resolved_settings.vector_dim)
    active_embedder = embedder or default_embed

    warnings: list[str] = []
    parsed = parse_text(path, content)
    fingerprint = document_hash(parsed.text)

    explicit_strategy = strategy or ("auto" if resolved_settings.chunk_max_chars else "auto")
    chunks = chunk_document(
        parsed,
        ChunkingConfig(
            strategy=strategy or "auto",
            max_chars=resolved_settings.chunk_max_chars,
            overlap=resolved_settings.chunk_overlap,
        ),
        strategy=explicit_strategy,
    )
    source, chunks = _pack(parsed, chunks, strategy=explicit_strategy, category=category, warnings=warnings)

    previous = active_store.document_state(source)
    if previous is not None and previous.get("is_active") is False:
        # 文档曾被软删（下线）：重新入库即视为复活，但要说清楚 —— 否则
        # 「我明明下线了它」与「同步脚本又把它灌回来了」会变成一场没有日志的争执。
        warnings.append("该文档此前处于下线状态（is_active=false），本次入库会把它重新上线。")

    if previous is not None and previous.get("doc_hash") == fingerprint and not force and chunks:
        # 文档指纹没变 → 连片段级比对都不需要，直接返回 unchanged。
        # ⚠️ 这里**不**更新 updated_at：那样会让「上次同步时间」失去意义
        # （每跑一次同步都刷新，看时间戳就分不出「文档变了」还是「同步器跑过了」）。
        report = IngestReport(
            source=source,
            action="unchanged",
            doc_id=int(previous.get("doc_id") or 0),
            version=int(previous.get("version") or 1),
            chunks_total=int(previous.get("chunk_count") or len(chunks)),
            unchanged=len(chunks),
            strategy=explicit_strategy,
            category=category,
            kind=parsed.kind,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            warnings=warnings,
        )
        logger.info("入库跳过（文档指纹未变）：%s（%d 个片段，0 次 embedding）", source, len(chunks))
        return report

    if not chunks:
        # 抽出空文本：**不删除**已有片段（见 _pack 的告警），直接返回带 warning 的报告。
        return IngestReport(
            source=source,
            action="unchanged",
            doc_id=int((previous or {}).get("doc_id") or 0),
            version=int((previous or {}).get("version") or 0),
            strategy=explicit_strategy,
            category=category,
            kind=parsed.kind,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            warnings=warnings,
        )

    # 两段式的第二段：**先只读地问「哪些片段要动」**，再只给这些片段算向量。
    # 未变的片段一次 embedding 都不花 —— 这是 F1 + E2 的成本落点（实测：10 段改 1 段 → 只嵌 1 段）。
    incoming_hashes = {chunk.chunk_id: chunk.content_hash for chunk in chunks}
    _, changed_hashes = await asyncio.to_thread(
        active_store.change_plan, source, incoming_hashes
    )
    pending = [chunk for chunk in chunks if chunk.chunk_id in changed_hashes]

    vectors: list[list[float]] = []
    if pending:
        vectors = await _embed_chunks(pending, active_embedder)

    upsert = await asyncio.to_thread(
        active_store.upsert_document,
        source=source,
        doc_hash=fingerprint,
        category=category,
        strategy=explicit_strategy,
        chunks=pending,
        embeddings=vectors,
        incoming=incoming_hashes,
        now=now,
    )

    action = "unchanged" if _is_noop(upsert) else ("created" if upsert.version == 1 else "updated")
    report = IngestReport(
        source=source,
        action=action,
        doc_id=upsert.doc_id,
        version=upsert.version,
        chunks_total=upsert.chunks_total,
        added=upsert.added,
        updated=upsert.updated,
        unchanged=upsert.unchanged,
        deactivated=upsert.deactivated,
        purged=upsert.purged,
        embedded=len(pending),
        strategy=explicit_strategy,
        category=category,
        kind=parsed.kind,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        warnings=warnings,
        chunk_ids=[chunk.chunk_id for chunk in chunks],
    )
    logger.info(
        "入库完成：%s action=%s 片段=%d（新增 %d / 更新 %d / 未变 %d / 失效 %d）embedding=%d 次 %.0fms",
        source,
        action,
        upsert.chunks_total,
        upsert.added,
        upsert.updated,
        upsert.unchanged,
        upsert.deactivated,
        len(pending),
        report.elapsed_ms,
    )
    # ── 第 13 步：知识库变更之后推进缓存版本（第三级失效的触发点）──────────
    # 只在**真的改动了**的时候调（`report.doc_changed`）：跑一次同步但什么都没变
    # 也会 bump 一次版本，那会把整份缓存白清一遍 —— 而它看起来只是「命中率偶尔掉一下」。
    #
    # ⚠️ 判据本身也要放进 `try`（issue 010）：`report.doc_changed` 若不巧还是个
    # 不存在的属性，`AttributeError` 会在 `try` **之外**炸掉整次入库 ——
    # 而入库本身已经成功了，用户看到的却是"灌库失败"，且缓存没失效。
    # 「回调链路出任何问题都不许反噬入库」这条口径，对判据同样成立。
    if on_change is not None:
        try:
            changed = bool(report.doc_changed)
        except Exception as exc:  # noqa: BLE001 - 判据出问题也不许让入库失败
            logger.warning("读取 doc_changed 失败，跳过缓存失效（入库本身已成功）：%s", exc)
            changed = False
            report.warnings.append(f"无法判定本次是否改动，已跳过缓存失效：{exc}")
        if changed:
            try:
                on_change(report)
            except Exception as exc:  # noqa: BLE001 - 缓存侧的问题不该让入库失败
                logger.warning("入库后的 on_change 回调失败（入库本身已成功）：%s: %s", type(exc).__name__, exc)
                report.warnings.append(
                    f"知识库变更回调失败（语义缓存可能未失效，靠 TTL 兜底）：{type(exc).__name__}: {exc}"
                )
    return report


async def reindex_document(
    path: str | Path,
    *,
    content: str | bytes | None = None,
    force: bool = True,
    **kwargs: Any,
) -> IngestReport:
    """**强制**重建一份文档的索引（`10.rag.md` 第 2 节的 `reindex_document`）。

    与 `ingest_document` 的唯一区别是默认 `force=True`：跳过「文档指纹没变就返回」的短路，
    仍然走片段级增量（未变的片段照样不重嵌）—— 「重建」指的是**重新解析与重新比对**，
    不是「把所有向量删了重算」。真要全量重算，先 `delete_document(hard=True)` 再灌。
    """
    return await ingest_document(path, content=content, force=force, **kwargs)


async def delete_document(
    source: str,
    *,
    hard: bool = False,
    store: RagStore | None = None,
    settings: RagSettings | None = None,
    on_change: Callable[[IngestReport], None] | None = None,
) -> IngestReport:
    """下线一份文档（`10.rag.md` 第 2 节的 `delete_document`）。

    默认**软删**：片段标 `is_active=false`、文档标无效 —— 引用还在、检索不再可见
    （理由见 `store.delete_document`）。`hard=True` 真的删（误灌文档时用）。

    `on_change`：下线也是**知识库变更**，缓存里引用这份文档的答案同样要失效
    （否则「文档已经下线了，缓存还在照它回答」）。语义与 `ingest_document` 一致：
    只有真的删到了东西（`action="deleted"`）才回调。
    """
    resolved_settings = settings or RagSettings.from_settings()
    active_store = store or RagStore(dimension=resolved_settings.vector_dim)
    counts = await asyncio.to_thread(active_store.delete_document, source, hard=hard)
    report = IngestReport(
        source=source,
        action="deleted" if counts["documents"] else "unchanged",
        chunks_total=0,
        deactivated=int(counts["chunks"]),
        warnings=[] if counts["documents"] else ["库里没有这份文档（source 对不上？）：什么都没做。"],
    )
    if on_change is not None and report.doc_changed:
        try:
            on_change(report)
        except Exception as exc:  # noqa: BLE001 - 缓存侧的问题不该让下线失败
            logger.warning("下线后的 on_change 回调失败（下线本身已成功）：%s: %s", type(exc).__name__, exc)
            report.warnings.append(
                f"知识库变更回调失败（语义缓存可能未失效，靠 TTL 兜底）：{type(exc).__name__}: {exc}"
            )
    return report


@dataclass
class DirectoryIngestReport:
    """一目录文档的入库汇总（`scripts/load_knowledge_base.py` 的输出契约）。"""

    documents: list[IngestReport] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def created(self) -> int:
        return sum(1 for item in self.documents if item.action == "created")

    @property
    def updated(self) -> int:
        return sum(1 for item in self.documents if item.action == "updated")

    @property
    def unchanged(self) -> int:
        return sum(1 for item in self.documents if item.action == "unchanged")

    @property
    def chunks(self) -> int:
        return sum(item.chunks_total for item in self.documents)

    @property
    def embedded(self) -> int:
        return sum(item.embedded for item in self.documents)

    @property
    def elapsed_ms(self) -> float:
        return sum(item.elapsed_ms for item in self.documents)

    def as_dict(self) -> dict[str, Any]:
        return {
            "documents": len(self.documents),
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "chunks": self.chunks,
            "embedded": self.embedded,
            "skipped": list(self.skipped),
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


async def ingest_path(
    root: str | Path,
    *,
    category: str = "general",
    store: RagStore | None = None,
    settings: RagSettings | None = None,
    embedder: Embedder | None = None,
    force: bool = False,
    recursive: bool = True,
    suffixes: Sequence[str] = SUPPORTED_SUFFIXES,
    on_change: Callable[[IngestReport], None] | None = None,
) -> DirectoryIngestReport:
    """把一个目录（或单个文件）灌进知识库：**只处理支持的后缀**，逐个增量入库。

    `on_change` 会**逐份文档**透传给 `ingest_document`（只在那一份真的变了时触发）。
    为什么不做成「整目录只回调一次」：一次灌库可能有多份文档变化，
    而版本推进的语义是「变了就 +1」，每一份都该留下自己的痕迹
    （`bumped_by` 里能看出是哪一份触发的）。多次推进的代价只是版本号变大，
    不影响正确性 —— 缓存是按 `fingerprint` 隔离的，旧的一律不可见。
    """
    path = Path(root)
    resolved_settings = settings or RagSettings.from_settings()
    active_store = store or RagStore(dimension=resolved_settings.vector_dim)

    if path.is_file():
        report = await ingest_document(
            path,
            category=category,
            store=active_store,
            settings=resolved_settings,
            embedder=embedder,
            force=force,
            on_change=on_change,
        )
        return DirectoryIngestReport(documents=[report])

    if not path.exists():
        raise FileNotFoundError(f"知识库目录不存在：{path}")

    pattern = "**/*" if recursive else "*"
    files = sorted(item for item in path.glob(pattern) if item.is_file())
    supported = [item for item in files if item.suffix.lower() in suffixes]
    skipped = [item.as_posix() for item in files if item.suffix.lower() not in suffixes]

    result = DirectoryIngestReport(skipped=skipped)
    for file in supported:
        report = await ingest_document(
            file,
            category=category,
            store=active_store,
            settings=resolved_settings,
            embedder=embedder,
            force=force,
            on_change=on_change,
        )
        result.documents.append(report)
    return result


__all__ = [
    "EMBED_BATCH",
    "DirectoryIngestReport",
    "Embedder",
    "IngestReport",
    "delete_document",
    "document_hash",
    "ingest_document",
    "ingest_path",
    "reindex_document",
]
