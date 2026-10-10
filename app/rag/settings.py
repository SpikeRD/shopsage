"""RAG 配置面：把 `Settings` 里的 `rag_*` 收成一个不可变对象。

为什么单独一层而不是到处 `get_settings().rag_candidates`：

1. **一处默认值**。检索层要在「没配 RAG_* 的环境」里也能跑（`.env` 是后来才加的这些键），
   散着写 getattr 必然出现某处回退到 20、另一处回退到 30。
2. **可注入**。测试要固定 `rerank=off` / `candidates=5` 这种极端参数，
   传一个 `RagSettings` 比 monkeypatch 环境变量干净得多（也不受 `get_settings` 的 lru_cache 影响）。
3. **启动期能校验**。`rag_vector_dim` 与 `embedding_dim` 不一致是**静默事故**
   （维度相同但向量空间不相容时，检索看着正常、结果全是乱的），所以在这里对账。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from app.config import get_settings

logger = logging.getLogger(__name__)

RerankMode = Literal["off", "local", "api"]

# 与 `10.rag.md` 第 3 节对齐的三个字段：backend / model / dim。
# backend 直接复用第 5 步的 `EMBEDDING_BACKEND`（embed() 的门面），所以这里只跟 model / dim 对账。
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"
# bge 系 reranker 的经验阈值：sigmoid(logit) 低于它基本是「不相关」。
# 不做硬过滤（会让 top-k 变少），只进 metadata 供上层调节。
DEFAULT_RERANK_MIN_SCORE = 0.0


@dataclass(frozen=True)
class RagSettings:
    """检索层的全部可调项（不可变：跑一轮检索的过程中参数不许漂移）。"""

    vector_dim: int = 0
    candidates: int = 30
    rrf_k: int = 60
    keyword_min_ratio: float = 0.6
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    rerank: RerankMode = "local"
    rerank_model: str = DEFAULT_RERANK_MODEL
    rerank_api_base: str = ""
    rerank_api_key: str = ""
    max_top_k: int = 20
    chunk_max_chars: int = 600
    chunk_overlap: int = 80
    # 溯源用的来源文档根目录（相对路径展示与 doc_id 生成都基于它）
    knowledge_dir: str = "docs/kb"

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "RagSettings":
        """从全局 `Settings` 取一份快照，缺键时用本函数的默认值兜底。

        用 `getattr(..., 默认)` 而不是 `settings.rag_candidates`：`RagSettings` 要能在
        「`.env` 还没加 RAG_* 的旧环境」里工作（首次落地时的真实场景），
        少一个键就 AttributeError 会让整条检索路径不可用。
        """
        resolved = settings if settings is not None else get_settings()

        def pick(name: str, default: Any) -> Any:
            value = getattr(resolved, name, None)
            if value is None:
                return default
            if isinstance(value, str) and not value.strip():
                return default
            return value

        dim = int(pick("rag_vector_dim", 0))
        embedding_dim = int(pick("embedding_dim", 0))
        if dim and embedding_dim and dim != embedding_dim:
            # 不抛：抛了会让「只想改一处」的人整条链路不可用。真正需要 fail-fast 的是
            # 「向量维度 vs 建表维度」，那一条在 store 写库时由数据库自己拦（vector(n) 类型检查）。
            logger.warning(
                "RAG_VECTOR_DIM=%d 与 EMBEDDING_DIM=%d 不一致：向量的真实维度由 embedding 决定，"
                "而向量表的 vector(n) 由迁移决定 —— 两者不一致会在写库时报错（更好）"
                "或在「维度恰好相同但空间不相容」时静默出错（最坏）。请把两处与迁移改成同一个值。",
                dim,
                embedding_dim,
            )

        overlap = int(pick("rag_chunk_overlap", 80))
        max_chars = int(pick("rag_chunk_max_chars", 600))
        if overlap >= max_chars:
            # overlap >= max_chars 会让「按段落累加到 max_chars」的循环永远不前进（死循环）。
            # 这里夹住并告警：配置错不该变成挂死。
            overlap = max(0, max_chars // 2)
            logger.warning(
                "RAG_CHUNK_OVERLAP(%s) >= RAG_CHUNK_MAX_CHARS(%d)：重叠已夹到 %d，"
                "否则分块循环会不前进。",
                pick("rag_chunk_overlap", 80),
                max_chars,
                overlap,
            )

        api_key = pick("rag_rerank_api_key", "")
        # SecretStr → 明文（只在这一层解包，别的地方拿不到它）
        api_key = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else str(api_key)

        return cls(
            vector_dim=dim,
            candidates=int(pick("rag_candidates", 30)),
            rrf_k=int(pick("rag_rrf_k", 60)),
            keyword_min_ratio=float(pick("rag_keyword_min_ratio", 0.6)),
            bm25_k1=float(pick("rag_bm25_k1", 1.2)),
            bm25_b=float(pick("rag_bm25_b", 0.75)),
            rerank=str(pick("rag_rerank", "local")),  # type: ignore[arg-type]
            rerank_model=str(pick("rag_rerank_model", DEFAULT_RERANK_MODEL)),
            rerank_api_base=str(pick("rag_rerank_api_base", "")),
            rerank_api_key=api_key,
            max_top_k=int(pick("rag_max_top_k", 20)),
            chunk_max_chars=max_chars,
            chunk_overlap=overlap,
            knowledge_dir=str(pick("knowledge_dir", "docs/kb")),
        )

    def effective_vector_dim(self, embedding_dim: int = 0) -> int:
        """本进程真正会写进库的向量维度：显式配置优先，否则跟随 embedding 配置。"""
        return self.vector_dim or embedding_dim


__all__ = ["DEFAULT_RERANK_MIN_SCORE", "DEFAULT_RERANK_MODEL", "RagSettings", "RerankMode"]
