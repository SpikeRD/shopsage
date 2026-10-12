"""缓存模块的**门面**（`13.cache.md` §2 的对外接口全在这里落地）。

```python
from app.cache import semantic_cache_get, semantic_cache_set
from app.cache import result_cache_get, result_cache_set
```

## 四条 API 线的分工

| 接口 | 谁该用 | 一次调用的代价 |
|---|---|---|
| `semantic_cache_get(messages, user_id=..., role=...)` | 问答链路（`rag_node`） | 2µs~40ms（看走哪一步） |
| `semantic_cache_set(...)` / `Prepared.respond(...)` | 同上（生成完之后） | 一次写入 |
| `result_cache_get/set(kind=..., key=...)` | 报表、聚合、外部只读查询 | 一次 Redis 往返 |
| `lookup` / `remember`（同步） | 脚本、同步代码、演示 | 同上（不经过线程） |

**为什么 `semantic_cache_get` 还有「同步版」**：`13.cache.md` §7 的用例形态是

```python
semantic_cache_set("怎么退货", answer)
assert semantic_cache_get("退货流程是什么") == answer
```

这是**同步**调用（那句用例是文档里的原文，本仓照它实现了同步门面：`lookup` / `remember`）。
异步门面（`semantic_cache_get` / `semantic_cache_set`）是给节点用的 ——
它们把阻塞部分丢进 `asyncio.to_thread`，所以**同一份逻辑两个入口**，
下层（`semantic.SemanticCache`）只实现一遍。

## 连接与生命周期

Redis 客户端与条目存储是**进程级懒建单例**（`default_cache()`），
由 `close_cache()` 释放（FastAPI 的 lifespan 退出时调它）——
与第 12 步 `queue.backend.close_default_queues()` 同一口径：
「谁创建谁释放」，且**必须在 lifespan 里释放**（漏掉的症状是进程退出时挂着的连接）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.cache.backend import (
    CacheKeySpace,
    MemoryKeyValue,
    ResilientEntryStore,
    RedisEntryStore,
    RedisKeyValue,
    build_embedding_cache,
    build_key_value,
)
from app.cache.corpus import RevisionStore, set_revision_store
from app.cache.result import ResultStore
from app.cache.semantic import DefaultEmbedder, Embedder, Prepared, SemanticCache, WriteLog
from app.cache.core import CacheDecision
from app.cache.settings import CacheBackendSettings, CacheConfig, CacheSettings
from app.cache.stores import EntryStore, MemoryEntryStore, LookupLog

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheHealth:
    """一次自检的结论（`/health` 与 `scripts/cache_report.py` 共用）。"""

    enabled: bool
    store: str
    degraded: bool
    entry_store: dict[str, Any]
    result_store: dict[str, Any]
    revisions: dict[str, Any]
    problems: list[str]
    stats: dict[str, int]

    def summary(self) -> str:
        flag = "降级" if self.degraded else "正常"
        head = f"缓存{flag}（存储={self.store}，条目={self.entry_store.get('entries', '?')}）"
        return head if not self.problems else head + f"；问题 {len(self.problems)} 项"


class Cache:
    """把四样东西绑在一起：条目存储、结果缓存、版本表、embedding 适配器。

    **绑在一起**是刻意的（与第 11 步 `Telemetry` 把 provider/metrics/exporter 绑在一起同理）：
    它们必须成套 —— 换了条目存储却没换版本表，就会出现「缓存写了，
    但版本闸永远判失效」这种看起来完全正常的故障。
    """

    def __init__(
        self,
        *,
        store: EntryStore,
        results: ResultStore,
        settings: CacheSettings | None = None,
        revisions: RevisionStore | None = None,
        embedder: Embedder | None = None,
        keys: CacheKeySpace | None = None,
        key_value: Any = None,
        degraded: bool = False,
        key_value_owned: bool = False,
    ) -> None:
        self.settings = settings or CacheSettings()
        self.store = store
        self.results = results
        self.revisions = revisions
        self.keys = keys or CacheKeySpace()
        self.key_value = key_value
        self.degraded = bool(degraded)
        self._kv_owned = bool(key_value_owned)
        self.embedding_cache = (
            build_embedding_cache(key_value, keys=self.keys, ttl_seconds=CacheBackendSettings().embedding_ttl_seconds)
            if key_value is not None
            else None
        )
        self.semantic = SemanticCache(
            store=store,
            embedder=embedder or DefaultEmbedder(),
            revisions=revisions,
            settings=self.settings,
            keys=self.keys,
            embedding_cache=self.embedding_cache,
            degraded=self.degraded,
        )

    # ── 语义缓存（同步）──────────────────────────────────────────────────
    def record(
        self,
        *,
        answer: str,
        prepared: Prepared | None = None,
        question: str = "",
        raw_question: str = "",
        scope: str = "",
        fingerprint: str = "",
        revision: int = 0,
        tier: str = "normal",
        ttl_seconds: int = 0,
        citations: Any = (),
        meta: Any = None,
    ) -> WriteLog:
        """写缓存：**从可序列化的字段重建**（图节点之间的唯一姿势）。

        `prepared=` 与「显式字段」二选一：前者给同进程内的两段式调用方
        （`Cache.remember`），后者给**跨节点**的调用方（`rag_node` 把
        `question_hash` 那几项写进 state，`synthesis` 再喂回来）——
        原因见 `SemanticCache.record` 的说明：`Prepared` 过不了 LangGraph 的序列化。
        """
        if prepared is not None:
            return self.respond(prepared, answer, citations=citations, meta=meta)
        decision = CacheDecision(
            action="cache", tier=str(tier), ttl_seconds=int(ttl_seconds), rule="", reason=""
        )
        return self.semantic.record(
            question=question,
            raw_question=raw_question,
            scope=scope,
            fingerprint=fingerprint,
            revision=int(revision),
            decision=decision,
            answer=answer,
            citations=citations or (),
            meta=meta,
        )

    def lookup(
        self,
        messages: Any,
        *,
        user_id: int = 0,
        role: str = "customer",
        params: Any = None,
        now: float | None = None,
        context_turns: int | None = None,
        **extra: Any,
    ) -> Prepared:
        """同步查（`13.cache.md` §7 用例形态的落点）。

        `params` 是**检索参数**（`top_k` / `min_score` / `embedder_key`），
        它进版本指纹；`now` / `context_turns` 是**调用参数**（不进指纹，见下）。
        ⚠️ `**extra` 只是为了「顺手多传一个参数」时给出**明确报错**而不是静默把它吸进指纹
        —— 那正是本步最贵的坑（见 `semantic.fingerprint_for`）。

        ⚠️ `now` 与 `context_turns` 必须是**显式的具名参数**（而不是走 `**extra`
        或塞进 `params`）：前者是「假装现在是几点」（时间旅行，测试用），
        后者是「拼几轮上下文」——两者都**不改变答案的语义**，
        所以它们都不该进指纹（放进 `params` 会让同一个问题因为测试的时间旅行而算成另一条）。
        """
        if extra:
            raise TypeError(
                f"lookup() 收到未登记的参数 {sorted(extra)}。检索参数必须放进 `params=` "
                "（它是一个 mapping）—— 随手加的关键字会被算进版本指纹，"
                "于是同一组参数在读写两侧算出不同指纹，缓存写了也命中不了。"
            )
        options: dict[str, Any] = {}
        if now is not None:
            options["now"] = now
        if context_turns is not None:
            options["context_turns"] = context_turns
        return self.semantic.prepare(
            self._messages_of(messages),
            user_id=user_id,
            role=role,
            params=params or {},
            **options,
        )

    def remember(
        self,
        messages: Any,
        answer: str,
        *,
        user_id: int = 0,
        role: str = "customer",
        citations: Any = (),
        meta: Any = None,
        params: Any = None,
        **extra: Any,
    ) -> WriteLog:
        """同步写：**内部先查一次**（拿到 scope/fingerprint/决策），再写。

        这就是 `13.cache.md` §7 里 `semantic_cache_set("怎么退货", answer)` 的形状 ——
        一个「问题 + 答案」的写入口。它比 `prepare` + `respond` 多一次查询，
        但省掉了调用方持有中间态的负担；两条路径共用同一个 `SemanticCache`。

        ⚠️ 同一份 `params` **同时给了读写两侧**（`lookup` 拿它算指纹，写的时候再算一次校验）：
        这就是「读用了一组参数、写用了另一组」这件事在类型上不可能发生的落点 ——
        第一版两处各算一次，于是第一次问答写进缓存、第二次问同一个问题**仍然未命中**。
        """
        prepared = self.lookup(messages, user_id=user_id, role=role, params=params, **extra)
        # ⚠️ 走 `respond`（而不是再调一次 `semantic.respond`）：它用的消息是
        # `prepared.context_messages`（读那一刻的快照），与 `prepare` 逐字一致 ——
        # 否则「同一句话两次调用」会因为消息串不同而算出两个缓存键。
        write = self.respond(prepared, answer, citations=citations or (), meta=meta)
        if not write.written and "指纹" in write.reason:  # 上面那道锁不该被触发
            raise AssertionError(f"读写指纹不一致：{write.reason}")
        return write

    def respond(
        self,
        prepared: Prepared,
        answer: str,
        *,
        citations: Any = (),
        meta: Any = None,
        fingerprint: str | None = None,
    ) -> WriteLog:
        """用**读那一刻的上下文**写缓存（`prepare` → `respond` 两段式的写半边）。

        ⚠️ 它写的是 `prepared.context_messages`（读那一刻的 messages 快照），
        **不是**调用方此刻手里的 messages —— 图会在 `synthesis` 之后把这一轮的问答
        追加进历史，用「当前消息」去写会让缓存键与读时不同（写了永远读不到）。
        """
        return self.semantic.respond(
            prepared, answer, citations=citations or (), meta=meta, fingerprint=fingerprint
        )

    # ── 语义缓存（异步）──────────────────────────────────────────────────
    async def semantic_get(
        self, messages: Any, *, user_id: int = 0, role: str = "customer", params: Any = None, **extra: Any
    ) -> Prepared:
        """异步查（节点用）：阻塞部分丢进线程，事件循环不被占住。"""
        return await asyncio.to_thread(
            self.lookup, messages, user_id=user_id, role=role, params=params, **extra
        )

    async def semantic_set(
        self,
        messages: Any,
        answer: str,
        *,
        user_id: int = 0,
        role: str = "customer",
        citations: Any = (),
        meta: Any = None,
        params: Any = None,
        prepared: Prepared | None = None,
        **extra: Any,
    ) -> WriteLog:
        """异步写。

        ⚠️ `prepared=` 是**给图节点用的**那条路（`rag` 读、`synthesis` 写）：
        图会把这一轮的问答追加进 `messages`，所以写的时候必须用
        「读那一刻的消息快照」，而它就在 `prepared.context_messages` 里。
        传了 `prepared` 时 `messages` 被忽略（可以为 `None`）。
        """
        if prepared is not None:
            return await asyncio.to_thread(self.respond, prepared, answer, citations=citations or (), meta=meta)
        return await asyncio.to_thread(
            self.remember,
            messages,
            answer,
            user_id=user_id,
            role=role,
            citations=citations or (),
            meta=meta,
            params=params,
            **extra,
        )

    # ── 结果缓存 ─────────────────────────────────────────────────────────
    def result_get(self, *, kind: str, key: str, now: float | None = None) -> Any:
        hit = self.results.get(kind=kind, key=key, now=now)
        return None if hit is None else hit.value

    def result_set(
        self,
        *,
        kind: str,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
        now: float | None = None,
        meta: Any = None,
    ) -> int:
        ttl = int(ttl_seconds if ttl_seconds is not None else self.settings.ttl_seconds)
        return self.results.set(kind=kind, key=key, value=value, ttl_seconds=ttl, now=now, meta=meta)

    # ── 失效与自检 ───────────────────────────────────────────────────────
    def invalidate(self, *, scope: str | None = None, question: str | None = None, allow_purge: bool = False) -> dict[str, int]:
        """主动失效（`13.cache.md` §5 第 2 条）：删掉具体的问题 / 清空某个 scope。

        **刻意没有「清空全部」的默认动作**：一个「清空缓存」的运维动作代价是
        「接下来一段时间所有问题都重新走检索 + 生成」，它该是一次显式操作
        （`scripts/cache_report.py --purge-all`），不该是一个顺手能按的按钮。
        """
        from app.cache.stores import ScopeContext

        removed = {"entries": 0, "results": 0}
        if scope and question:
            removed["entries"] = int(
                self._safe_store(lambda: self.store.deactivate(ScopeContext(scope=scope, fingerprint=""), question))
            )
        elif scope:
            removed["entries"] = int(self._safe_store(lambda: self.store.deactivate_scope(scope)) or 0)
        if allow_purge:
            removed["entries"] += int(self._safe_store(lambda: self.store.purge(only_expired=False)) or 0)
            removed["results"] = int(self.results.purge())
        return removed

    def health(self, *, config: CacheConfig | None = None) -> CacheHealth:
        resolved = config or CacheConfig.from_settings(cache=self.settings)
        return CacheHealth(
            enabled=bool(self.settings.enabled),
            store=self.store.name(),
            degraded=self.degraded,
            entry_store=self._safe_dict(self.store.health),
            result_store=self.results.state(),
            revisions=self._safe_dict(getattr(self.revisions, "health", lambda: {"store": "injected"})),
            problems=resolved.problems(),
            stats=dict(self.semantic.stats),
        )

    def close(self) -> None:
        """释放连接（lifespan 退出时调）。"""
        kv = self.key_value
        if self._kv_owned and kv is not None:
            close = getattr(kv, "close", None)
            if callable(close):
                close()

    # ── 内部 ─────────────────────────────────────────────────────────────
    @staticmethod
    def _messages_of(messages: Any) -> list[dict[str, Any]]:
        """把几种入参形态归一成消息列表。

        支持三种（**都是为了那句文档用例能照着写**）：
          * `"怎么退货"`（一句字符串）→ `[{"role": "user", ...}]`
          * `[{"role": "user", "content": ...}, ...]`（标准形态）
          * 图里的 state（有 `messages` 键的 dict）→ 取它的 `messages`
        """
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        if isinstance(messages, dict) and "messages" in messages:
            return list(messages.get("messages") or [])
        if isinstance(messages, (list, tuple)):
            return [
                item if isinstance(item, dict) else {"role": "user", "content": str(item)}
                for item in messages
            ]
        return []

    def _safe_store(self, call: Any) -> Any:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - 失效失败不该让调用方崩
            logger.warning("缓存失效操作失败（已忽略）：%s: %s", type(exc).__name__, exc)
            return 0

    def _safe_dict(self, call: Any) -> dict[str, Any]:
        try:
            return dict(call())
        except Exception as exc:  # noqa: BLE001 - 自检不该抛
            return {"error": f"{type(exc).__name__}: {exc}"}


# ── 进程级默认实例（懒建；按「是否已有 Redis 连接」分两档）──────────────────


_DEFAULT: Cache | None = None


def build_cache(
    *,
    config: CacheConfig | None = None,
    embedder: Embedder | None = None,
    store: EntryStore | None = None,
    revisions: RevisionStore | None = None,
    key_value: Any = None,
) -> Cache:
    """按配置造一个缓存实例（**测试、演示、多租户都用它**，不走进程单例）。

    降级矩阵（每一格都实测过，见 runbook §4）：

    | Redis | Postgres | 条目存储 | 相似度查询 | 结果缓存 |
    |---|---|---|---|---|
    | 可用 | 可用 | Postgres | HNSW 索引 | Redis |
    | 可用 | 不可用 | Redis | **客户端全量算**（慢但正确） | Redis |
    | 不可用 | 可用 | Postgres | HNSW 索引 | 进程内存 |
    | 不可用 | 不可用 | 进程内存 | 暴力相似度 | 进程内存 |

    第一列的四种组合**全部可用**是刻意的：缓存的价值是「让主路径更快」，
    所以它自己不存在「不可用」这个状态 —— 只有「快一点」与「慢一点」。
    代价是必须把降级说出来（`degraded` 进指标与 `/health`）。
    """
    resolved = config or CacheConfig.from_settings()
    settings = resolved.cache
    backend = resolved.backend
    keys = CacheKeySpace(namespace=backend.namespace, instance=backend.instance)

    kv = key_value
    kv_degraded = False
    kv_owned = False
    if kv is None:
        kv, kv_degraded = build_key_value(backend, settings)
        kv_owned = True

    entry_store: EntryStore | None = store
    if entry_store is None:
        from app.cache.schema import VECTOR_DIM

        primary: EntryStore | None = None
        try:
            from app.cache.stores import PostgresEntryStore

            candidate = PostgresEntryStore(dimension=VECTOR_DIM)
            health = candidate.health()
            if health.get("exists"):
                primary = candidate
            else:
                logger.warning(
                    "缓存表不存在（%s），本条链路回落。先跑 `uv run alembic upgrade head`。",
                    health.get("table"),
                )
        except Exception as exc:  # noqa: BLE001 - 库不可用不是错误，是降级
            logger.warning("Postgres 不可用（缓存条目回落）：%s: %s", type(exc).__name__, exc)

        if primary is None:
            primary = RedisEntryStore(kv, keys=keys, max_entries=settings.max_entries, settings=settings)
        fallback = MemoryEntryStore(max_entries=settings.max_entries)
        entry_store = ResilientEntryStore(primary, fallback if settings.fallback_to_memory else None)

    results = ResultStore(kv, namespace=backend.namespace, instance=backend.instance, settings=settings)
    degraded = bool(kv_degraded) or bool(getattr(entry_store, "degraded", False))
    return Cache(
        store=entry_store,
        results=results,
        settings=settings,
        revisions=revisions,
        embedder=embedder,
        keys=keys,
        key_value=kv,
        degraded=degraded,
        key_value_owned=kv_owned,
    )


def default_cache() -> Cache:
    """取进程级默认缓存（懒建）。**第一次调用会连 Redis / 探库**。"""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = build_cache()
    return _DEFAULT


def set_default_cache(cache: Cache | None) -> None:
    """替换进程默认缓存（测试与演示用；`None` = 丢弃，下次访问重建）。"""
    global _DEFAULT
    previous = _DEFAULT
    _DEFAULT = cache
    if previous is not None and previous is not cache:
        previous.close()


def close_cache() -> bool:
    """释放默认缓存的连接；返回是否真的关掉了（lifespan 退出时调）。"""
    global _DEFAULT
    if _DEFAULT is None:
        return False
    _DEFAULT.close()
    _DEFAULT = None
    return True


def reset_for_tests(
    *,
    settings: CacheSettings | None = None,
    embedder: Embedder | None = None,
    store: EntryStore | None = None,
    revisions: RevisionStore | None = None,
) -> Cache:
    """造一个**全离线**的缓存并装成进程默认（用例与演示脚本的第一行）。

    为什么它存在（而不是让每个用例自己拼）：`semantic_cache_get` 这类门面函数
    走的是进程默认，而「默认」在测试里必须是可控的 —— 漏一步就会去连真 Redis
    （本仓的 `.env` 里有一个真地址），症状是「用例随机变慢、偶尔红」。
    把「怎么造一个离线默认」写成函数，等于把这个坑封掉。
    """
    cache = build_cache(
        config=CacheConfig(cache=settings or CacheSettings(), backend=CacheBackendSettings(redis_url="")),
        embedder=embedder,
        store=store or MemoryEntryStore(),
        revisions=revisions,
        key_value=MemoryKeyValue(),
    )
    set_default_cache(cache)
    if revisions is not None:
        set_revision_store(revisions)
    return cache


# ── 文档 §2 点名的四个函数（门面 → 进程默认实例）────────────────────────────


def semantic_cache_get(
    messages: Any, *, user_id: int = 0, role: str = "customer", params: Any = None, **extra: Any
) -> Prepared:
    """异步版：`await semantic_cache_get(messages, user_id=..., role=...)` → `Prepared`。

    ⚠️ 它按 `13.cache.md` §2 的名字提供，但**返回的是 `Prepared` 而不是答案字符串**：
    命中时 `prepared.answer` / `prepared.citations` 就是答案与出处，
    未命中时它是「带着写入上下文的空结果」。同步版 `lookup()` 与
    `13.cache.md` §7 用例的 `semantic_cache_get("怎么退货")` 形状完全一致。
    """
    return default_cache().semantic_get(messages, user_id=user_id, role=role, params=params, **extra)


def semantic_cache_set(
    messages: Any,
    answer: str,
    *,
    user_id: int = 0,
    role: str = "customer",
    citations: Any = (),
    meta: Any = None,
    params: Any = None,
    **extra: Any,
) -> WriteLog:
    """异步版：`await semantic_cache_set(messages, answer, user_id=..., role=...)`。"""
    return default_cache().semantic_set(
        messages,
        answer,
        user_id=user_id,
        role=role,
        citations=citations,
        meta=meta,
        params=params,
        **extra,
    )


def result_cache_get(*, kind: str, key: str, now: float | None = None) -> Any:
    """结果缓存读（同步）。键由**调用方**派生（见 `result.py` 的三条口径）。"""
    return default_cache().result_get(kind=kind, key=key, now=now)


def result_cache_set(
    *,
    kind: str,
    key: str,
    value: Any,
    ttl_seconds: int | None = None,
    now: float | None = None,
    meta: Any = None,
) -> int:
    """结果缓存写（同步）；返回写入字节数。"""
    return default_cache().result_set(
        kind=kind, key=key, value=value, ttl_seconds=ttl_seconds, now=now, meta=meta
    )


__all__ = [
    "Cache",
    "CacheHealth",
    "build_cache",
    "close_cache",
    "default_cache",
    "reset_for_tests",
    "result_cache_get",
    "result_cache_set",
    "semantic_cache_get",
    "semantic_cache_set",
    "set_default_cache",
]


def _unused_imports() -> tuple[Any, ...]:
    """（把 `LookupLog` / `RedisKeyValue` 留在这里，说明它们的读者是谁。）

    * `LookupLog` —— `Prepared.log` 的类型；门面把它一起导出，方便调用方类型标注；
    * `RedisKeyValue` —— 「Redis 可用」那一档的键值实现（排障时 `isinstance` 判它）。
    """
    return (LookupLog, RedisKeyValue)
