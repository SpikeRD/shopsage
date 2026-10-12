"""缓存的**数据形状**与三套实现：`CacheEntry` / `CacheMatch` / 日志账目 + 内存与 Postgres 后端。

这一层刻意不知道「阈值、TTL 策略、no_cache」这些**判断**（它们在 `core.py` / `intent.py`），
它只回答三个机械的问题：

| 问题 | 方法 | 为什么单独一个 |
|---|---|---|
| 「这个 scope 下有没有这句话？」 | `get()` | 完全匹配（hash）—— 最便宜的一条路 |
| 「这个 scope 下最像的是哪几条？」 | `nearest()` | 相似度查询 —— **不判命中**，只给候选与分数 |
| 「把这条写进去 / 标记失效 / 清掉」 | `put()` / `deactivate()` / `purge()` | 写路径只有这三件事 |

**「判不判命中」为什么不在这一层**：那是 `13.cache.md` §4 第 3 条那个「保守阈值」的落点，
它要被单测逐条钉住（0.92 不命中 / 0.94 命中），而「怎么算余弦相似度」不需要被测。
把两者混在一起，测阈值就得起一个数据库。

## 为什么是「幂等写入」而不是「先查后写」

`put()` 的语义是 upsert（同一 `scope + fingerprint + question_hash` 覆盖旧行）。
不用「先 `get` 再决定插还是改」有两个理由：

1. 两个并发请求会同时判断「不存在」并发起两条插入 —— 那会在唯一索引上撞车，
   而撞车的表现是一条 `IntegrityError`（一个与业务无关的错误）；
2. upsert 让「写两次」与「写一次」的结果完全一样 —— 这是重放/重试场景的前提
   （第 12 步的任务重投会让同一段代码跑两遍）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CacheStoreError(RuntimeError):
    """存储层错误（表不存在、维度不符…）：消息必须能照着修。"""


# ── 数据形状 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScopeContext:
    """「这一条缓存属于谁、属于哪一版」—— **缓存的隔离边界**。

    三个字段各挡一类串味（每一类都实测过是灾难）：

    | 字段 | 挡的是 | 不挡会怎样 |
    |---|---|---|
    | `scope`（身份 + 角色） | 用户 A 的答案给用户 B | 「我的订单到哪了」的缓存**必然**答错人（A2 越权） |
    | `fingerprint`（知识库版本 + prompt 版本 + embedding 模型） | 旧知识库的答案 | 文档改了、prompt 改了，缓存还在讲老政策（F1 失效） |

    ⚠️ `scope` **不包含** trace_id / 会话 id 这类「每次都不同」的值：
    带上它们等于「缓存命中率恒为 0」，而它看起来完全正常（只是从没命中过）。
    会话隔离靠 `question` 本身（见 `semantic.build_question`：多轮上下文是问题的一部分）。
    """

    scope: str
    fingerprint: str

    def as_key(self) -> str:
        return f"{self.scope}|{self.fingerprint}"


def scope_for(user_id: int, role: str) -> str:
    """身份 + 角色 → 稳定的 scope 串。

    ⚠️ **角色要进来**：同一个 user_id 在不同角色下的可见范围不同
    （第 4 步的 RBAC：`internal_employee` 有 `read:all_orders`，`customer` 没有）。
    只用 user_id 会让「一次内部查询」的答案被一个客户命中 —— 那是一起越权。
    """
    return f"{str(role or 'anonymous').strip() or 'anonymous'}:{int(user_id or 0)}"


@dataclass(frozen=True)
class CacheEntry:
    """一条缓存条目（值 + 溯源 + 时间戳，全是 `13.cache.md` §4 第 4 条点名要的字段）。"""

    #: 隔离边界
    scope: str
    fingerprint: str
    #: 问题原文与它的 hash（hash 用于完全匹配的等值查询）
    question: str
    #: 原始用户消息（多轮时是最近一轮）。`question` 可能拼了上下文，这个是**只有用户说的那句**
    raw_question: str
    embedding: tuple[float, ...]
    answer: str
    #: 出处（`[doc.citation()]` 那一串），命中时连出处一起返回 —— 这是「可追溯」的全部实现
    citations: tuple[str, ...] = ()
    #: 结构化附加信息（route / model / node 这类；**不放 prompt 全文**，见 tracing 的白名单口径）
    meta: dict[str, Any] = field(default_factory=dict)
    #: 写入时的知识库版本（第三级失效的判据：它变了就整批失效）
    revision: int = 0
    #: 分档（`normal` / `policy` / `time_sensitive`）—— 排障时「为什么这条只活了 60 秒」要看它
    policy: str = "normal"
    ttl_seconds: int = 0
    #: 写入时刻（epoch 秒）。用它而不是 `created_at` 字符串：过期判断是纯算术，不必解析时间
    created_at: float = 0.0
    hit_count: int = 0
    last_hit_at: float | None = None

    @property
    def expires_at(self) -> float:
        """过期时刻（epoch 秒）。"""
        return float(self.created_at) + float(self.ttl_seconds)

    def age_seconds(self, *, now: float | None = None) -> float:
        return max(0.0, (float(now) if now is not None else time.time()) - float(self.created_at))

    def is_expired(self, *, now: float | None = None) -> bool:
        """第一级失效：**时间过期（TTL）**。

        `ttl_seconds <= 0` 视为「立刻过期」（而不是「永不过期」）：
        配置写错时选「不缓存」比选「永久缓存」安全一个数量级 —— 后者会把
        一次错误的写入变成一个永久错误的答案。

        ⚠️ 三元表达式的括号**不能省**（本仓实测踩到）：
        Python 的条件表达式优先级比比较运算符**低**，所以
        `float(now) if now is not None else time.time() >= self.expires_at`
        会被解析成 `float(now) if cond else (time.time() >= self.expires_at)` ——
        传了 `now` 时它返回一个**浮点数**（恒为真），于是**所有传了 now 的调用都判过期**。
        症状极其隐蔽：`judge()` 里每一处 `is_expired(now=moment)` 都返回真，
        于是「缓存永远不命中」，而日志说「TTL 已过（0s > 3600s）」。
        """
        if self.ttl_seconds <= 0:
            return True
        moment = float(now) if now is not None else time.time()
        return moment >= self.expires_at

    def is_fresh(self, *, revision: int | None = None) -> bool:
        """第三级失效：**版本失效**（写入时的知识库版本 vs 当前版本）。

        `revision=None` 表示「调用方不知道当前版本」——那种情况下**判为新鲜**
        （保守方向是「不因为读不到版本而清空缓存」，读不到版本时 TTL 仍然是兜底）；
        想主动失效请显式传 `revision=`。
        """
        if revision is None:
            return True
        return int(self.revision) == int(revision)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "fingerprint": self.fingerprint,
            "question": self.question,
            "raw_question": self.raw_question,
            "answer": self.answer,
            "citations": list(self.citations),
            "meta": dict(self.meta),
            "revision": self.revision,
            "policy": self.policy,
            "ttl_seconds": self.ttl_seconds,
            "created_at": self.created_at,
            "hit_count": self.hit_count,
            "last_hit_at": self.last_hit_at,
            "embedding": list(self.embedding),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CacheEntry:
        return cls(
            scope=str(payload["scope"]),
            fingerprint=str(payload["fingerprint"]),
            question=str(payload["question"]),
            raw_question=str(payload.get("raw_question") or payload["question"]),
            embedding=tuple(float(value) for value in (payload.get("embedding") or ())),
            answer=str(payload["answer"]),
            citations=tuple(str(item) for item in (payload.get("citations") or ())),
            meta=dict(payload.get("meta") or {}),
            revision=int(payload.get("revision") or 0),
            policy=str(payload.get("policy") or "normal"),
            ttl_seconds=int(payload.get("ttl_seconds") or 0),
            created_at=float(payload.get("created_at") or 0.0),
            hit_count=int(payload.get("hit_count") or 0),
            last_hit_at=payload.get("last_hit_at"),
        )

    def with_hit(self, *, now: float | None = None) -> CacheEntry:
        """命中之后的那一份（`hit_count + 1`，其余不变）。"""
        from dataclasses import replace

        return replace(
            self,
            hit_count=int(self.hit_count) + 1,
            last_hit_at=float(now) if now is not None else time.time(),
        )

    def question_hash(self) -> str:
        return question_hash(self.question)


@dataclass(frozen=True)
class CacheMatch:
    """`nearest()` 的一次结果：**相似度 + 条目**（相似度是排障唯一需要的解释）。"""

    similarity: float
    entry: CacheEntry

    def as_dict(self) -> dict[str, Any]:
        return {
            "similarity": round(float(self.similarity), 6),
            "question": self.entry.question,
            "answer": self.entry.answer,
            "age_seconds": round(self.entry.age_seconds(), 3),
        }


@dataclass(frozen=True)
class LookupLog:
    """一次查询的完整账目（**演示、排障、指标的唯一素材**）。

    为什么不只返回 `CacheEntry | None`：本模块最值得回答的问题是
    「**为什么没命中**」，而它的答案有六种（没写入 / 过期 / 版本变了 / 相似度不够 /
    强时效被跳过 / 存储不可用）。只给 `None` 等于把这六种压成一句话。
    """

    outcome: str
    detail: str = ""
    similarity: float | None = None
    entry: CacheEntry | None = None
    elapsed_ms: float = 0.0
    policy: str = "normal"
    #: 灰区候选中最近的那个（`similarity` 在 floor 与 threshold 之间时它不是 None）
    nearest: CacheMatch | None = None
    #: 存储是否处于降级形态（Redis 不可用 → 内存）
    degraded: bool = False

    @property
    def hit(self) -> bool:
        return self.outcome == "hit"

    def summary(self) -> str:
        """一句话（日志与 CLI 共用；不打印答案正文 —— 它可能含 PII）。"""
        head = {
            "hit": "命中",
            "miss": "未命中",
            "stale": "已失效",
            "bypassed": "按策略跳过",
            "error": "存储不可用（已降级）",
            "disabled": "缓存已关闭",
        }.get(self.outcome, self.outcome)
        bits = [head]
        if self.similarity is not None:
            bits.append(f"相似度={self.similarity:.4f}")
        if self.entry is not None:
            bits.append(f"问了「{self.entry.question[:24]}」")
        if self.detail:
            bits.append(self.detail)
        bits.append(f"{self.elapsed_ms:.2f}ms")
        return " | ".join(bits)


def question_hash(question: str) -> str:
    """问题 → 稳定 hash（**规范化后**再算：空白差别不该变成两条缓存）。

    规范化只做三件不会改变语义的事：① 首尾去空白；② 连续空白折叠成一个空格；
    ③ `casefold()`（英文大小写）。**不做**去标点/去语气词 ——
    那会让「怎么退货？」与「怎么退货」共用一条，而这恰好是语义缓存该做的事，
    不该由 hash 偷偷替它决定（两者的后果完全不同：hash 撞车是静默的错误命中）。
    """
    normalized = " ".join(str(question or "").split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


# ── 存储契约 ────────────────────────────────────────────────────────────────


@runtime_checkable
class EntryStore(Protocol):
    """读一条 / 找最近的 / 写一条 / 标失效 —— 缓存的全部存储操作。

    ⚠️ 五个方法都是**同步**的：这一层被 `asyncio.to_thread` 包着调用
    （Postgres 用同步引擎，Redis 用 `redis` 同步客户端的连接池）。
    做成 async 会让「内存实现」多一层无意义的 `await`，而它其实是纯计算。
    """

    def name(self) -> str: ...

    def get(self, context: ScopeContext, question: str) -> CacheEntry | None: ...

    def nearest(
        self, context: ScopeContext, embedding: Sequence[float], *, limit: int = 5
    ) -> list[CacheMatch]: ...

    def put(self, entry: CacheEntry) -> bool: ...

    def touch(self, entry: CacheEntry) -> None: ...

    def deactivate(self, context: ScopeContext, question: str) -> bool: ...

    def deactivate_scope(self, scope: str) -> int: ...

    def purge(self, *, limit: int | None = None, only_expired: bool = True) -> int: ...

    def count(self, *, scope: str | None = None, only_active: bool = True) -> int: ...

    def list_entries(self, *, scope: str | None = None, limit: int = 100) -> list[CacheEntry]: ...

    def health(self) -> dict[str, Any]: ...


# ── 内存实现（离线用例、演示、Redis 不可用时的回落）─────────────────────────


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度（**这才是缓存判相似的唯一口径**）。

    两个刻意的处置：

    * 长度不一致 → 返回 `-1.0`（明确「不相干」）而**不是抛异常**：
      向量维度变化（换模型）会让旧条目与新查询维度不同，那时正确的行为是
      「全都当作不命中、由 TTL/版本把它们淘汰掉」，而不是让每个请求 500。
    * 零向量 → 返回 `0.0`：除零会得到 `nan`，而 `nan >= threshold` 恒为 False
      （看起来「不命中」，实际上是把一个错误数据静默当成了正常路径）。
    """
    if len(left) != len(right) or not left:
        return -1.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return float(dot / ((left_norm**0.5) * (right_norm**0.5)))


