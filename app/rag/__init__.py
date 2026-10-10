"""RAG 检索层（第 10 步）：**知识库怎么存、怎么查**。

边界（`10.rag.md` 第 1 节）：本包只管「解析 → 分块 → embedding → 索引 → 混合检索 + rerank + 溯源」，
**不管**「拿到片段之后怎么合成答案」—— 那是 `app/agents/rag.py` 的事。

对外出口（其余都是内部细节）：

    from app.rag import hybrid_search, ingest_document, reindex_document, delete_document

    hits = hybrid_search(query="退货政策", top_k=5)
    hits[0].source, hits[0].chunk_id, hits[0].text      # 每条都带溯源

`app/tools/retriever.py` 是这些出口的**上层适配器**（归一化字段、强制溯源、收敛 top_k + 审计）：
分层理由是「工具的契约要稳定，而检索算法要能换」—— 两者混在一个模块里，
换检索算法就会动到工具契约（第 6 步的测试钉住的是契约，不该跟着算法一起改）。

⚠️ 本模块**不在 import 期做任何重活**：不连数据库、不加载模型、不读 .env 之外的东西。
`app/tools/retriever.py` 会在**第一次工具调用时**才 import 本包，
所以「rag 包 import 变慢」会直接变成「第一次问知识库变慢」。
"""

from __future__ import annotations

from app.rag.ingest import (
    DirectoryIngestReport,
    IngestReport,
    delete_document,
    ingest_document,
    ingest_path,
    reindex_document,
)
from app.rag.parse import PAGE_BREAK, ParseError, ParsedDocument, parse_text
from app.rag.retriever import (
    RetrievalHit,
    RetrievalResult,
    Retriever,
    default_retriever,
    hybrid_search,
    hybrid_search_async,
    reset_default_retriever,
    vector_only_search,
)
from app.rag.settings import RagSettings
from app.rag.store import RagStore, StoreError
from app.rag.sync import RagSyncWorker, SyncReport

__all__ = [
    "DirectoryIngestReport",
    "IngestReport",
    "PAGE_BREAK",
    "ParseError",
    "ParsedDocument",
    "RagSettings",
    "RagStore",
    "RagSyncWorker",
    "RetrievalHit",
    "RetrievalResult",
    "Retriever",
    "StoreError",
    "SyncReport",
    "default_retriever",
    "delete_document",
    "hybrid_search",
    "hybrid_search_async",
    "ingest_document",
    "ingest_path",
    "parse_text",
    "reindex_document",
    "reset_default_retriever",
    "vector_only_search",
]
