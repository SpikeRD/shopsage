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

# embedding 的批大小：一次请求里塞太多文本会被厂商的 token 上限截断（litellm 返回 400），
# 而本地后端在超大 batch 上会吃满内存。32 是本仓实测的稳妥值（1024 维 bge-m3，单条 ~600 字）。
EMBED_BATCH = 32


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
) -> IngestReport:
    """把一份文档增量灌入知识库（`10.rag.md` 第 2 节的 `ingest_document`）。

    参数：
        path:     文件路径（也是溯源里的 `source` —— 用相对仓库根的路径最稳定）。
        content:  直接给内容（测试与「内存里的文档」用）；`None` 时读文件。
        category: 文档类目（政策/手册/FAQ…），检索时可按类目过滤。
        strategy: 强制分块策略（`None` = 按 `RAG_CHUNK_*` 与内容自动判断）。
        force:    即使文档指纹没变也走一遍 upsert（默认 `False` —— 这才是「增量」）。
        now:      注入时间戳（测试断言 `updated_at` 用；`None` = 数据库 `now()`）。

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
) -> IngestReport:
    """下线一份文档（`10.rag.md` 第 2 节的 `delete_document`）。

    默认**软删**：片段标 `is_active=false`、文档标无效 —— 引用还在、检索不再可见
    （理由见 `store.delete_document`）。`hard=True` 真的删（误灌文档时用）。
    """
    resolved_settings = settings or RagSettings.from_settings()
    active_store = store or RagStore(dimension=resolved_settings.vector_dim)
    counts = await asyncio.to_thread(active_store.delete_document, source, hard=hard)
    return IngestReport(
        source=source,
        action="deleted" if counts["documents"] else "unchanged",
        chunks_total=0,
        deactivated=int(counts["chunks"]),
        warnings=[] if counts["documents"] else ["库里没有这份文档（source 对不上？）：什么都没做。"],
    )


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
) -> DirectoryIngestReport:
    """把一个目录（或单个文件）灌进知识库：**只处理支持的后缀**，逐个增量入库。"""
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