class MemoryEntryStore:
    """进程内存里的条目表：**LRU + 暴力相似度**。

    它的三个用途（都不是「假的实现」）：

    1. **离线用例**：语义缓存的全部语义（阈值、TTL、版本失效、灰区）都能在它上面验完，
       不起数据库、不花钱（与第 12 步用内存队列验队列语义是同一个思路）；
    2. **演示脚本**：`scripts/cache_demo.py` 的默认后端；
    3. **回落**：Redis 与 Postgres 都不可用时，`fallback_to_memory=True` 让它顶上 ——
       此时 `degraded=True` 会一路传到指标与 `/health`（**绝不静默**）。

    ⚠️ 暴力相似度是**刻意的**：候选集上限 `max_entries`（默认 2000）× 1024 维
    ≈ 200 万次浮点乘加，实测 5~10ms。超过这个量级就该换 Postgres 的 HNSW
    （那是 `PostgresEntryStore` 的活），而不是在这里加一棵 KD 树 ——
    一个自己写的近邻索引在 1024 维上通常**比暴力还慢**，而且它的正确性没人验。
    """

    def __init__(self, *, max_entries: int = 2000) -> None:
        self.max_entries = max(1, int(max_entries))
        #: `(scope, fingerprint, question_hash)` → 条目
        self._entries: OrderedDict[tuple[str, str, str], CacheEntry] = OrderedDict()
        #: 计数（自检用）
        self.writes = 0
        self.evictions = 0

    # -- EntryStore 协议 ----------------------------------------------------
    def name(self) -> str:
        return "memory"

    def get(self, context: ScopeContext, question: str) -> CacheEntry | None:
        key = (context.scope, context.fingerprint, question_hash(question))
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)  # LRU：读也算「最近用过」
        return entry

    def nearest(
        self, context: ScopeContext, embedding: Sequence[float], *, limit: int = 5
    ) -> list[CacheMatch]:
        scored: list[CacheMatch] = []
        for (scope, fingerprint, _), entry in self._entries.items():
            if scope != context.scope or fingerprint != context.fingerprint:
                continue
            scored.append(CacheMatch(similarity=cosine_similarity(embedding, entry.embedding), entry=entry))
        # 分数降序；同分按写入时间倒序（**确定性**：不然用例会时对时错）
        scored.sort(key=lambda item: (-item.similarity, -item.entry.created_at))
        return scored[: max(1, int(limit))]

    def put(self, entry: CacheEntry) -> bool:
        key = (entry.scope, entry.fingerprint, entry.question_hash())
        replaced = key in self._entries
        self._entries[key] = entry
        self._entries.move_to_end(key)
        self.writes += 1
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self.evictions += 1
        return not replaced

    def touch(self, entry: CacheEntry) -> None:
        key = (entry.scope, entry.fingerprint, entry.question_hash())
        if key in self._entries:
            self._entries[key] = entry.with_hit()

    def deactivate(self, context: ScopeContext, question: str) -> bool:
        """按 scope + 问题失效；`context.fingerprint` 为**空串时匹配任意版本**。

        空指纹 = 通配是刻意的（见 `app/cache/invalidate.py` 的说明）：
        「主动失效」的语义是「不再需要它了」，而调用方（人工、注销流程）
        **不该**需要知道指纹是什么 —— 那是缓存内部的版本细节。
        要求调用方提供指纹，等于让「删一条缓存」变成一件要先去查 fingerprint 的事。
        """
        digest = question_hash(question)
        victims = [
            key
            for key in self._entries
            if key[0] == context.scope
            and key[2] == digest
            and (not context.fingerprint or key[1] == context.fingerprint)
        ]
        for key in victims:
            self._entries.pop(key, None)
        return bool(victims)

    def deactivate_scope(self, scope: str) -> int:
        victims = [key for key in self._entries if key[0] == scope]
        for key in victims:
            self._entries.pop(key, None)
        return len(victims)

    def purge(self, *, limit: int | None = None, only_expired: bool = True) -> int:
        now = time.time()
        victims = [
            key
            for key, entry in self._entries.items()
            if (entry.is_expired(now=now) if only_expired else True)
        ]
        if limit is not None:
            victims = victims[: max(0, int(limit))]
        for key in victims:
            self._entries.pop(key, None)
        return len(victims)

    def count(self, *, scope: str | None = None, only_active: bool = True) -> int:
        now = time.time()
        total = 0
        for (entry_scope, _, _), entry in self._entries.items():
            if scope is not None and entry_scope != scope:
                continue
            if only_active and entry.is_expired(now=now):
                continue
            total += 1
        return total

    def health(self) -> dict[str, Any]:
        return {
            "store": "memory",
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "writes": self.writes,
            "evictions": self.evictions,
        }

    # -- 测试与演示用的小工具（不属于协议）--------------------------------
    def clear(self) -> None:
        self._entries.clear()

    def all_entries(self) -> list[CacheEntry]:
        return list(self._entries.values())


