# 模块 10 · rag（文档解析 / 分块 / 混合检索）

## 目的

把 `docs/kb/` 的 8 份语料变成**可检索、可溯源、可增量更新**的片段，供 `rag` 节点使用。

> ⚠️ **技术选型以代码为准**：本仓 **不使用 LlamaIndex**（`pyproject.toml` 无此依赖、`uv.lock` 零命中），解析/分块/检索/rerank 全部自研。`docs/architecture.md:28` 与 `docs/modules/10.rag.md:20` 的 "LlamaIndex" 说法已作废（见 `../../02-异常清单.md` E-01）。

## 关键结论

### 10.1 链路

```
docs/kb/*  → parse（md/txt/html/pdf）→ chunk（三种策略）→ embed（llm.embed）
           → rag_documents / rag_chunks（pgvector vector(1024) + tsvector）
           → 检索：向量腿（HNSW 余弦） + 关键词腿（n-gram + SQL 内手算 BM25）
           → RRF 融合 → rerank（可选）→ top-k
```

| 阶段 | 实现 | 依据 |
|---|---|---|
| 解析 | `SUPPORTED_SUFFIXES = (.md .markdown .txt .html .htm .pdf)` | `app/rag/parse.py:35` |
| 分块 | `chunk_document()`，含标题栈、FAQ 问答合并、滑动窗口、小块合并；哈希式 `chunk_id_for()` | `app/rag/chunking.py:342`、`:144` |
| 入库 | `ingest_document()` 增量：内容哈希不变 → 不动；变了 → 只重嵌受影响片段 | `app/rag/ingest.py:182` |
| 向量检索 | `search_vector()` 余弦距离升序（能直接吃 HNSW 索引） | `app/rag/store.py:541` |
| 关键词检索 | **不是 `ts_rank`**：n-gram 词表 + `tsvector` + SQL 内手算 BM25（tf/df/N/avgdl） | `app/rag/store.py:9-13`、`:643` |
| 融合 | `reciprocal_rank_fusion()`，`RAG_RRF_K` 默认 60 | `app/rag/retriever.py:159`、`app/config/settings.py:269` |
| 重排 | `build_reranker(mode)`：`off` / `local`（CrossEncoder）/ `api` | `app/rag/rerank.py:229` |
| 增量同步 | `RagSyncWorker` + `scan_documents()` | `app/rag/sync.py:99/81` |

### 10.2 三条容易被忽略的硬约束

1. **维度是写死的**：表上 `vector(1024)`（`alembic/versions/20261010_a1f0c3d5b7e9_*.py:48 VECTOR_DIM = 1024`），必须与 `EMBEDDING_DIM` / `RAG_VECTOR_DIM` 三者一致（`app/rag/store.py:254-262` 有维度自检）。
2. **索引选 HNSW 而不是 IVFFlat**：IVFFlat 需要先有数据才能训练聚类中心，空表建索引会退化；HNSW 空表建索引也正确（迁移文件 :12-13 注释）。
3. **中文全文检索不能用 `to_tsvector('simple', 中文)`**：它不切词。而且 `to_tsvector` 是 **stable 不是 immutable**，做成生成列会得到"有时对有时错"的索引 —— 所以用 n-gram 词表 + 独立维护（迁移文件 :16-19）。

### 10.3 增量索引与缓存的一致性（`issues/010`）

灌库必须**推进 `corpus_revision`**，否则语义缓存里的旧答案永远不会失效。当前实现：
- `app/rag/ingest.py` 补了 `doc_changed` 字段并透传 `on_change`；
- `scripts/load_knowledge_base.py` 调 `bump_after_ingest()`（`app/cache/corpus.py:219`）；
- 内容没变再灌一次 → 版本**不动**（不白清缓存）。

## 实际用法

```python
# 检索（同步/异步两个入口，工具层与节点都用它）
from app.rag import hybrid_search, hybrid_search_async
hits = hybrid_search(query, top_k=5)     # app/rag/retriever.py:491

# 灌一份文档
from app.rag import ingest_document
report = await ingest_document(path=..., embedder=embed)   # app/rag/ingest.py:182
```

日常命令：`uv run python -m scripts.load_knowledge_base`（约 45–82s，8 份文档）。

## 待办 / 已知边界

- ⚠️ **片段数未复核**：文档多处写"8 文档 / 61 片段"，本次未执行灌库（S2），只能确认文档数为 8。→ `../../03-待确认清单.md` Q-07。
- ⚠️ `docs/modules/10.rag.md` §8 的"增量更新可见延迟 167~463ms"是在 **bge-m3 本地后端**时代测的；当前默认后端是 `litellm` + `dashscope/qwen3.7-text-embedding-flash`（`env.list.example:116-118`），换后端后未重新标定（见 `../../02-异常清单.md` E-19）。
- `RAG_RERANK` 默认在 `app/config/settings.py:278` 是 `local`，但 `env.list.example:137` 与 `.env:135` 都设成了 `off` —— 两者不一致是**配置覆盖**而非 bug，但意味着生产默认**不做重排**。

## 来源

- `docs/modules/10.rag.md`（92 行，含 2026-10-10 的旧证据）、`docs/runbook/10.rag-runbook.md`（1167 行）
- `issues/010`、`docs/kb/*`（8 份语料）
- 代码：`app/rag/*.py`；测试：`tests/rag/*.py`
