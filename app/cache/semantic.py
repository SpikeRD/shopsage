"""语义缓存（`13.cache.md` §2 的 `semantic_cache_get/set` 的实现）与它用到的 embedding 适配器。

## 一次「问 → 答」在缓存里的完整形状

```python
cache = default_cache()
prepared = cache.prepare(messages, user_id=2, role="customer")   # 查（同步）
if prepared.hit:
    answer, citations = prepared.answer, prepared.citations       # 直接返回，不检索、不打模型
else:
    result = await do_the_real_work()                             # 正常检索 + 生成
    prepared.respond(result.answer, citations=result.citations, meta={"kind": "rag"})   # 写
```

**为什么是「两段式」（`prepare` + `respond`）而不是 `get` / `set` 两个独立函数**：

1. `set` 需要的东西（scope / fingerprint / 决策 / revision）**全是 `get` 那一步算出来的**。
   拆成两个函数就意味着调用方要把这些中间结果一路带着 —— 而「忘了带 fingerprint」
   是一个不会报错的错误：它会用**当前**的版本写进去，于是「知识库刚变了、
   但这次用的是旧知识库的结果」这种时序问题会把旧答案重新写回缓存。
2. 两段式让「跳过缓存」在类型上就是一次 `if`：`prepared.bypassed` 为真时
   `respond()` 什么都不做（强时效问题在 `prepare` 那一步就决定不查也不写）。

## 一次查询的耗时账（决定顺序的唯一依据）

| 步骤 | 实测量级 | 什么时候发生 |
|---|---|---|
| ① 策略判定（`intent` 的正则） | **2µs** | 每次 |
| ② Redis 关键值查询（完全同问） | 0.2ms | 每次（除非 ① 跳过） |
| ③ 版本查询（一次主键 select） | 0.5ms | ② 未命中时 |
| ④ embedding（**Redis 里先查向量缓存**） | 30ms（本地 bge-m3）/ 0.3ms（缓存命中） | ②③ 都未命中时 |
| ⑤ 相似度查询（Postgres HNSW） | 1~2ms | ④ 之后 |

所以顺序是 ①→②→③→④→⑤：把最便宜、最能挡掉后续步骤的放在最前面。
**唯一一处反直觉的是 ③ 排在 ④ 前面**：版本查询要连库，而它在 ④ 未命中时反正也要做
（写缓存时要版本）。提前到 ④ 之前让「库不可用」这条路径**不必白算一次向量** ——
一次 30ms 的本地 embedding 比一次 0.5ms 的 select 贵 60 倍。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.cache.backend import CacheKeySpace, EmbeddingCache
from app.cache.core import (
    DEFAULT_CONTEXT_TURNS,
    OUTCOME_ERROR,
    OUTCOME_HIT,
    CacheDecision,
    LookupRequest,
    assemble_question,
    build_entry,
    decide,
    fingerprint_for,
    judge,
)
from app.cache.corpus import UNKNOWN_REVISION, RevisionStore, current_revision
from app.cache.settings import CacheSettings
from app.cache.stores import EntryStore, LookupLog, ScopeContext, scope_for
from app.observability import metrics as M
from app.observability.tracing import telemetry

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """把一段文本向量化（同步，接受 `cache` 参数以便本地/API 后端共用一套配置）。"""

    def __call__(self, text: str, *, cache: CacheSettings) -> Sequence[float]: ...


class DefaultEmbedder:
    """生产 embedding 适配器：走 `app.llm.embed`（**唯一允许打 embedding 模型的门面**）。

    三件它必须处理好的事：

    1. **同步 ↔ 异步**：`app.llm.embed` 是 async，而缓存链路有同步调用方
       （`semantic_cache_get` 在同步上下文里被调用）。判定用
       `asyncio.get_running_loop()`（**不是** `get_event_loop()`：
       后者在没有循环时 Python 3.12 会抛 DeprecationWarning 甚至 RuntimeError）。
    2. **单条向量**：`embed()` 收一批、返一批；这里只问一句，取 `[0]` 并校验条数。
    3. **异常原样抛**：把「embedding 挂了」翻译成什么，是**上层**的决定
       （对 `prepare` 来说它是「缓存不可用」→ 未命中；不该在这一层被做成 return None，
       那样调用方分不清「没有向量」与「向量是空」）。
    """

    def __init__(self, *, model: str = "", backend: str = "") -> None:
        self.model = str(model)
        self.backend = str(backend)

    def resolve_model(self, cache: CacheSettings) -> str:
        """取**实际会用**的模型名（进版本指纹，所以它必须与真调用一致）。"""
        if self.model:
            return self.model
        from app.config import get_settings

        settings = get_settings()
        return str(getattr(settings, "embedding_model", "") or "")

    def __call__(self, text: str, *, cache: CacheSettings) -> Sequence[float]:
        del cache  # 参数只为「两套后端共用签名」
        vectors = _run_awaitable(_call_embed([text]))
        if len(vectors) != 1:
            raise RuntimeError(
                f"embedding 返回条数与入参不一致：期望 1，实际 {len(vectors)}"
                "（不猜顺序，避免向量与文本错配）"
            )
        return vectors[0]


async def _call_embed(texts: list[str]) -> list[list[float]]:
    from app.llm.client import embed

    return await embed(texts)


def _run_awaitable(awaitable: Any) -> Any:
    """在一个**同步**函数里驱动一个协程（有事件循环时丢进线程，没有时 `asyncio.run`）。

    为什么要有这么一个东西：缓存的门面函数既被同步代码调用（`semantic_cache_get`
    在脚本/测试里），也被异步节点调用（`rag_node`）。两种上下文都要能用同一个
    embedding 适配器 —— 各写一份必然漂移（后端切换只改一处的那种一致性会丢）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    # 已在事件循环里：不能 `asyncio.run`（会抛 "cannot be called from a running event loop"）。
    # 丢进一个新线程跑一个自己的循环 —— 代价是一次线程 + 一次循环创建（约 0.1ms），
    # 换来「同步门面在 async 上下文里也能用」。
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # noqa: BLE001 - 原样转交给调用方
            box["error"] = exc

    thread = threading.Thread(target=runner, name="cache-embed", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


@dataclass
class Prepared:
    """一次 `prepare()` 的结果：**要么直接可用（命中），要么带着写入所需的全部上下文**。

    ⚠️ `fingerprint` 是**唯一**的版本指纹：`respond()` 必须用它，不能再自己算一遍
    （两处算 = 两个指纹 = 写进去的永远命中不了，见 `SemanticCache.fingerprint_for`）。
    """

    log: LookupLog
    scope: str
    fingerprint: str
    question: str
    raw_question: str
    revision: int
    decision: CacheDecision
    #: 这一次用的**检索参数**（`top_k` / `min_score` / `embedder_key`）——
    #: 写路径必须原样带上它们（见 `remember_answer` 的说明：漏一个就永远不命中）。
    params: dict[str, Any] = field(default_factory=dict)
    #: **用来算缓存键的那一串消息**（往往是读那一刻的 `messages`，而不是写那一刻的）。
    #:
    #: ⚠️ 为什么要把「读用的消息」也带出来（本仓实测踩到的第二个非对称）：
    #: 图会在 `synthesis` 之后把这一轮的问答**追加进 `messages`**（`add_messages`），
    #: 所以「写缓存时手里的 `messages`」比「读缓存时的那一份」多了一条 assistant 消息 ——
    #: 于是两处拼出的问题串不同、缓存键不同，**写进去的条永远读不到**
    #: （症状与指纹不一致完全一样：命中率恒为 0）。
    #: 所以读那一刻的消息必须被带出来，写的时候原样用。
    context_messages: tuple[Any, ...] = ()
    embedding: tuple[float, ...] = ()
    #: 命中时的答案与出处（未命中时分别是空串与空元组）
    answer: str = ""
    citations: tuple[str, ...] = ()

    @property
    def hit(self) -> bool:
        return self.log.hit

    @property
    def bypassed(self) -> bool:
        return self.decision.bypassed

    def age_seconds(self) -> float:
        entry = self.log.entry
        return entry.age_seconds() if entry is not None else 0.0

    def summary(self) -> str:
        return self.log.summary()


@dataclass
class WriteLog:
    """一次 `respond()` 的账目（写入是否真的发生、为什么没发生）。"""

    written: bool
    reason: str = ""
    bytes_written: int = 0
    ttl_seconds: int = 0
    detail: str = ""

    def summary(self) -> str:
        if self.written:
            return f"已写缓存（{self.ttl_seconds}s / {self.bytes_written}B）"
        return f"未写缓存：{self.reason}"


class SemanticCache:
    """语义缓存的编排者：把「判定（`core`）」「存储（`stores`）」「成本（`backend`）」串起来。

    ⚠️ 它**不做命中判定**，只负责「按成本从低到高去问」并把结果交给 `core.judge`。
    判定逻辑全在 `core.py` 里（纯函数、离线可测），这一层是「接线」——
    接线出错的典型症状是「用了错的 scope」或「顺序反了」，
    而这两件事都有专门的结构用例钉着（`test_cache_contracts.py`）。
    """

    def __init__(
        self,
        *,
        store: EntryStore,
        embedder: Embedder | None = None,
        revisions: RevisionStore | None = None,
        settings: CacheSettings | None = None,
        keys: CacheKeySpace | None = None,
        embedding_cache: EmbeddingCache | None = None,
        enable_metrics: bool = True,
        degraded: bool = False,
    ) -> None:
        self.store = store
        self.embedder = embedder or DefaultEmbedder()
        self.revisions = revisions
        self.settings = settings or CacheSettings()
        self.keys = keys or CacheKeySpace()
        self.embedding_cache = embedding_cache
        self.enable_metrics = bool(enable_metrics)
        #: 存储是否处于降级形态（Redis 不可用 → 内存）。**由构造方注入**而不是每次
        #: 查询时问一遍存储：`ResilientEntryStore.degraded` 是一个内存里的布尔，
        #: 而 `store.health()` 对 Redis 后端是一次全量 `scan`（见 `RedisEntryStore.count`）——
        #: 把它放进热路径等于给每次缓存查询加一次 O(N)。第一版就是这么写的。
        self.degraded = bool(degraded)
        #: 最近一次 `prepare` 是不是多轮会话（演示与自检读它，用来解释「为什么这条没命中」）
        self.last_multi_turn = False
        #: 计数（自检与演示脚本读它；指标是另一个出口，两者不互相替代）
        self.stats: dict[str, int] = {"hit": 0, "miss": 0, "stale": 0, "bypassed": 0, "error": 0, "written": 0}

    def fingerprint_for(self, params: Mapping[str, Any] | None = None) -> str:
        """算出「这一组检索参数」对应的版本指纹。

        ⚠️ **只有 `params` 里能放「会改变答案」的量**（`top_k` / `min_score` / 检索后端标识）。
        第一版把写路径的记账标志（`llm_calls_saved`）也混了进来，
        于是读路径与写路径算出两个不同的指纹 —— 「写了但永远不命中」，
        而两边代码各自看起来都对（见 runbook §10.1 坑 1）。
        """
        return self._fingerprint_with(self._revision(), params or {})

    def _fingerprint_with(self, revision: int, params: Mapping[str, Any]) -> str:
        model = getattr(self.embedder, "resolve_model", lambda _settings: "")(self.settings)
        return fingerprint_for(corpus_revision=revision, embedding_model=model, params=params)

    # ── 查 ────────────────────────────────────────────────────────────────
    def prepare(
        self,
        messages: Sequence[Any],
        *,
        user_id: int,
        role: str,
        params: Mapping[str, Any] | None = None,
        now: float | None = None,
        context_turns: int = DEFAULT_CONTEXT_TURNS,
    ) -> Prepared:
        """查缓存：返回一个 `Prepared`（命中就直接用它的答案）。

        全过程**同步**：它的调用方要么本来就是同步的，要么在 async 节点里
        （`rag_node` 直接调它 —— 一次缓存查询是 2ms 级的，为它多留一层
        `to_thread` 的收益还不如它的开销；真正贵的是 embedding，
        那一步由 `_embed` 自己决定要不要丢线程）。

        `params` 是**检索参数**（`top_k` / `min_score` / `embedder_key`…），
        它们进版本指纹。为什么必须进：缓存下来的是「**用这几个参数查出来的答案**」——
        `top_k=5` 与 `top_k=20` 不是同一个问题。不把它们算进去，
        改配置（`RAG_MAX_TOP_K`）之后缓存会继续返回旧参数下的答案，
        而那种错误看起来完全正常（答案确实来自知识库）。

        ⚠️ `params` 是**显式的一个映射**而不是 `**kwargs`：`**kwargs` 会把
        「顺手多传一个记账标志」也吸进指纹里，而那正是本步最贵的那个坑（见 `fingerprint_for`）。
        要传的那一组参数由 `app/cache/params.py` 的 `retrieval_params()` 统一构造。
        """
        retrieval = dict(params or {})
        question, multi_turn = assemble_question(messages, context_turns=context_turns)
        raw = _last_user_message(messages)
        scope = scope_for(user_id, role)
        decision = decide(raw or question, settings=self.settings)
        self.last_multi_turn = bool(multi_turn)

        if not question:
            # 没有用户消息：不查也不写（route=final 的路径）。返回一个「跳过」的账目，
            # 而不是抛异常 —— 「这一轮没有可缓存的问题」是正常状态。
            log = LookupLog(outcome="bypassed", detail="会话里没有用户消息", policy=decision.tier)
            return Prepared(
                log=log,
                scope=scope,
                fingerprint="",
                question="",
                raw_question="",
                revision=UNKNOWN_REVISION,
                decision=decision,
                params=retrieval,
                context_messages=tuple(messages),
            )

        if self.settings.enabled is False:
            log = LookupLog(outcome="disabled", detail="CACHE_ENABLED=false", policy=decision.tier)
            return Prepared(
                log=log,
                scope=scope,
                fingerprint="",
                question=question,
                raw_question=raw,
                revision=UNKNOWN_REVISION,
                decision=decision,
                params=retrieval,
                context_messages=tuple(messages),
            )

        if decision.bypassed:
            # ① 策略闸：**连存储都不碰**（强时效数据的问题，答案没有正确性可言）
            log = LookupLog(
                outcome="bypassed",
                detail=decision.reason or decision.rule,
                policy=decision.tier,
            )
            self._count("bypassed", decision.tier)
            self._record_lookup(log)
            logger.debug("缓存跳过：%r（%s）", raw[:30], decision.reason or decision.rule)
            return Prepared(
                log=log,
                scope=scope,
                fingerprint="",
                question=question,
                raw_question=raw,
                revision=UNKNOWN_REVISION,
                decision=decision,
                params=retrieval,
                context_messages=tuple(messages),
            )

        started = time.perf_counter()
        revision = self._revision()
        # 指纹只算一次、只算在这里；写路径复用 `prepared.fingerprint`（见 `fingerprint_for`）
        fingerprint = self._fingerprint_with(revision, retrieval)
        context = ScopeContext(scope=scope, fingerprint=fingerprint)

        # ② 完全同问（Redis 的 O(1) GET）
        exact = self._safe(lambda: self.store.get(context, question))
        candidates: tuple[Any, ...] = ()
        embedding: tuple[float, ...] = ()
        if exact is None:
            # ③ 版本已经拿到；④ 算向量（先查 embedding 缓存）；⑤ 相似度查询
            embedding = self._embed(question)
            if embedding:
                found = self._safe(lambda: self.store.nearest(context, embedding, limit=self.settings.semantic_candidates))
                candidates = tuple(found or ())

        request = LookupRequest(
            question=question,
            raw_question=raw,
            scope=scope,
            fingerprint=fingerprint,
            revision=revision,
            decision=decision,
            exact=exact,
            candidates=candidates,
            degraded=self.degraded,
        )
        log = judge(request, settings=self.settings, now=now)
        log = LookupLog(**{**log.__dict__, "elapsed_ms": (time.perf_counter() - started) * 1000.0})
        if log.nearest is not None and not log.hit:
            # 灰区：**单独记一条**（阈值校准的唯一在线信号）
            self._counter(M.CACHE_NEAR_MISS, {"policy": decision.tier})
        self._count(log.outcome, decision.tier)
        self._record_lookup(log)
        self._trace(log, decision, fingerprint)
        if log.hit and log.entry is not None:
            self._safe(lambda: self.store.touch(log.entry))  # 命中计数（旁路，失败只告警）
            self._counter(M.CACHE_SAVED_CALLS, {"policy": decision.tier})
            logger.info(
                "缓存命中：%r → %r（相似度 %s，躺了 %.0fs）",
                raw[:30],
                str(log.entry.question)[:30],
                f"{log.similarity:.4f}" if log.similarity is not None else "完全同问",
                log.entry.age_seconds(),
            )
        return Prepared(
            log=log,
            scope=scope,
            fingerprint=fingerprint,
            question=question,
            raw_question=raw,
            revision=revision,
            decision=decision,
            params=retrieval,
            context_messages=tuple(messages),
            embedding=embedding,
            answer=str(log.entry.answer) if log.entry is not None and log.hit else "",
            citations=tuple(log.entry.citations) if log.entry is not None and log.hit else (),
        )

    def record(
        self,
        *,
        question: str,
        raw_question: str,
        scope: str,
        fingerprint: str,
        revision: int,
        decision: CacheDecision,
        answer: str,
        citations: Sequence[str] = (),
        meta: Mapping[str, Any] | None = None,
        embedding: Sequence[float] | None = None,
        now: float | None = None,
    ) -> WriteLog:
        """**写缓存的唯一实现**：所有输入都是显式参数（全部可序列化）。

        ## 为什么不是「把 `Prepared` 传进来」（本仓实测踩到的第三处非对称）

        `respond(prepared, ...)` 看起来更省事，但 `Prepared` **无法穿过图**：
        LangGraph 的 checkpointer 会把 state 值做 msgpack 序列化 ——
        一个 dataclass 会被静默丢掉、一个闭包会直接抛
        `TypeError: Type is not msgpack serializable: function`。
        所以「读那一刻的上下文」只能以**可序列化的字段**（字符串 / 整数 / dict）
        带过去，再由这些字段重建出 `Prepared`。

        算法与 `respond` 完全一致（`respond` 现在只是它的一层解包），
        所以「两条路径行为一致」是结构性的，而不是靠人记得改两处。
        """
        prepared = Prepared(
            log=LookupLog(outcome="miss", detail="由 record() 直接构造（写路径）"),
            scope=scope,
            fingerprint=fingerprint,
            question=question,
            raw_question=raw_question,
            revision=int(revision),
            decision=decision,
            embedding=tuple(embedding or ()),
        )
        return self.respond(
            prepared, answer, citations=citations, meta=meta, embedding=embedding, now=now
        )

    # ── 写 ────────────────────────────────────────────────────────────────
    def respond(
        self,
        prepared: Prepared,
        answer: str,
        *,
        citations: Sequence[str] = (),
        meta: Mapping[str, Any] | None = None,
        embedding: Sequence[float] | None = None,
        llm_calls_saved: bool = False,
        now: float | None = None,
        fingerprint: str | None = None,
    ) -> WriteLog:
        """把这次生成的结果写进缓存（**六种情况下什么都不做**，每一种都有理由）。

        写缓存不是「总要发生」的动作 —— 下面每一条「不写」的原因都比「写」更常见：

        | 不写的原因 | 为什么 |
        |---|---|
        | 策略跳过了（强时效） | F1：写了就是错的 |
        | 缓存被关闭 | `CACHE_ENABLED=false`（压测对照） |
        | 回答是空的 | 空的缓存值 = 之后所有命中都返回空回答（最坏的一类静默故障） |
        | 命中的那一条**没有过期** | 已经有一条更好的（旧但已验证的）答案躺在那儿，重写会刷新它的年龄、掩盖「它其实很久没被真正校验过」这件事 |
        | **指纹对不上** | 写进去的那条之后永远读不到（见下） |
        | 存储写失败 | fail-open（缓存写不进去不该让用户看到错误） |

        ⚠️ 「命中的那一条没过期就重写」被明确拒绝，是因为它会**掩盖缓存的年龄**：
        每次用户问都重新生成、重新写一遍，缓存里那条的 `created_at` 永远是刚才 ——
        于是「这条答案到底多久没被真正校验过」永远查不出来（而它恰恰是排障第一问）。

        ## `fingerprint=` 是**锁**，不是选项

        它由 `app/agents/rag.py::remember_answer` 从「读那一步的 `Prepared.params`」
        算出来并显式传进来 —— 因为**指纹只该由「读」那一步决定**。
        对不上就**拒绝写入并打 WARNING**：写一条「之后永远读不到」的记录毫无价值，
        而它的症状（命中率莫名其妙地低）极难查。这是本步实测到的坑
        （读路径带 `top_k`/`min_score`/`embedder_key`、写路径漏了其中一个 →
        第一次问答写进缓存、第二次问同一个问题**仍然未命中**，见 runbook §10.1 坑 1）。
        """
        if fingerprint is not None and prepared.fingerprint and fingerprint != prepared.fingerprint:
            logger.warning(
                "拒绝写缓存：写路径算出的指纹（%s）与读路径的（%s）不一致 —— "
                "两边的检索参数必须逐字一致（top_k / min_score / embedder_key）。",
                fingerprint,
                prepared.fingerprint,
            )
            return WriteLog(written=False, reason="指纹与读路径不一致（写进去也读不到）")
        if prepared.bypassed or not prepared.question:
            return WriteLog(written=False, reason=prepared.decision.reason or "按策略跳过")
        if self.settings.enabled is False:
            return WriteLog(written=False, reason="缓存已关闭")
        if str(answer or "").strip() == "":
            # **空回答绝不进缓存**：它会让之后每一次命中都返回一个空回答，
            # 而「模型这次没说话」与「缓存里存的是空」在用户看来完全一样。
            return WriteLog(written=False, reason="回答为空（空值进缓存会让以后每次都返回空）")
        if prepared.log.hit:
            return WriteLog(written=False, reason="已命中且未过期（不刷新旧条目的年龄）")

        vector = tuple(embedding) if embedding is not None else prepared.embedding
        if not vector:
            vector = self._embed(prepared.question)
        if not vector:
            return WriteLog(written=False, reason="拿不到问题向量（embedding 不可用）")

        entry = build_entry(
            LookupRequest(
                question=prepared.question,
                raw_question=prepared.raw_question,
                scope=prepared.scope,
                fingerprint=prepared.fingerprint,
                revision=prepared.revision,
                decision=prepared.decision,
            ),
            answer=str(answer),
            embedding=vector,
            citations=citations,
            meta={"saved_llm_call": bool(llm_calls_saved), **dict(meta or {})},
            settings=self.settings,
            created_at=now,
        )
        try:
            created = self.store.put(entry)
        except Exception as exc:  # noqa: BLE001 - 写缓存失败是 fail-open
            logger.warning("写缓存失败（已忽略，不影响本次回答）：%s", exc)
            return WriteLog(written=False, reason=f"写入失败：{type(exc).__name__}")

        self.stats["written"] += 1
        self._gauge(M.CACHE_ENTRIES, float(self._count_active()), {"store": self.store.name()})
        ttl = int(prepared.decision.ttl_seconds)
        detail = "新建" if created else "覆盖同问的旧条目"
        logger.info("缓存写入：%r（%s，TTL %ds，出处 %d 条）", prepared.raw_question[:30], detail, ttl, len(entry.citations))
        return WriteLog(written=True, bytes_written=len(entry.answer.encode("utf-8")), ttl_seconds=ttl, detail=detail)

    # ── 内部：成本从低到高的三步 ──────────────────────────────────────────
    def _revision(self) -> int:
        return current_revision(self.revisions)

    def _embed(self, text: str) -> tuple[float, ...]:
        """算问题向量（Redis 里先查一遍 embedding 缓存）。"""
        model = getattr(self.embedder, "resolve_model", lambda _settings: "")(self.settings)
        if self.embedding_cache is not None and model:
            cached = self.embedding_cache.get(model=model, text=text)
            if cached:
                return tuple(float(value) for value in cached)
        try:
            vector = tuple(float(value) for value in self.embedder(text, cache=self.settings))
        except Exception as exc:  # noqa: BLE001 - embedding 不可用 = 缓存不可用（不是请求失败）
            logger.warning(
                "计算问题向量失败，本次缓存不可用（业务不受影响，只是会多打一次模型）：%s: %s",
                type(exc).__name__,
                exc,
            )
            self._counter(M.CACHE_RESULTS, {"outcome": OUTCOME_ERROR, "policy": "n/a"})
            return ()
        if self.embedding_cache is not None and model and vector:
            self.embedding_cache.set(model=model, text=text, vector=vector)
        return vector

    def _safe(self, call: Any) -> Any:
        """存储调用的一次包装：出错 → 记 ERROR 账目 + 返回 `None`（**fail-open**）。

        ⚠️ 这里**不做降级到内存**（那是 `backend.ResilientEntryStore` 的职责，
        它在传输层就把错误吃掉了）。所以这里能接到的错误是「降级也没有兜住」的那一类：
        例如表不存在、维度不符。它们必须被记下来，否则「缓存怎么一直不命中」
        会查很久 —— 而它在日志里一句都没有。
        """
        try:
            return call()
        except Exception as exc:  # noqa: BLE001
            logger.warning("缓存存储调用失败（本次按未命中处理）：%s: %s", type(exc).__name__, exc)
            self._counter(M.CACHE_RESULTS, {"outcome": OUTCOME_ERROR, "policy": "n/a"})
            return None

    def store_health(self) -> dict[str, Any]:
        try:
            return dict(self.store.health())
        except Exception as exc:  # noqa: BLE001 - 自检不该抛
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _count_active(self) -> int:
        try:
            return int(self.store.count())
        except Exception:  # noqa: BLE001
            return 0

    # ── 内部：指标与链路（**全部走 observability，绝不自己造一个出口**）──
    def _metrics(self) -> Any:
        return telemetry().metrics

    def _counter(self, name: str, labels: Mapping[str, str]) -> None:
        if not self.enable_metrics:
            return
        self._metrics().counter(name, dict(labels))

    def _gauge(self, name: str, value: float, labels: Mapping[str, str]) -> None:
        if not self.enable_metrics:
            return
        self._metrics().gauge(name, float(value), dict(labels))

    def _count(self, outcome: str, policy: str) -> None:
        if outcome in self.stats:
            self.stats[outcome] += 1
        self._counter(M.CACHE_RESULTS, {"outcome": outcome, "policy": policy})

    def _record_lookup(self, log: LookupLog) -> None:
        self._observe(log)
        if log.degraded:
            self._gauge(M.CACHE_DEGRADED, 1.0, {"store": self.store.name()})
        else:
            self._gauge(M.CACHE_DEGRADED, 0.0, {"store": self.store.name()})

    def _observe(self, log: LookupLog) -> None:
        if not self.enable_metrics:
            return
        self._metrics().observe(
            M.CACHE_LOOKUP_DURATION, max(0.0, log.elapsed_ms / 1000.0), {"outcome": log.outcome}
        )

    def _trace(self, log: LookupLog, decision: CacheDecision, fingerprint: str) -> None:
        """把这次查询挂进**当前活跃的 span**（只放白名单里的低基数标签，见 `tracing.ALLOWED_ATTRIBUTES`）。

        实现上直接用 OTEL 的 `get_current_span()` 而**不是**本仓的门面：
        `telemetry()` 手里只有「配置好的 provider + exporter」，没有「当前 span」这个概念
        （那是 OTEL 的 contextvars 管的，门面刻意没去包它 —— 见 `node_span` 的说明）。
        没有活跃 span 时（脚本、单测、同步调用）它就是一个空操作，所以整段是防御性的。

        ⚠️ 问题文本**不进 span**：它可能含 PII（订单号、手机号），而 span 会进 trace UI
        与外部 Langfuse。要看「命中的是哪一条」请用日志（同进程内）或
        `scripts/cache_report.py`（本地工具），它们都不出网。
        """
        try:
            from opentelemetry import trace as otel_trace

            span = otel_trace.get_current_span()
            if not getattr(span.get_span_context(), "is_valid", False):
                return
            span.set_attribute("cache_outcome", log.outcome)
            span.set_attribute("cache_policy", decision.tier)
            span.set_attribute("cache_hit", bool(log.hit))
            if fingerprint:
                span.set_attribute("cache_fingerprint", fingerprint)
        except Exception:  # noqa: BLE001 - 埋点是旁路，永不改变控制流
            logger.debug("写缓存 span 属性失败（忽略）", exc_info=True)


def _last_user_message(messages: Sequence[Any]) -> str:
    for message in reversed(list(messages or ())):
        role = (message.get("role") if isinstance(message, dict) else getattr(message, "role", "")) or ""
        if str(role) == "user":
            content = (message.get("content") if isinstance(message, dict) else getattr(message, "content", "")) or ""
            return str(content).strip()
    return ""


__all__ = [
    "DefaultEmbedder",
    "Embedder",
    "Prepared",
    "SemanticCache",
    "WriteLog",
]