# ── Postgres 实现（相似度查询走 pgvector 的 HNSW 索引）──────────────────────


#: 应用时钟与数据库时钟的偏差（秒）：`db_now - app_now`。**只测一次、缓存住。**
#:
#: ## 为什么需要它（本仓实测踩到的一个真坑）
#:
#: 开发机（Windows）与依赖 VM（Linux 上的 PostgreSQL）的时钟**实测差了 1.9 秒**：
#:
#: ```
#: db_now - client_mid = -1.879s / -1.892s / -1.892s   ← 连续三次测量
#: ```
#:
#: 于是「应用写入 `expires_at = 应用时钟的 now + 1 秒`」在数据库看来是**未来 2.9 秒**，
#: 那条 `ttl_seconds=1` 的条目会在库里继续「新鲜」将近 3 秒 ——
#: `count()` 与 `purge()`（它们问的是数据库的 `now()`）于是慢半拍，
#: 症状是「明明配置了 1 秒 TTL，计数还是 2 条 / 回收删了 0 条」。
#:
#: ## 处置：把两个时钟**对齐到数据库那一边**
#:
#: 写库时把应用侧的时间戳平移到数据库的时钟域（`epoch + skew`），
#: 读回来再平移回去。于是**存储里的 `created_at` / `expires_at` 与数据库的 `now()`
#: 是同一个口径**，两种时钟可以混用（SQL 里的 `expires_at >= now()` 与
#: Python 里的 `CacheEntry.is_expired()` 得到同一个答案）。
#:
#: 为什么不干脆「统一用应用时钟」：`purge()` 是一条 `DELETE ... WHERE`，
#: 它天生要在数据库里过滤（否则要先 `SELECT` 全表回内存）。让存储侧的时间戳服从
#: 数据库时钟，代价只是一次 `select now()`，换的是「一条不会因为 NTP 漂移而失准的 TTL」。
_SKEW_SECONDS: float | None = None


