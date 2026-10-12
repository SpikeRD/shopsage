"""cache 包的门面：**缓存相关的一切出口**（别的模块只从这里 import）。

```python
from app.cache import lookup, remember                       # 同步（脚本 / 用例）
from app.cache import semantic_cache_get, semantic_cache_set  # 异步（图节点）
from app.cache import result_cache_get, result_cache_set      # 结果缓存
```

## 这一层刻意不做的事

* **不 import FastAPI / SQLAlchemy / redis**（在 `core.py` / `semantic.py` 这一层）。
  `stores.py` 与 `backend.py` 里有 SQL 与 Redis —— 它们是「传输层」，
  而门面只把 `SemanticCache` / `Cache` 两个对象递出去。
  这条纪律有结构用例钉着（`tests/cache/test_cache_contracts.py`），
  理由与第 12 步 `app/queue/__init__.py` 一样：**分层倒了之后，
  想单测判定逻辑就不得不先起一套依赖栈**。
* **不自己写指标**。命中率、耗时、降级、条目数四条指标都写进
  `app/observability` 的注册表（第 11 步定的那一套）——
  「一个量只有一个写入方」这条铁律在这里的落点是
  `observability/metrics.py` 定义名字、`cache/semantic.py` 写值。

## 一次命中省下什么（E1 的账）

| 省下的 | 从哪看出来 |
|---|---|
| 一次检索（向量 + 关键词 + rerank） | `RetrievalResult` 的命中条数不再出现（`rag_node` 直接返回） |
| 一次生成（上千 token） | `shopsage_llm_cost_usd_total` 不增长，而请求数在增长 |
| 首 token 延迟（B2） | `shopsage_request_duration_seconds` 的 P95 掉一个数量级 |

第三行是「缓存放在 LLM 调用之前」这条设计的唯一证据（`13.cache.md` §5 最后一条）。
"""

from app.cache.api import (
    Cache,
    CacheHealth,
    build_cache,
    close_cache,
    default_cache,
    reset_for_tests,
    result_cache_get,
    result_cache_set,
    semantic_cache_get,
    semantic_cache_set,
    set_default_cache,
)
from app.cache.backend import (
    CacheKeySpace,
    MemoryKeyValue,
    RedisEntryStore,
    RedisKeyValue,
    ResilientEntryStore,
    build_key_value,
)
from app.cache.core import (
    OUTCOME_BYPASSED,
    OUTCOME_DISABLED,
    OUTCOME_ERROR,
    OUTCOME_HIT,
    OUTCOME_MISS,
    OUTCOME_STALE,
    PROMPT_VERSION,
    CacheDecision,
    LookupRequest,
    assemble_question,
    build_entry,
    classify_cache_intent,
    decide,
    fingerprint_for,
    is_time_sensitive,
    judge,
)
from app.cache.corpus import (
    MemoryRevisionStore,
    PostgresRevisionStore,
    RevisionStore,
    bump_after_ingest,
    current_revision,
    set_revision_store,
)
from app.cache.result import ResultCacheError, ResultHit, ResultStore
from app.cache.invalidate import (
    invalidate_document,
    invalidate_knowledge_base,
    invalidate_on_kb_change,
    invalidate_scope,
    make_ingest_callback,
    purge_expired,
)
from app.cache.schema import (
    CACHE_TABLES,
    ENTRIES_TABLE,
    VECTOR_DIM,
    VERSIONS_TABLE,
    create_statements,
    drop_statements,
)
from app.cache.semantic import DefaultEmbedder, Embedder, Prepared, SemanticCache, WriteLog
from app.cache.settings import (
    CacheBackendSettings,
    CacheConfig,
    CacheSettings,
    default_config,
)
from app.cache.stores import (
    CacheEntry,
    CacheMatch,
    CacheStoreError,
    EntryStore,
    LookupLog,
    MemoryEntryStore,
    PostgresEntryStore,
    ScopeContext,
    cosine_similarity,
    question_hash,
    scope_for,
)


def lookup(messages, *, user_id: int = 0, role: str = "customer", params=None, **extra) -> Prepared:
    """同步查缓存（`13.cache.md` §7 用例形态：`semantic_cache_get("怎么退货")`）。"""
    return default_cache().lookup(messages, user_id=user_id, role=role, params=params, **extra)


def remember(messages, answer: str, **kwargs) -> WriteLog:
    """同步写缓存（`13.cache.md` §7 用例形态：`semantic_cache_set("怎么退货", answer)`）。"""
    return default_cache().remember(messages, answer, **kwargs)


__all__ = [
    # ── 文档 §2 点名的四个接口 ──────────────────────────────────────────
    "semantic_cache_get",
    "semantic_cache_set",
    "result_cache_get",
    "result_cache_set",
    # ── 同步门面（文档 §7 的用例形态）──────────────────────────────────
    "lookup",
    "remember",
    # ── 实例与装配 ─────────────────────────────────────────────────────
    "Cache",
    "CacheHealth",
    "SemanticCache",
    "Prepared",
    "WriteLog",
    "build_cache",
    "default_cache",
    "set_default_cache",
    "close_cache",
    "reset_for_tests",
    "default_config",
    # ── 配置 ───────────────────────────────────────────────────────────
    "CacheBackendSettings",
    "CacheConfig",
    "CacheSettings",
    # ── 判定（纯函数层）────────────────────────────────────────────────
    "CacheDecision",
    "LookupRequest",
    "OUTCOME_BYPASSED",
    "OUTCOME_DISABLED",
    "OUTCOME_ERROR",
    "OUTCOME_HIT",
    "OUTCOME_MISS",
    "OUTCOME_STALE",
    "PROMPT_VERSION",
    "assemble_question",
    "build_entry",
    "classify_cache_intent",
    "decide",
    "fingerprint_for",
    "is_time_sensitive",
    "judge",
    # ── 存储层 ─────────────────────────────────────────────────────────
    "CacheEntry",
    "CacheMatch",
    "CacheStoreError",
    "EntryStore",
    "LookupLog",
    "MemoryEntryStore",
    "MemoryKeyValue",
    "MemoryRevisionStore",
    "PostgresEntryStore",
    "PostgresRevisionStore",
    "RedisEntryStore",
    "RedisKeyValue",
    "ResilientEntryStore",
    "ResultHit",
    "ResultStore",
    "ResultCacheError",
    "RevisionStore",
    "ScopeContext",
    "CacheKeySpace",
    "cosine_similarity",
    "question_hash",
    "scope_for",
    "build_key_value",
    # ── embedding 与版本 ───────────────────────────────────────────────
    "DefaultEmbedder",
    "Embedder",
    "bump_after_ingest",
    "current_revision",
    "set_revision_store",
    # ── 失效三策略的②与③（①是条目自己的 TTL）────────────────────────────
    "invalidate_document",
    "invalidate_knowledge_base",
    "invalidate_on_kb_change",
    "invalidate_scope",
    "make_ingest_callback",
    "purge_expired",
    # ── DDL（迁移与测试共用）───────────────────────────────────────────
    "CACHE_TABLES",
    "ENTRIES_TABLE",
    "VERSIONS_TABLE",
    "VECTOR_DIM",
    "create_statements",
    "drop_statements",
]
