"""传输层（Redis）：**完全同问的快路径 + embedding 缓存 + 降级回落**。

## 这一层在缓存链路里的位置（以及为什么不能没有它）

```
  semantic_cache_get(question)
        │
        ├─① Redis 的「完全同问」快路径 ── 命中 → 直接返回（~0.2ms，不碰数据库、不算向量）
        │      ↑ 用户刷新页面、同一条 FAQ 被反复点开的落点
        │
        ├─② 问题的 embedding（**先查 Redis 的 embedding 缓存**，miss 才算）
        │      ↑ 一句话的向量在同一进程/同一 Redis 上算一次就够
        │
        └─③ Postgres 的相似度查询（HNSW）── 语义命中的落点（见 `stores.py` 的说明）
```

三条路径的成本差一个数量级（0.2ms / 30ms 本地模型 / 2ms 数据库），
所以**顺序不能换**：把 ① 放在最前面，最常见的那类重复问题就完全不走后面两步。

## 为什么同步客户端（而不是 `redis.asyncio`）

第 12 步的队列用 `redis.asyncio`，因为它整个是异步链路（arq 就是异步的）。
本模块不同：入口 `semantic_cache_get` / `result_cache_get` 既被同步代码调用、
也被异步节点调用，而**只有异步调用方**能 `await` 异步客户端。
统一用同步客户端 + `asyncio.to_thread`（在 `api.py` 里）有两个好处：

1. 同一个后端对象能被两条路径共用（不必维护两套连接池）；
2. **不绑定事件循环** —— 第 12 步的 `backend.py` 为「跨事件循环复用连接池」
   付过代价（`RuntimeError: Event loop is closed`，症状是「单跑绿、全量红」），
   而同步连接池没有这个问题。代价是一次线程切换（约 0.1ms）。

## 降级：**绝不静默**

`ResilientEntryStore` 把「Redis 挂了」这件事分成三层表达，缺一层都会变成静默故障：

| 表达 | 落点 | 谁看 |
|---|---|---|
| WARNING 日志 | 状态翻转时打一次（**不是每次请求**） | 值班人 |
| `LookupLog.degraded=True` | 每次查询的账目 | 演示脚本 / 排障 |
| `metrics.CACHE_DEGRADED` + `/health` 的 `cache` 组件 | 长期趋势 | 看板 |

⚠️ 「状态翻转时打一次」是刻意的：Redis 挂掉时每个请求都打 WARNING 会让日志变成
一片噪音，而噪音里的第一条和第一万条**读起来一样**。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from typing import Any, Protocol

from app.cache.settings import CacheBackendSettings, CacheSettings
from app.cache.stores import (
    CacheEntry,
    CacheMatch,
    EntryStore,
    MemoryEntryStore,
    ScopeContext,
    question_hash,
)

logger = logging.getLogger(__name__)

#: Redis 里 key 的形态：`cache:<instance>:entry:<scope>|<fingerprint>|<question_hash>`。
#:
#: ⚠️ `scope` 与 `fingerprint` 之间用 `|` 而不是 `:` —— `scope` 自己含冒号
#: （`customer:2`），而 `|` 被 `scope_for()` 排除在外（角色名来自白名单、
#: user_id 是整数）。**分隔符不能有歧义**：`customer:2` + `x` 与 `customer` + `2:x`
#: 会拼出同一个 key，那是一起跨用户命中。
SEPARATOR = "|"

#: 索引（LRU 淘汰用）的 key 后缀
INDEX_SUFFIX = ":__index"

#: 键值层的 TTL 相对条目 TTL 多出来的秒数（**唯一的定义处**）。
#:
#: 为什么键值层的 TTL 必须比应用层的长：两层都在判过期，而**应用层必须先发生** ——
#: 那样「条目还在、只是过期了」这条路径可被观测（`judge` 会给 `stale` 与它的年龄），
#: 而反过来（key 先消失）就只能看到「未命中」，两种状态在排障时无法区分。
#: 这个常量同时被 `RedisKeyValue.set` 与测试读，所以它只能有一处。
KEY_VALUE_TTL_GRACE_SECONDS: int = 60


def key_value_ttl(entry_ttl_seconds: int, *, grace_seconds: int = KEY_VALUE_TTL_GRACE_SECONDS) -> int:
    """条目的 TTL → 键值层的 TTL（`RedisKeyValue.set` 与用例共用的唯一算法）。

    `grace_seconds` 可覆盖：真 Redis 的用例要**看得见过期**（不能等 60 秒），
    所以它传 1 —— 而生产口径由那个常量定死。
    """
    return max(1, int(entry_ttl_seconds) + int(grace_seconds))


class KeyValue(Protocol):
    """最小键值契约（Redis 与内存实现都满足它）。

    只有四个方法：**这一层不知道条目长什么样**（那是 `EntryStore` 的事）。
    让 Redis 只看到「一串 key → 一个字符串」，是为了让 key 的构造只有一处
    （`CacheKeySpace`），而它的正确性可以被一条用例逐字钉住。
    """

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, *, ttl_seconds: int, grace_seconds: int = 0) -> None: ...

    def delete(self, *keys: str) -> int: ...

    def ping(self) -> bool: ...


class EmbeddingCache(Protocol):
    """「一段文本 → 它的向量」的缓存契约。

    它是**可选的加速器**（`build_embedding_cache` 造它，`semantic.SemanticCache` 用它）：
    算一次 embedding 是本链路最贵的一步（本地 bge-m3 实测 15~40ms、API 后端还要算 token 钱），
    而同一个问题在一段时间里会被反复问到 —— 命中这条缓存等于把 ④ 那一步从 40ms 变成 0.3ms。

    ⚠️ 它的任何失败都**只影响速度、不影响正确性**（最坏情况就是重算一遍），
    所以实现里一律吞异常并计数 —— 调用方不该为它写 `try`。
    """

    def get(self, *, model: str, text: str) -> list[float] | None: ...

    def set(self, *, model: str, text: str, vector: Sequence[float]) -> None: ...

    def state(self) -> dict[str, int]: ...


class MemoryKeyValue:
    """内存键值（离线用例、Redis 缺失时的回落；**在进程重启后清空**）。"""

    def __init__(self) -> None:
        #: key → (值, 过期时刻 或 None)
        self._data: dict[str, tuple[str, float | None]] = {}
        self.reads = 0
        self.writes = 0

    def get(self, key: str) -> str | None:
        self.reads += 1
        item = self._data.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at is not None and time.time() >= expires_at:
            # 惰性过期（与 Redis 的语义一致：**读到过期的就当作不存在**）
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: str, *, ttl_seconds: int, grace_seconds: int = 0) -> None:
        """与 `RedisKeyValue.set` 同签名（含 `grace_seconds`）。

        ⚠️ 内存实现**不忽略 `grace_seconds`**：它是「Redis 是兜底 GC、应用层先判过期」
        这条口径的一部分，而两条后端的行为必须一致 —— 否则「离线用例通过、
        真 Redis 上不通过」（或反过来），而那正是最难查的一类差异。
        """
        self.writes += 1
        ttl = max(1, int(ttl_seconds) + int(grace_seconds))
        self._data[key] = (value, time.time() + ttl)

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._data.pop(key, None) is not None:
                removed += 1
        return removed

    def ping(self) -> bool:
        return True

    def scan_keys(self, pattern: str, *, limit: int = 5000) -> list[str]:
        """按通配符扫描 key（与 `RedisKeyValue.scan_keys` 同签名）。

        为什么内存实现也要有它：`ResultStore.purge` 与 `RedisEntryStore.count`
        都靠「扫描」实现，而**降级路径**（Redis 不可用 → 内存）也会用到它们。
        内存没有它的话，降级之后 `--purge` 会静默变成「什么都不做」——
        一个「以为清掉了其实没清」的运维动作，比直接报错危险得多。

        支持 `*` 通配（`fnmatch` 语义），够用：本仓的 pattern 只有
        `prefix:*` 与 `prefix:<kind>:*` 两种形态，都不涉及 `?` / `[]`。
        """
        import fnmatch

        found: list[str] = []
        for key in list(self._data):
            if fnmatch.fnmatchcase(key, pattern):
                found.append(key)
                if len(found) >= limit:
                    break
        return found

    def clear(self) -> None:
        self._data.clear()

    @property
    def size(self) -> int:
        return len(self._data)


class RedisKeyValue:
    """Redis 键值（同步客户端 + 连接池）。

    ⚠️ 客户端的默认 socket 超时是 `None`（= 永不超时）。这在本模块是**不可接受**的：
    缓存的意义是让主路径更快，而一个卡住的 Redis 会把「更快」变成「全挂」。
    所以超时由 `CacheSettings.lookup_timeout_seconds` 显式给，且 `retry_on_timeout=False`
    （重试由调用方按「未命中」处理 —— 缓存不负责重试）。
    """

    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 0.5,
        client: Any = None,
        decode_responses: bool = True,
    ) -> None:
        self.url = url
        self.timeout_seconds = float(timeout_seconds)
        if client is not None:
            self._client = client
            self._owns_client = False
            return
        import redis

        self._client = redis.Redis.from_url(
            url,
            decode_responses=decode_responses,
            socket_timeout=self.timeout_seconds,
            socket_connect_timeout=self.timeout_seconds,
            retry_on_timeout=False,
            health_check_interval=30,
        )
        self._owns_client = True

    @property
    def client(self) -> Any:
        return self._client

    def get(self, key: str) -> str | None:
        raw = self._client.get(key)
        if raw is None:
            return None
        return raw if isinstance(raw, str) else raw.decode("utf-8")

    def set(self, key: str, value: str, *, ttl_seconds: int, grace_seconds: int = 0) -> None:
        """写一个 key；`grace_seconds` 是**相对应用层 TTL 的宽限**（见 `key_value_ttl`）。

        ⚠️ 这里**不再自己加宽限**（本仓实测踩到的一次双重变换）：
        第一版 `RedisKeyValue.set` 无条件做 `ttl + 60`，而 `RedisEntryStore.put`
        又调了一次 `key_value_ttl(...)` —— 于是 `ttl=0` 的条目在 Redis 里活了 **61 秒**，
        而应用层以为它 1 秒就该过期。症状是「`ttl_seconds` 配置改了但缓存还是活很久」。
        现在宽限由**调用方**通过 `grace_seconds` 显式指定（默认 0 = 不加），
        所以「加了几次」在代码里数得出来。
        """
        ttl = max(1, int(ttl_seconds) + int(grace_seconds))
        self._client.set(key, value, ex=ttl)

    def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(self._client.delete(*keys) or 0)

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:  # noqa: BLE001 - 探活失败只回答 False
            return False

    def scan_keys(self, pattern: str, *, limit: int = 5000) -> list[str]:
        """按模式扫描 key（**用 `scan_iter` 而不是 `keys`**）。

        `KEYS` 在大实例上会阻塞整个 Redis（它的复杂度是 O(N) 且单线程执行）——
        而「清一下缓存」这个运维动作不该让业务抖一下。
        """
        found: list[str] = []
        for key in self._client.scan_iter(match=pattern, count=200):
            found.append(key if isinstance(key, str) else key.decode("utf-8"))
            if len(found) >= limit:
                logger.warning("扫描 key 达到上限 %d（pattern=%s）：结果可能不完整", limit, pattern)
                break
        return found

    def close(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不该让调用方崩
                logger.debug("关闭 Redis 连接时出错（忽略）：%s", exc)


# ── key 的构造：**唯一一处** ─────────────────────────────────────────────────


class CacheKeySpace:
    """把 `(scope, fingerprint, question)` 变成 Redis key —— 且只有这一处能构造。

    为什么值得一个类（而不是三处 `f"{...}"`）：key 的形态是**跨进程契约**。
    队列那一层为同一件事付过代价（`arq:` 前缀 + `q_` job_id 前缀各写一处，
    见 `12.queue-runbook.md` §2.2），而缓存更敏感 —— key 撞车 = 把 A 的答案给 B。
    """

    def __init__(self, *, namespace: str = "cache", instance: str = "default") -> None:
        self.namespace = str(namespace or "cache")
        self.instance = str(instance or "default")

    def entry(self, context: ScopeContext, question: str) -> str:
        """`cache:<instance>:entry:<scope>|<fingerprint>|<question_hash>`。

        ⚠️ `scope` 与 `fingerprint` 之间用 `|` 而不是 `:`（第一条注释里的理由），
        所以整条 key 长这样：`cache:dev:entry:customer:2|a1b2c3d4|9f8e7d6c`
        —— `scope` 内部的冒号与层级分隔符**长得一样**，这是刻意的：
        Redis 的 key 没有层级语义，点号/冒号只是习惯；
        而「分隔符在 scope 里也出现」这件事没有歧义，因为 `scope` 一定是第 4 段之后、
        到第一个 `|` 为止的全部内容（解析规则只有一条）。
        """
        return SEPARATOR.join(
            [
                f"{self.namespace}:{self.instance}:entry:{context.scope}",
                str(context.fingerprint),
                question_hash(question),
            ]
        )

    def index(self, context: ScopeContext) -> str:
        return self.entry(context, "") + INDEX_SUFFIX

    def scope_pattern(self, scope: str) -> str:
        """某个 scope 的全部条目（`deactivate_scope` / 计数用）。

        ⚠️ **必须带上 `entry:` 这一段**（第一版漏了）：少了它的通配是
        `cache:dev:customer:2*`，它**什么也匹配不到**（真实 key 里 `entry:` 在 scope 之前）——
        而症状是「按 scope 清缓存」静默地什么都不做（返回 0），
        看起来像「这个用户本来就没缓存」。
        """
        return f"{self.namespace}:{self.instance}:entry:{scope}{SEPARATOR}*"

    def entry_pattern(self) -> str:
        """本实例的全部条目（`purge` / 计数用）。"""
        return f"{self.namespace}:{self.instance}:entry:*"

    def embedding(self, *, model: str, text: str) -> str:
        """embedding 缓存的 key：**文本 hash 进 key**，文本本身不进（key 会进日志/metrics）。

        `model` 必须在 key 里：换 embedding 模型会让旧向量与新查询不在同一个空间，
        而它们的 key 若相同，症状就是「换了模型之后缓存开始返回乱答案」。
        """
        import hashlib

        digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:32]
        return f"{self.namespace}:{self.instance}:embed:{str(model)}:{digest}"

    def prefix(self) -> str:
        """本命名空间的全部 key 前缀（`purge` 用 `scan_iter` 扫它）。"""
        return f"{self.namespace}:{self.instance}:"


# ── Redis 支撑的 EntryStore ─────────────────────────────────────────────────


class RedisEntryStore:
    """把条目放进 Redis（JSON）+ 一个 ZSET 索引（LRU 淘汰 + 计数）。

    它在链路里的角色是「**完全同问的快路径**」：
    `get()` 是 O(1) 的 Redis GET（0.2ms 级），而 `nearest()` 要把同一 scope 的条目
    全拉回来在客户端算余弦 —— 那在几千条时会变成一次几十毫秒的网络往返。

    所以一个刻意的取舍：**`nearest()` 在这里是「尽力而为」的**。
    它被 `api.py` 用在「Redis 可用但 Postgres 不可用」这条降级路径上
    （见 `README`/`runbook` 的降级矩阵），此时缓存仍在工作、只是慢一点。
    真正的主力相似度查询是 `PostgresEntryStore.nearest()`（HNSW 索引）。
    """

    def __init__(
        self,
        key_value: KeyValue,
        *,
        keys: CacheKeySpace | None = None,
        max_entries: int = 2000,
        settings: CacheSettings | None = None,
        grace_seconds: int = KEY_VALUE_TTL_GRACE_SECONDS,
    ) -> None:
        self.kv = key_value
        self.keys = keys or CacheKeySpace()
        self.max_entries = max(1, int(max_entries))
        self.settings = settings or CacheSettings()
        #: 键值层 TTL 相对条目 TTL 的宽限（见 `KEY_VALUE_TTL_GRACE_SECONDS`）。
        #: 只有「真 Redis 的过期用例」会调小它，生产口径就是那个默认值。
        self.grace_seconds = int(grace_seconds)
        self.writes = 0
        self.evictions = 0

    def name(self) -> str:
        return "redis"

    def get(self, context: ScopeContext, question: str) -> CacheEntry | None:
        raw = self.kv.get(self.keys.entry(context, question))
        if raw is None:
            return None
        try:
            return CacheEntry.from_dict(json.loads(raw))
        except (ValueError, KeyError, TypeError) as exc:
            # 反序列化失败 = 这条 key 的内容坏了（人工改过？旧格式？）：
            # 删掉它比每次请求都失败一次好 —— 但要留日志。
            logger.warning("缓存条目反序列化失败，已删除该 key：%s", exc)
            self.kv.delete(self.keys.entry(context, question))
            return None

    def nearest(
        self, context: ScopeContext, embedding: Sequence[float], *, limit: int = 5
    ) -> list[CacheMatch]:
        from app.cache.stores import cosine_similarity

        scored: list[CacheMatch] = []
        scan = getattr(self.kv, "scan_keys", None)
        if not callable(scan):
            return []
        # `scope_pattern` 通配的正是「同 scope 同指纹」那一段的前缀
        # （指纹在 key 里紧跟在 scope 之后，所以前缀 = `...entry:<scope>|<fingerprint>|`）
        prefix = self.keys.entry(context, "")
        head = prefix.rsplit(SEPARATOR, 1)[0] + SEPARATOR
        for key in scan(f"{head}*"):
            raw = self.kv.get(key)
            if raw is None:
                continue
            try:
                entry = CacheEntry.from_dict(json.loads(raw))
            except (ValueError, KeyError, TypeError):
                continue
            scored.append(CacheMatch(similarity=cosine_similarity(embedding, entry.embedding), entry=entry))
        scored.sort(key=lambda item: (-item.similarity, -item.entry.created_at))
        return scored[: max(1, int(limit))]

    def put(self, entry: CacheEntry) -> bool:
        key = self.keys.entry(
            ScopeContext(scope=entry.scope, fingerprint=entry.fingerprint), entry.question
        )
        existed = self.kv.get(key) is not None
        # ⚠️ 这里**不再**自己算宽限（第一版调了 `key_value_ttl(...)`，而 `set` 里又加了一次
        # → `ttl=0` 的条目在 Redis 里活了 61 秒）。宽限只走 `grace_seconds` 这一个参数，
        # 「加了几次」在代码里数得出来。
        self.kv.set(
            key,
            json.dumps(entry.as_dict(), ensure_ascii=False),
            ttl_seconds=int(entry.ttl_seconds),
            grace_seconds=self.grace_seconds,
        )
        self.writes += 1
        self._maybe_evict(ScopeContext(scope=entry.scope, fingerprint=entry.fingerprint))
        return not existed

    def touch(self, entry: CacheEntry) -> None:
        updated = entry.with_hit()
        key = self.keys.entry(
            ScopeContext(scope=entry.scope, fingerprint=entry.fingerprint), entry.question
        )
        # 剩余寿命 = 原到期时刻 - 现在（**沿用原来那个「应用层 TTL + 宽限」的口径**）
        remaining = int(max(1.0, updated.expires_at - time.time()))
        self.kv.set(
            key,
            json.dumps(updated.as_dict(), ensure_ascii=False),
            ttl_seconds=remaining,
            grace_seconds=self.grace_seconds,
        )

    def deactivate(self, context: ScopeContext, question: str) -> bool:
        return self.kv.delete(self.keys.entry(context, question)) > 0

    def deactivate_scope(self, scope: str) -> int:
        scan = getattr(self.kv, "scan_keys", None)
        if not callable(scan):
            return 0
        keys = scan(self.keys.scope_pattern(scope))
        return self.kv.delete(*keys) if keys else 0

    def purge(self, *, limit: int | None = None, only_expired: bool = True) -> int:
        """清过期条目。

        ⚠️ Redis 自己会把过期的 key 删掉（`set(ex=...)`），所以这个方法的实际作用是
        「把**应用层判过期**的条目也清掉」—— 两者的 TTL 差了 60 秒（见 `RedisKeyValue.set`），
        于是确实存在一个窗口：条目已经不该被命中，但 key 还在。
        `only_expired=False` 时清掉整个命名空间（那是一次**运维动作**，见 CLI）。
        """
        scan = getattr(self.kv, "scan_keys", None)
        if not callable(scan):
            return 0
        keys = scan(self.keys.entry_pattern())
        if only_expired:
            now = time.time()
            alive: list[str] = []
            for key in keys:
                raw = self.kv.get(key)
                if raw is None:
                    continue
                try:
                    entry = CacheEntry.from_dict(json.loads(raw))
                except (ValueError, KeyError, TypeError):
                    continue
                if entry.is_expired(now=now):
                    alive.append(key)
            keys = alive
        if limit is not None:
            keys = keys[: max(0, int(limit))]
        return self.kv.delete(*keys) if keys else 0

    def count(self, *, scope: str | None = None, only_active: bool = True) -> int:
        scan = getattr(self.kv, "scan_keys", None)
        if not callable(scan):
            return 0
        pattern = self.keys.scope_pattern(scope) if scope else self.keys.entry_pattern()
        keys = scan(pattern)
        if not only_active:
            return len(keys)
        now = time.time()
        total = 0
        for key in keys:
            raw = self.kv.get(key)
            if raw is None:
                continue
            try:
                entry = CacheEntry.from_dict(json.loads(raw))
            except (ValueError, KeyError, TypeError):
                continue
            if not entry.is_expired(now=now):
                total += 1
        return total

    def health(self) -> dict[str, Any]:
        return {
            "store": "redis",
            "url_scheme": self.keys.namespace,
            "entries": self.count(),
            "ping": self.kv.ping(),
            "namespace": self.keys.prefix(),
        }

    def _maybe_evict(self, context: ScopeContext) -> None:
        """按 scope 的条目数做 LRU（超了删最老的）。

        ⚠️ 只做**尽力而为**的淘汰：它需要一次 `scan`（O(N)），所以不是每次写入都跑
        （按 `max_entries` 的 10% 触发）。Redis 的 TTL 才是主力 GC ——
        这里防的是「一时涌入大量不同的问题，把实例撑爆」。
        """
        total = self.count(scope=context.scope)
        if total <= self.max_entries:
            return
        overflow = total - self.max_entries
        removed = self.purge(limit=overflow, only_expired=False)
        if removed:
            self.evictions += removed
            logger.info("Redis 缓存超出上限，淘汰了 %d 条（scope=%s）", removed, context.scope)


# ── 装饰器：降级与状态表达 ───────────────────────────────────────────────────


class ResilientEntryStore:
    """把一个 `EntryStore` 包成「出错就回落」，并**把降级这件事说出来**。

    三种结果分开表达（这是本模块最容易被写错的一处）：

    | 情况 | 行为 | 为什么 |
    |---|---|---|
    | 主存储抛错 | 切到 `fallback`，`active` 变成 "fallback" | 缓存不可用不该让请求失败（fail-open） |
    | 主存储恢复 | 切回主存储，`active` 变回原名 | 不切回去 = 永久降级到内存（重启前都不再用 Redis） |
    | 主存储正常 | 原样透传 | —— |

    ⚠️ 「切回去」这件事值得单独一次判断：本仓最典型的事故形态是
    **一次抖动之后永久降级** —— 业务看着完全正常，只有命中率悄悄掉了一个数量级
    （内存缓存是每个进程一份，多副本时命中率会被副本数除）。
    所以 `active` 与 `degraded` 都是**可观测状态**，不是内部变量。
    """

    def __init__(self, primary: EntryStore, fallback: EntryStore | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        #: 主存储的名字（**在构造时记下来**，与 `active_name` 比较得出降级状态）。
        # ⚠️ 不用 `active_name != primary.name()`：`name()` 是**每次调用都问一遍**，
        # 而两个内存实现的 `name()` 都返回 `"memory"` —— 于是「内存主存储 + 内存回落」
        # 这个组合永远显示「未降级」，而降级这件事恰恰最需要被看见。
        self.primary_name = primary.name()
        self.active_name = self.primary_name
        self.degraded_since: float | None = None
        #: 主存储报错次数 / 恢复次数（自检脚本会打出来）
        self.failures = 0
        self.recoveries = 0
        #: `probe_interval` 秒内只试一次主存储（否则每次请求都要先吃一次超时）
        self.probe_interval = 5.0

    # -- 状态 --------------------------------------------------------------
    @property
    def degraded(self) -> bool:
        """是否处于回落形态（`active_name` 不再是主存储的名字）。"""
        return self.active_name != self.primary_name

    def _record_failure(self, exc: Exception) -> None:
        self.failures += 1
        if self.degraded_since is None:
            self.degraded_since = time.time()
            logger.warning(
                "缓存主存储（%s）不可用，已回落到 %s：%s。"
                "回落期间缓存仍然工作，但**副本之间不再共享**（多副本时命中率会下降）。",
                self.primary_name,
                self.active_name,
                exc,
            )

    def _record_recovery(self) -> None:
        if self.degraded_since is not None:
            self.recoveries += 1
            logger.info(
                "缓存主存储（%s）已恢复，切回它（降级持续了 %.1fs）",
                self.primary.name(),
                time.time() - self.degraded_since,
            )
            self.degraded_since = None

    def _invoke(self, store: EntryStore, method: str, *args: Any, **kwargs: Any) -> Any:
        return getattr(store, method)(*args, **kwargs)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """先试主存储；失败则回落。**主存储恢复后自动切回。**

        ⚠️ 恢复探测必须走**同一次真实调用**（而不是单独的 `health()` 探活）：
        探活成功之后再把这次操作交给主存储，等于一次调用做两遍 ——
        `put` 与 `touch` 做两遍无害，但 `purge` 会删两批、`deactivate` 会返回 0
        （第一次已经把行改掉了）。所以这里的顺序是「先试主存储 → 成了就说明恢复了」。

        代价是：探测失败时这次调用仍然落在回落上（正确），
        而「探测」与「真实调用」是同一次 —— 于是 `probe_interval` 内不再重复试主存储，
        避免每次请求都先吃一次 socket 超时。
        """
        if self.degraded and self.fallback is not None:
            elapsed = time.time() - float(self.degraded_since or 0)
            if elapsed < self.probe_interval:
                return self._invoke(self.fallback, method, *args, **kwargs)
            # 到探测时刻了：这一次调用直接打主存储 —— 成了就是恢复，不成就继续回落。
            try:
                result = self._invoke(self.primary, method, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 还没恢复
                self.failures += 1
                logger.debug("缓存主存储还没恢复（%s）：%s", self.primary.name(), exc)
                return self._invoke(self.fallback, method, *args, **kwargs)
            self.active_name = self.primary_name
            self._record_recovery()
            return result

        try:
            result = self._invoke(self.primary, method, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 缓存出错一律 fail-open
            self._record_failure(exc)
            if self.fallback is None:
                raise
            self.active_name = self.fallback.name()
            return self._invoke(self.fallback, method, *args, **kwargs)
        return result

    # -- EntryStore 协议（逐个转发，签名与 Protocol 一致）-------------------
    def name(self) -> str:
        return self.active_name

    def get(self, context: ScopeContext, question: str) -> CacheEntry | None:
        return self._call("get", context, question)

    def nearest(
        self, context: ScopeContext, embedding: Sequence[float], *, limit: int = 5
    ) -> list[CacheMatch]:
        return self._call("nearest", context, embedding, limit=limit)

    def put(self, entry: CacheEntry) -> bool:
        return bool(self._call("put", entry))

    def touch(self, entry: CacheEntry) -> None:
        self._call("touch", entry)

    def deactivate(self, context: ScopeContext, question: str) -> bool:
        return bool(self._call("deactivate", context, question))

    def deactivate_scope(self, scope: str) -> int:
        return int(self._call("deactivate_scope", scope) or 0)

    def purge(self, *, limit: int | None = None, only_expired: bool = True) -> int:
        return int(self._call("purge", limit=limit, only_expired=only_expired) or 0)

    def count(self, *, scope: str | None = None, only_active: bool = True) -> int:
        return int(self._call("count", scope=scope, only_active=only_active) or 0)

    def list_entries(self, *, scope: str | None = None, limit: int = 100) -> list[CacheEntry]:
        """列条目（运维工具用；**透传**给当前生效的那一个存储）。

        ⚠️ 这个方法**必须也在这里转发**（第一版漏了）：`ResilientEntryStore` 是
        门面拿到的对象，而 `getattr(store, "list_entries")` 在它身上找不到时
        工具会静默打印「（空）」—— 「缓存里有 1 条但抽样一条都没有」
        这种读数不一致极难查（本仓实测踩到）。
        """
        return list(self._call("list_entries", scope=scope, limit=limit) or [])

    def health(self) -> dict[str, Any]:
        try:
            payload = dict(self.primary.health())
        except Exception as exc:  # noqa: BLE001 - 自检本身不该抛
            payload = {"store": self.primary.name(), "error": f"{type(exc).__name__}: {exc}"}
        payload |= {
            "active": self.active_name,
            "degraded": self.degraded,
            "failures": self.failures,
            "recoveries": self.recoveries,
        }
        if self.fallback is not None and self.degraded:
            payload["fallback"] = self.fallback.health()
        return payload


# ── 工厂：按配置造一个「主存储 + 回落」的组合 ────────────────────────────────


def build_key_value(settings: CacheBackendSettings, cache: CacheSettings) -> tuple[KeyValue, bool]:
    """造键值层：返回 `(实现, 是否回落到了内存)`。

    两种情况会回落（都是**可见**的，不静默）：
      * `redis_url` 为空 —— 本仓的 `REDIS_URL` 在 `.env` 里有值，为空说明配置漏了；
      * Redis 连不上 / `PING` 失败 —— 依赖栈没起或地址写错。

    `fallback_to_memory=False` 时**不回落**（返回内存实现并把 `degraded` 标成 True，
    由调用方决定要不要用）：生产想强制「没有 Redis 就不缓存」时用得上。
    """
    url = str(settings.redis_url or "").strip()
    if not url:
        logger.warning(
            "REDIS_URL 为空：缓存回落为进程内存（%s 只在本进程内有效）。"
            "这会让「命中率」在多副本部署时被副本数除 —— 生产请配好 redis_url。",
            settings.instance,
        )
        return MemoryKeyValue(), True

    try:
        kv = RedisKeyValue(url=url, timeout_seconds=cache.lookup_timeout_seconds)
        if not kv.ping():
            raise ConnectionError("PING 返回假")
    except Exception as exc:  # noqa: BLE001 - 连不上不是错误，是降级
        logger.warning(
            "Redis 不可达（%s），缓存回落为进程内存：%s。"
            "排查顺序：① .env 的 REDIS_URL 是不是 Linux VM 的 IP；② `redis-cli -h <ip> ping`。",
            url.split("@")[-1],
            exc,
        )
        return MemoryKeyValue(), True
    return kv, False


def build_embedding_cache(key_value: KeyValue, *, keys: CacheKeySpace, ttl_seconds: int) -> Any:
    """造一个「文本 → 向量」的缓存（同一句话只算一次 embedding）。

    它是**可选的加速器**：挂了也不影响正确性（只是每次都要真算一遍）。
    所以它自己吞掉所有异常 —— 调用方不该为它写 `try`。
    """

    class _EmbeddingCache:
        """实现 `EmbeddingCache` 协议的内存对象（用闭包抓住 key 空间与 TTL）。"""

        def __init__(self) -> None:
            self.hits = 0
            self.misses = 0
            self.errors = 0

        def get(self, *, model: str, text: str) -> list[float] | None:
            try:
                raw = key_value.get(keys.embedding(model=model, text=text))
            except Exception:  # noqa: BLE001 - 加速器不该抛
                self.errors += 1
                return None
            if raw is None:
                self.misses += 1
                return None
            try:
                payload = json.loads(raw)
                vector = [float(value) for value in payload]
            except (ValueError, TypeError):
                self.errors += 1
                return None
            self.hits += 1
            return vector

        def set(self, *, model: str, text: str, vector: Sequence[float]) -> None:
            try:
                key_value.set(
                    keys.embedding(model=model, text=text),
                    json.dumps([float(value) for value in vector]),
                    ttl_seconds=ttl_seconds,
                )
            except Exception:  # noqa: BLE001
                self.errors += 1

        def state(self) -> dict[str, int]:
            return {"hits": self.hits, "misses": self.misses, "errors": self.errors}

    return _EmbeddingCache()


__all__ = [
    "INDEX_SUFFIX",
    "SEPARATOR",
    "CacheKeySpace",
    "EmbeddingCache",
    "KeyValue",
    "MemoryKeyValue",
    "RedisEntryStore",
    "RedisKeyValue",
    "ResilientEntryStore",
    "build_embedding_cache",
    "build_key_value",
]