def db_clock_skew(engine: Any, *, refresh: bool = False) -> float:
    """测量（并缓存）`db_now - app_now`；测不出来就是 0.0（退化成「时钟一致」）。"""
    global _SKEW_SECONDS
    if _SKEW_SECONDS is not None and not refresh:
        return _SKEW_SECONDS
    try:
        from sqlalchemy import text

        before = time.time()
        with engine.connect() as conn:
            raw = conn.execute(text("select extract(epoch from now())")).scalar_one()
        after = time.time()
        # 用往返的**中点**当「客户端此刻」，抵消网络延迟带来的偏差
        _SKEW_SECONDS = float(raw) - (before + after) / 2.0
        logger.debug("应用/数据库时钟偏差 %.3fs", _SKEW_SECONDS)
    except Exception as exc:  # noqa: BLE001 - 测不出来不该让缓存不可用
        logger.debug("测量时钟偏差失败（按 0 处理）：%s", exc)
        _SKEW_SECONDS = 0.0
    return float(_SKEW_SECONDS)


def reset_clock_skew() -> None:
    """丢掉缓存的偏差（进程换库、测试改了环境时用）。"""
    global _SKEW_SECONDS
    _SKEW_SECONDS = None


class PostgresEntryStore:
    """条目表落在业务库（pgvector）。

    连接策略与 `RagStore` 一致：**写走写引擎、读走读引擎**
    （`3.db.md` 的读写分离口径）。读路径复用只读角色 `shopsage_readonly`，
    于是「缓存查询不会写库」这件事顺手被数据库权限证明了 ——
    代价是迁移后要给这个角色补一次授权（`scripts/db_readonly.py` 的清单里已加上）。
    """

    def __init__(
        self,
        *,
        engine: Any = None,
        read_engine: Any = None,
        entries_table: str = "cache_semantic_entries",
        versions_table: str = "cache_corpus_versions",
        dimension: int | None = None,
    ) -> None:
        for name, value in (("entries_table", entries_table), ("versions_table", versions_table)):
            if not _IDENTIFIER.match(value):
                raise CacheStoreError(f"表名只允许标识符字符，收到 {name}={value!r}")
        self._engine = engine
        self._read_engine = read_engine
        self.entries_table = entries_table
        self.versions_table = versions_table
        self.dimension = dimension

    @property
    def engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        from app.db.session import get_write_engine

        return get_write_engine()

    @property
    def read_engine(self) -> Any:
        if self._read_engine is not None:
            return self._read_engine
        from app.db.session import get_read_engine

        return get_read_engine()

    def name(self) -> str:
        return "postgres"

    # -- 内部：SQL 与错误翻译 ----------------------------------------------
    def _check_error(self, exc: Exception) -> CacheStoreError:
        """把驱动报错翻成「能照着修」的说明（最常见的一类：忘了跑迁移）。"""
        message = str(exc)
        if "does not exist" in message and self.entries_table in message:
            return CacheStoreError(
                f"缓存表 {self.entries_table} 不存在：先执行 `uv run alembic upgrade head`"
                "（第 13 步的迁移会建 cache_semantic_entries / cache_corpus_versions 两张表）。"
                f"原始报错：{message.splitlines()[0]}"
            )
        if "expected" in message and "dimensions" in message:
            return CacheStoreError(
                f"缓存向量维度不符：{message.splitlines()[0]}。请把 EMBEDDING_DIM / RAG_VECTOR_DIM "
                "与缓存表的 vector(n) 统一（三处一起改），然后清空缓存表重灌。"
            )
        return CacheStoreError(message.splitlines()[0])

    _COLUMNS = (
        "scope, question, question_hash, embedding, answer, citations, meta, fingerprint, "
        "corpus_revision, policy, ttl_seconds, created_at, expires_at, hit_count, last_hit_at, "
        "is_active"
    )

    def _to_db_time(self, epoch_seconds: float) -> Any:
        """应用时钟的 epoch → **数据库时钟域**的时间戳（见 `db_clock_skew` 的长注释）。"""
        return _to_timestamp(float(epoch_seconds) + db_clock_skew(self.engine))

    def _from_db_time(self, value: Any) -> float:
        """数据库时钟域的时间戳 → 应用时钟的 epoch（与 `_to_db_time` 互为逆运算）。"""
        return float(value.timestamp()) - db_clock_skew(self.engine)

    def _row_to_entry(self, row: Any) -> CacheEntry:
        payload = dict(row._mapping)
        embedding = payload.get("embedding")
        if isinstance(embedding, str):
            embedding = [float(value) for value in embedding.strip("[]").split(",") if value.strip()]
        created = payload.get("created_at")
        return CacheEntry(
            scope=str(payload["scope"]),
            fingerprint=str(payload["fingerprint"]),
            question=str(payload["question"]),
            raw_question=str((payload.get("meta") or {}).get("raw_question") or payload["question"]),
            embedding=tuple(float(value) for value in (embedding or ())),
            answer=str(payload["answer"]),
            citations=tuple(str(item) for item in (payload.get("citations") or ())),
            meta=dict(payload.get("meta") or {}),
            revision=int(payload.get("corpus_revision") or 0),
            policy=str(payload.get("policy") or "normal"),
            ttl_seconds=int(payload.get("ttl_seconds") or 0),
            created_at=self._from_db_time(created) if created is not None else 0.0,
            hit_count=int(payload.get("hit_count") or 0),
            last_hit_at=(
                self._from_db_time(payload["last_hit_at"])
                if payload.get("last_hit_at") is not None
                else None
            ),
        )

    # -- EntryStore 协议 ----------------------------------------------------
    def get(self, context: ScopeContext, question: str) -> CacheEntry | None:
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        try:
            with self.read_engine.connect() as conn:
                row = conn.execute(
                    text(
                        f"select {self._COLUMNS} from {self.entries_table} "
                        "where scope = :scope and fingerprint = :fingerprint "
                        "and question_hash = :question_hash and is_active "
                        "order by created_at desc limit 1"
                    ),
                    {
                        "scope": context.scope,
                        "fingerprint": context.fingerprint,
                        "question_hash": question_hash(question),
                    },
                ).one_or_none()
        except SQLAlchemyError as exc:
            raise self._check_error(exc) from exc
        return self._row_to_entry(row) if row is not None else None

    def nearest(
        self, context: ScopeContext, embedding: Sequence[float], *, limit: int = 5
    ) -> list[CacheMatch]:
        """向量腿：pgvector 的余弦距离 ANN（走 HNSW 索引）。

        `1 - (embedding <=> :q)` 是余弦相似度（pgvector 的 `<=>` 是**距离**）。
        `order by` 用距离升序（等价于相似度降序），这样它才能吃到 HNSW 索引 ——
        写成 `order by 1 - (.. <=> ..) desc` 在某些计划下会退化成全表扫描
        （与 `rag/store.py::search_vector` 是同一个坑、同一个处置）。

        ⚠️ 查询里**不带** `expires_at` / 版本过滤：过期与版本判定留在 `core.py`
        （见模块 docstring）。代价是索引要扫到「最近的 5 条（含过期的）」，
        好处是「为什么没命中」能拿到那条过期/旧版本的候选 —— 那是排障唯一有用的信息。
        """
        if not embedding:
            return []
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        try:
            with self.read_engine.connect() as conn:
                rows = conn.execute(
                    text(
                        f"""
                        with candidates as (
                            select {self._COLUMNS},
                                   embedding <=> cast(:query as vector) as distance
                            from {self.entries_table}
                            where scope = :scope and fingerprint = :fingerprint and is_active
                            order by embedding <=> cast(:query as vector)
                            limit :limit
                        )
                        select {self._COLUMNS}, 1 - distance as similarity
                        from candidates
                        order by distance
                        """
                    ),
                    {
                        "query": _vector_literal(embedding),
                        "scope": context.scope,
                        "fingerprint": context.fingerprint,
                        "limit": max(1, int(limit)),
                    },
                ).all()
        except SQLAlchemyError as exc:
            raise self._check_error(exc) from exc
        return [CacheMatch(similarity=float(row.similarity), entry=self._row_to_entry(row)) for row in rows]

    def put(self, entry: CacheEntry) -> bool:
        """写一条（**upsert**：唯一索引是 `(scope, fingerprint, question_hash)`），返回「是否是新建」。

        ⚠️ 返回值是「表里原本没有这一行」——所以**要在 upsert 之前**查一次
        （见下面 `existing` 那次 select）。不要试图从 `rowcount` 里推：
        PostgreSQL 的 `on conflict do update` 与纯 insert 的 rowcount 都是 1。
        

        为什么 `on conflict do update` 而不是 `do nothing`：`do nothing` 会让
        「同一句话、新答案」永远写不进去 —— 而「重新生成了一次更好的回答」正是
        我们希望在缓存里看到的事（`13.cache.md` §5 的「缓存值存答案+溯源+时间戳」，
        时间戳不更新就等于这条缓存永远显示第一次的年龄）。

        `hit_count` **不重置**：它是这条问题的历史命中次数（换个答案不等于没人问过）。
        """
        if self.dimension is not None and len(entry.embedding) != self.dimension:
            raise CacheStoreError(
                f"缓存向量维度不符：本 store 按 {self.dimension} 维配置，收到 {len(entry.embedding)} 维。"
                "请把 EMBEDDING_DIM / RAG_VECTOR_DIM 与缓存表的 vector(n) 统一，然后清空缓存表重灌。"
            )
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        payload = {
            "scope": entry.scope,
            "question": entry.question,
            "question_hash": entry.question_hash(),
            "embedding": _vector_literal(entry.embedding) if entry.embedding else None,
            "answer": entry.answer,
            "citations": _json_literal(list(entry.citations)),
            "meta": _json_literal({"raw_question": entry.raw_question, **dict(entry.meta)}),
            "fingerprint": entry.fingerprint,
            "corpus_revision": int(entry.revision),
            "policy": entry.policy,
            "ttl_seconds": int(entry.ttl_seconds),
            "created_at": self._to_db_time(entry.created_at),
            "expires_at": self._to_db_time(entry.expires_at),
            "hit_count": int(entry.hit_count),
            "last_hit_at": self._to_db_time(entry.last_hit_at) if entry.last_hit_at else None,
        }
        try:
            with self.engine.begin() as conn:
                existing = conn.execute(
                    text(
                        f"select id from {self.entries_table} "
                        "where scope = :scope and fingerprint = :fingerprint and question_hash = :question_hash"
                    ),
                    payload,
                ).one_or_none()
                conn.execute(
                    text(
                        f"""
                        insert into {self.entries_table} (
                            scope, question, question_hash, embedding, answer, citations, meta,
                            fingerprint, corpus_revision, policy, ttl_seconds, created_at, expires_at,
                            hit_count, last_hit_at, is_active
                        ) values (
                            :scope, :question, :question_hash, cast(:embedding as vector), :answer,
                            cast(:citations as jsonb), cast(:meta as jsonb), :fingerprint,
                            :corpus_revision, :policy, :ttl_seconds, :created_at, :expires_at,
                            :hit_count, :last_hit_at, true
                        )
                        on conflict (scope, fingerprint, question_hash) do update set
                            question = excluded.question,
                            embedding = excluded.embedding,
                            answer = excluded.answer,
                            citations = excluded.citations,
                            meta = excluded.meta,
                            corpus_revision = excluded.corpus_revision,
                            policy = excluded.policy,
                            ttl_seconds = excluded.ttl_seconds,
                            created_at = excluded.created_at,
                            expires_at = excluded.expires_at,
                            hit_count = excluded.hit_count,
                            last_hit_at = excluded.last_hit_at,
                            is_active = true
                        """
                    ),
                    payload,
                )
        except SQLAlchemyError as exc:
            raise self._check_error(exc) from exc
        return existing is None

    def touch(self, entry: CacheEntry) -> None:
        """命中之后累加 `hit_count`（**失败只告警**：命中统计不该影响已回答的请求）。"""
        from sqlalchemy import text

        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        f"update {self.entries_table} set hit_count = hit_count + 1, last_hit_at = now() "
                        "where scope = :scope and fingerprint = :fingerprint and question_hash = :question_hash"
                    ),
                    {
                        "scope": entry.scope,
                        "fingerprint": entry.fingerprint,
                        "question_hash": entry.question_hash(),
                    },
                )
        except Exception as exc:  # noqa: BLE001 - 命中统计是旁路
            logger.warning("累加缓存命中次数失败（已忽略）：%s", exc)

    def deactivate(self, context: ScopeContext, question: str) -> bool:
        """按 scope + 问题失效；`context.fingerprint` 为空串时**匹配任意版本**（同内存实现）。"""
        from sqlalchemy import text

        versions = (
            "and fingerprint = :fingerprint" if context.fingerprint else ""
        )
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    f"update {self.entries_table} set is_active = false "
                    "where scope = :scope and question_hash = :question_hash "
                    f"{versions} and is_active"
                ),
                {
                    "scope": context.scope,
                    "fingerprint": context.fingerprint,
                    "question_hash": question_hash(question),
                },
            )
        return bool(result.rowcount)

    def deactivate_scope(self, scope: str) -> int:
        """按 scope 整批失效（「这个用户的缓存全清」—— 数据主体删除请求的落点）。"""
        from sqlalchemy import text

        with self.engine.begin() as conn:
            result = conn.execute(
                text(f"update {self.entries_table} set is_active = false where scope = :scope and is_active"),
                {"scope": str(scope)},
            )
        return int(result.rowcount or 0)

    def purge(self, *, limit: int | None = None, only_expired: bool = True) -> int:
        """清掉过期（或全部）条目。

        ⚠️ 默认 `only_expired=True`：一个「清空缓存」的运维动作**不该一键完成** ——
        它的代价是「接下来一段时间所有问题都重新走一遍检索 + 生成」。
        要真清空必须显式传 `only_expired=False`（`scripts/cache_report.py --purge-all`）。

        ⚠️ 过期判据用**数据库自己的时钟**（`created_at + ttl_seconds * interval '1 second' < now()`）——
        与 `count()` 同一口径，也与写库时已经对齐过的时钟域一致（见 `db_clock_skew`）。
        `ttl_seconds` 是**相对量**，所以这条判据不受时钟漂移影响。
        """
        from sqlalchemy import text

        where = (
            "created_at + (ttl_seconds * interval '1 second') < now()" if only_expired else "true"
        )
        statement = (
            f"delete from {self.entries_table} where ctid in ("
            f"  select ctid from {self.entries_table} where {where} limit :limit"
            ")"
            if limit is not None
            else f"delete from {self.entries_table} where {where}"
        )
        with self.engine.begin() as conn:
            result = conn.execute(text(statement), {"limit": int(limit or 0)} if limit is not None else {})
        return int(result.rowcount or 0)

    def list_entries(self, *, scope: str | None = None, limit: int = 100) -> list[CacheEntry]:
        """列条目（运维工具 `scripts/cache_report.py` 用；**只列不写**）。

        与 `all_entries()`（内存实现）同名同义 —— 两个实现都有这个方法，
        所以「看现场」的那个脚本不必关心后端是谁。`limit` 是硬上限：
        一个有几万条缓存的实例不该把整个表拉进内存（默认 100 条够看分布了）。
        """
        from sqlalchemy import text

        clauses = ["true"]
        if scope is not None:
            clauses.append("scope = :scope")
        statement = (
            f"select {self._COLUMNS} from {self.entries_table} "
            f"where {' and '.join(clauses)} order by created_at desc limit :limit"
        )
        with self.read_engine.connect() as conn:
            rows = conn.execute(
                text(statement),
                {"scope": scope, "limit": max(1, int(limit))} if scope else {"limit": max(1, int(limit))},
            ).all()
        return [self._row_to_entry(row) for row in rows]

    def count(self, *, scope: str | None = None, only_active: bool = True) -> int:
        """数条目；`only_active=True`（默认）只数**未过期**的。

        ⚠️ 过期判据用数据库自己的时钟（`created_at + ttl_seconds * interval '1 second' >= now()`），
        与 `purge` 同一口径。两边用不同的时钟时会出现「`count` 说 1 条、`purge` 删掉 2 条」
        这种自相矛盾的读数（本仓实测：两个时钟差 1.9 秒 —— 见 `db_clock_skew` 的注释；
        因此写进库的时间戳已经在数据库时钟域里，两种写法得到同一个答案）。
        """
        from sqlalchemy import text

        clauses = ["true"]
        if scope is not None:
            clauses.append("scope = :scope")
        if only_active:
            clauses.append(
                "is_active and created_at + (ttl_seconds * interval '1 second') >= now()"
            )
        statement = f"select count(*) from {self.entries_table} where {' and '.join(clauses)}"
        with self.read_engine.connect() as conn:
            return int(conn.execute(text(statement), {"scope": scope} if scope else {}).scalar_one())

    def clock_skew(self) -> float:
        """当前测量到的「数据库时钟 − 应用时钟」（秒）；自检与演示脚本会打出来。"""
        return db_clock_skew(self.engine)

    def health(self) -> dict[str, Any]:
        """自检：表在不在、维度一致吗、有多少条（`/health` 与演示脚本的输入）。"""
        from sqlalchemy import text

        from app.cache.schema import current_vector_dim, table_exists

        with self.read_engine.connect() as conn:
            exists = table_exists(conn, self.entries_table)
            actual_dim = current_vector_dim(conn, self.entries_table) if exists else None
            entries = (
                int(conn.execute(text(f"select count(*) from {self.entries_table}")).scalar_one())
                if exists
                else 0
            )
        return {
            "store": "postgres",
            "table": self.entries_table,
            "exists": exists,
            "dim": actual_dim,
            "expected_dim": self.dimension,
            "dim_ok": True if not self.dimension or actual_dim is None else actual_dim == self.dimension,
            "entries": entries,
        }


# ── 小工具：字面量构造（都在这里，避免 SQL 拼接散落各处）──────────────────────


def _vector_literal(embedding: Sequence[float]) -> str:
    """向量 → pgvector 字面量 `[1,2,3]`。

    为什么不用绑定参数 + `:query::vector`（第 10 步踩过的同一个坑）：
    SQLAlchemy 会把列表当成 `IN (...)` 展开，一个 1024 维向量会变成 1024 个参数。
    代价是要自己保证元素是数字 —— 所以显式 `float()` 一遍，非法值当场报错。
    """
    return "[" + ", ".join(repr(float(value)) for value in embedding) + "]"


def _json_literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _to_timestamp(value: float) -> Any:
    """epoch 秒 → `datetime`（带时区）。

    ⚠️ 必须带时区：`timestamptz` 列收到一个 naive `datetime` 时 PostgreSQL 会按
    **服务器时区**解释它 —— 而开发机（Windows）与库（Linux）的时区可以不同，
    于是「刚写入的条目立刻过期」这种事故只在跨时区时出现。
    """
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def new_entry(*, context: ScopeContext, question: str, raw_question: str, embedding: Sequence[float], answer: str, **kwargs: Any) -> CacheEntry:
    """造一条条目（把「哪些字段是必填的」收在一处，门面与用例共用）。"""
    return CacheEntry(
        scope=context.scope,
        fingerprint=context.fingerprint,
        question=str(question),
        raw_question=str(raw_question),
        embedding=tuple(float(value) for value in embedding),
        answer=str(answer),
        created_at=float(kwargs.pop("created_at", time.time())),
        **kwargs,
    )


__all__ = [
    "CacheEntry",
    "CacheMatch",
    "CacheStoreError",
    "EntryStore",
    "LookupLog",
    "MemoryEntryStore",
    "PostgresEntryStore",
    "ScopeContext",
    "cosine_similarity",
    "new_entry",
    "question_hash",
    "scope_for",
]
