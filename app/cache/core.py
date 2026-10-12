"""缓存的**判断内核**：阈值、TTL、版本、灰区 —— 全部是纯函数，可离线逐条钉住。

这一层刻意**不 import** Redis、SQLAlchemy、FastAPI，也**不查库**：
它只吃「已经拿到手的候选」与「策略」，吐出「命中 / 未命中 / 失效 / 跳过」。
于是 `13.cache.md` §4 第 3 条那句「阈值要保守」可以被十来条纯逻辑用例逐点钉住，
而不必为每条用例起一个 PostgreSQL（与第 12 步的 `queue/core.py` 是同一个分层理由）。

## 一次查询要过几道闸（顺序即语义）

```
  ① 策略闸：强时效问题（订单/库存/物流）→ 直接跳过（不查任何存储）
  ② 完全匹配：同一 scope + 同一版本 + 同一句话（hash）→ 命中，阈值完全不参与
  ③ 相似度闸：最近邻的相似度 < threshold → 未命中（但灰区候选会进账目）
  ④ 时效闸：TTL 过期 → 失效（不是「未命中」——两者的运维动作不同）
  ⑤ 版本闸：写入时的知识库版本 ≠ 当前版本 → 失效
```

**为什么 ① 是第一道**：强时效问题的答案**没有正确性可言**（订单 5 分钟前发的货，
现在可能已经签收了）。对它们来说，「查一次存储」本身就把一次错误的可能性引入了，
所以最省事也最安全的处置是**根本不查**（`13.cache.md` §4 第 2 条的 `no_cache`）。

**为什么 ④⑤ 要与「未命中」分开报**：两者的运维动作完全不同 ——
「未命中」是正常的（缓存本来就该有 misses，E1 的命中率就是它算出来的），
而「失效」意味着**发生了某件事**（时间到了 / 知识库变了）。把它们混成一个 `None`，
「知识库更新之后缓存有没有被清掉」这条验收就永远答不出来（`13.cache.md` §7 的
`test_invalidate_on_change` 正是要回答它）。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Protocol

from app.cache.settings import CacheSettings
from app.cache.stores import (
    CacheEntry,
    CacheMatch,
    EntryStore,
    LookupLog,
    ScopeContext,
    new_entry,
)

logger = logging.getLogger(__name__)

#: 「缓存已关闭」/「按策略跳过」这类**不是 miss** 的结果，单独一个取值
OUTCOME_HIT: Final[str] = "hit"
OUTCOME_MISS: Final[str] = "miss"
OUTCOME_STALE: Final[str] = "stale"
OUTCOME_BYPASSED: Final[str] = "bypassed"
OUTCOME_DISABLED: Final[str] = "disabled"
OUTCOME_ERROR: Final[str] = "error"

#: 版本指纹里 prompt 的版本号。**改了任何影响答案的 prompt/模板就 +1**
#: （第 7 步的 router/synthesis/guardrail、第 10 步的检索口径）。
#: 这是第三级失效里「prompt 版本变了全局失效」的那一半（`13.cache.md` §5 第 3 条）。
PROMPT_VERSION: Final[str] = "p13"

#: 默认用最近几轮用户消息拼问题（见 `assemble_question`）
DEFAULT_CONTEXT_TURNS: Final[int] = 3


class Embedder(Protocol):
    """把问题向量化。**唯一允许调 embedding 模型的地方是注入进来的这个**。

    为什么是协议而不是直接调 `app.llm.embed`：这样离线用例不必加载 bge-m3
    （15 秒 + 1.3GB 显存/内存），而演示脚本可以用真模型 —— 与第 10 步
    `rag/retriever.py` 的 `embedder=` 注入点是同一个理由。
    """

    def __call__(self, text: str) -> Sequence[float]: ...


# ── 强时效（F1）：哪些问题不许缓存 ──────────────────────────────────────────


@dataclass(frozen=True)
class CacheDecision:
    """「这个问题能不能缓存、缓存多久」的结论（**唯一一处**做这个判断）。"""

    #: `bypass`（不缓存）/ `cache`（可缓存）
    action: str
    #: 分档：`normal` / `policy` / `time_sensitive`
    tier: str
    ttl_seconds: int
    #: 命中了哪条规则（排障与用例断言用；没有规则命中时是空串）
    rule: str = ""
    reason: str = ""

    @property
    def bypassed(self) -> bool:
        return self.action == "bypass"

    def summary(self) -> str:
        if self.bypassed:
            return f"不缓存（{self.tier}：{self.reason or self.rule or '按策略'}）"
        return f"可缓存 {self.ttl_seconds}s（{self.tier}{'：' + self.rule if self.rule else ''}）"


#: 「强时效」的规则表：`(规则名, 正则)`。
#:
#: ⚠️ 这是本模块**最像启发式**的一处，所以写得刻意保守、且每一条都有用例：
#: 只认那些「答案必然随时间变化」的问法。判错的代价是不对称的 ——
#: **多拦**只是少一次缓存命中（成本高一点），**漏拦**是把一个过期的答案给用户
#: （对客事故）。所以宁可多拦。
#:
#: ⚠️ `order_lookup` 刻意**只认「订单/快递/物流/包裹/发货」这几个名词**，
#: 不要求前面有「我的」：第一版写成 `(我的|我那个).{0,8}(订单|…)`，
#: 于是「帮我查一下订单 1024 的状态」这种没有「我的」的问法**漏拦了** ——
#: 而它恰恰是最常见的对客问法。名词本身已经足够说明「这是强时效数据」：
#: 「订单/物流」类问题的答案**没有正确性可言**（5 分钟前发的货现在可能已签收）。
#:
#: 与 router 的意图分类的关系：router 判的是 `data` / `rag` / `analysis`，
#: 而本表判的是「这个问题**等一分钟**答案会不会变」。两者不等价 ——
#: 「本月退货了几单」（data）可以缓存几分钟，而「我的订单 1024 到哪了」（data）
#: 一分钟都不能缓存。所以这里不看 route，只看问法。
TIME_SENSITIVE_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("order_status", r"(到哪|到哪了|到没到|什么时候能?到|发货了吗|发了吗|寄出|签收了吗|物流(状态|进度|信息|到哪)|快递(到哪|状态)|运单)"),
    ("order_lookup", r"(订单|快递|物流|包裹|发货)"),
    ("stock", r"(还有(货|库存)|有没有货|(剩余|当前|现在)?库存(还有|还剩|剩|多少)|有没有现货|现货吗|能不能拍|有货吗)"),
    ("price_now", r"(现在|此刻|实时).{0,6}(多少(钱|元)|价格|报价|余额|积分)"),
    ("account_now", r"(我的|我).{0,4}(余额|积分|优惠券|账户状态)"),
    ("realtime", r"(实时|最新).{0,6}(状态|进度|数据|库存|价格)"),
)

#: 「政策类」的规则表：这些问题的答案**只随文档变动**，所以 TTL 可以长得多。
#: 判据刻意窄（只看「政策/条款/规定/流程/怎么算」这类词）：宽了会把
#: 「我的退货进度」也算成政策类 —— 那条恰恰是强时效的。
POLICY_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("policy_doc", r"(政策|条款|规定|规则|制度|协议|说明书|指南|流程是|怎么算|如何计算|标准是)"),
    ("warranty_faq", r"(保修|三包|发票|开票|售后范围|常见问题|FAQ)"),
)


def _first_match(rules: Sequence[tuple[str, str]], text: str) -> str:
    import re

    for name, pattern in rules:
        if re.search(pattern, text, re.IGNORECASE):
            return name
    return ""


def classify_cache_intent(text: str) -> tuple[str, str]:
    """判分档：返回 `(分档, 命中的规则名)`。

    顺序**不能换**：先看强时效（宁可把它拦掉），再看政策类。
    反过来的话「退货政策里的订单状态怎么查」会被判成政策类并缓存一周 ——
    而它的答案明天就不对了。
    """
    haystack = str(text or "")
    rule = _first_match(TIME_SENSITIVE_RULES, haystack)
    if rule:
        return "time_sensitive", rule
    rule = _first_match(POLICY_RULES, haystack)
    if rule:
        return "policy", rule
    return "normal", ""


def decide(text: str, *, settings: CacheSettings | None = None) -> CacheDecision:
    """这个问题该怎么缓存（`13.cache.md` §4 第 2 条的全部实现）。

    两种强时效处置（配置项 `time_sensitive_policy`）：
      * `bypass`（默认）—— 完全不缓存。**不可能被误读成「缓存里是旧值」**；
      * `short` —— 缓存 `time_sensitive_ttl_seconds`（默认 60s）。
        适用「同一秒内被反复问」的场景（用户狂点刷新），代价是 60 秒的陈旧窗口。
    """
    resolved = settings or CacheSettings()
    tier, rule = classify_cache_intent(text)

    if tier == "time_sensitive":
        if resolved.time_sensitive_policy == "short":
            return CacheDecision(
                action="cache",
                tier=tier,
                ttl_seconds=int(resolved.time_sensitive_ttl_seconds),
                rule=rule,
                reason="强时效但配置为短 TTL",
            )
        return CacheDecision(
            action="bypass", tier=tier, ttl_seconds=0, rule=rule, reason="强时效数据必须实时查库"
        )

    if tier == "policy":
        return CacheDecision(
            action="cache",
            tier=tier,
            ttl_seconds=int(resolved.policy_ttl_seconds),
            rule=rule,
            reason="政策/文档类：只随文档变动",
        )

    return CacheDecision(
        action="cache", tier=tier, ttl_seconds=int(resolved.ttl_seconds), rule=rule, reason=""
    )


def is_time_sensitive(text: str) -> bool:
    """一句话判「是不是强时效」（`13.cache.md` §6 第 4 步要的那个 `no_cache` 标）。

    ⚠️ **只按问法判，不按 route 判** —— 理由见 `TIME_SENSITIVE_RULES` 的注释：
    `data` 分支里既有「本月退货几单」（可缓存）也有「我的订单到哪了」（不可缓存）。
    """
    return classify_cache_intent(text)[0] == "time_sensitive"


# ── 问题的组装与版本指纹 ────────────────────────────────────────────────────


def assemble_question(
    messages: Sequence[Any], *, context_turns: int = DEFAULT_CONTEXT_TURNS
) -> tuple[str, bool]:
    """把会话里的用户消息拼成「**要被缓存的那个问题**」，返回 `(问题, 是否多轮)`。

    这是本模块最容易想当然的一处，所以把理由写全。

## 为什么不能只用最后一句

「那退货运费谁出？」单独看是一个**不完整**的问题：它的意思是「（上一个话题里的）
退货运费谁出」。只用最后一句当缓存键，会把两个不同话题下的同一句追问
算成同一个问题 —— 而它们的答案完全不同。这是缓存最危险的一类错误：
**相似度很高（字面就是同一句话），但语义依赖上下文**。

## 为什么拼了上下文之后仍然安全

拼进去之后，缓存键 = 「这一串对话的指纹」：
* 上下文不同 → 拼接结果不同 → 连完全匹配都不会命中（连 hash 都不同）；
* 上下文相同 → 语义就是同一个问题 → **可以**命中（用户在同一个会话里重复问同一句）。

于是「多轮」这件事从「危险」变成了「本来就不会串味」。
唯一的代价是命中率：多轮问题的键更长、更具体，跨会话复用几乎不可能。

## 为什么只取最近 `context_turns` 轮

更早的上下文对「这句话在问什么」几乎没有影响，而**每多一轮就多一份
「两段本该相同的对话因为更早的历史不同而算成两个问题」的浪费**。
取 3 轮是「够用」与「够便宜」之间的折中（第 14 步的 golden set 可以校准它）。
    """
    users = [
        str((message.get("content") if isinstance(message, dict) else getattr(message, "content", "")) or "")
        for message in messages
        if str(
            (message.get("role") if isinstance(message, dict) else getattr(message, "role", "")) or ""
        )
        == "user"
    ]
    users = [text.strip() for text in users if text.strip()]
    if not users:
        return "", False
    window = users[-max(1, int(context_turns)) :]
    joined = "\n".join(window)
    return joined, len(users) > 1


def fingerprint_for(
    *,
    corpus_revision: int,
    embedding_model: str = "",
    prompt_version: str = PROMPT_VERSION,
    params: Mapping[str, Any] | None = None,
) -> str:
    """版本指纹：**知识库版本 + prompt 版本 + embedding 模型（+ 检索参数）**。

    三者任一变化 → 全局失效（`13.cache.md` §5 第 3 条的「版本失效」）。

    为什么把它算成一个**字符串**、而不是在查询时逐个比较：
    ① 它是 Redis key 的一段（`backend.CacheKeySpace`），必须是字符串；
    ② 逐个比较要三个字段都存在每一行里，而「少存了一个」不会报错 ——
       它只会让那道闸静默失效（缓存开始返回旧知识库的答案）。

    为什么 embedding 模型要进来：换模型之后旧向量与新查询**不在同一个向量空间**
    （最坏的形态是维度恰好相同，见 `llm/client._assert_embedding_dim` 的说明）——
    那时相似度看起来完全正常，只是答案开始乱。模型名进指纹 = 换了模型就整批失效。

    `params` 是**检索参数**（`top_k` / `min_score` / 检索后端标识）：
    「用 top_k=5 查出来的答案」与「用 top_k=20 查出来的」不是同一个答案，
    所以它们必须参与指纹。排序后拼接让**参数的书写顺序无关** ——
    顺序敏感的指纹会让「同一套参数换个写法」变成一次全量失效
    （命中率掉一个坑，而原因看起来只是「有人重排了 kwargs」）。

    ⚠️ **`params` 里只能放「会改变答案」的量**。本仓实测踩到过这个坑：
    第一版把 `llm_calls_saved=True`（一个纯粹的记账标志）也塞了进去，
    于是**读路径**（`rag_node` 传 `top_k`/`min_score`/`embedder_key`）与
    **写路径**（`synthesis` 多传了一个标志）算出了两个不同的指纹 ——
    症状是「第一次问答写进缓存、第二次问同一个问题**仍然未命中**」，
    而两边的代码各自看起来都对。判据很简单：**这个参数变了，答案会变吗？**
    不会变的量（谁在写、省了几次调用、耗时多久）一律不许进指纹。
    """
    parts = [f"kb={int(corpus_revision)}", f"prompt={prompt_version}", f"embed={embedding_model}"]
    for key in sorted((params or {})):
        value = (params or {})[key]
        if value is None or value == "":
            continue  # 缺省值不进指纹：不传与显式传默认值应当是同一个指纹
        parts.append(f"{key}={value}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


# ── 命中判定（纯函数：吃候选，吐结论）───────────────────────────────────────


@dataclass(frozen=True)
class LookupRequest:
    """一次查询的输入（**不含存储与模型** —— 所以判定逻辑可以被纯逻辑用例逐条钉住）。"""

    question: str
    raw_question: str
    scope: str
    fingerprint: str
    #: 当前知识库版本（来自 `cache_corpus_versions`；0 = 未知）
    revision: int = 0
    decision: CacheDecision = field(
        default_factory=lambda: CacheDecision(action="cache", tier="normal", ttl_seconds=0)
    )
    #: 完全匹配的候选（`store.get()` 的产物）
    exact: CacheEntry | None = None
    #: 相似度候选（`store.nearest()` 的产物；按相似度降序）
    candidates: tuple[CacheMatch, ...] = ()
    degraded: bool = False


def judge(request: LookupRequest, *, settings: CacheSettings | None = None, now: float | None = None) -> LookupLog:
    """**命中判定**（本模块唯一一处「允不允许把缓存答案给用户」）。

    判定顺序与理由见模块 docstring 的五道闸。三处刻意的取舍：

    1. **完全匹配不走阈值**。同一句话（hash 相同）就是同一个问题 ——
       它的相似度必然接近 1，让阈值再判一次只会引入「自己跟自己的相似度不到阈值」
       这种只有在浮点/维度变化时才出现、且完全无法解释的怪事。
    2. **相似度用 `>=`**：阈值是「命中线」，边界取等号。
       写 `>` 的症状是「调阈值时总差一个数」，很难查（用例里会明确钉住等号）。
    3. **灰区候选留在账目里**（`LookupLog.nearest`）：相似度落在
       `[floor, threshold)` 之间时判未命中，但把候选与分数带出去 ——
       这正是「阈值调得对不对」的判断依据（第 14 步校准阈值时要看的数）。
    """
    resolved = settings or CacheSettings()
    started = time.perf_counter()
    moment = float(now) if now is not None else time.time()

    def log(outcome: str, detail: str = "", **kwargs: Any) -> LookupLog:
        return LookupLog(
            outcome=outcome,
            detail=detail,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            policy=request.decision.tier,
            degraded=request.degraded,
            **kwargs,
        )

    # ① 策略闸
    if request.decision.bypassed:
        return log(OUTCOME_BYPASSED, request.decision.reason or request.decision.rule)

    # ② 完全匹配
    if request.exact is not None:
        entry = request.exact
        if entry.is_expired(now=moment):
            return log(
                OUTCOME_STALE,
                f"TTL 已过（{entry.age_seconds(now=moment):.0f}s > {entry.ttl_seconds}s）",
                entry=entry,
                similarity=1.0,
            )
        if not entry.is_fresh(revision=int(request.revision) or None):
            return log(
                OUTCOME_STALE,
                f"知识库版本变了（写入时 {entry.revision} → 现在 {request.revision}）",
                entry=entry,
                similarity=1.0,
            )
        return log(OUTCOME_HIT, "完全同问", entry=entry, similarity=1.0)

    # ③ 相似度闸
    ordered = tuple(request.candidates)
    if not ordered:
        return log(OUTCOME_MISS, "没有任何候选")
    best = ordered[0]
    nearest = best if best.similarity >= float(resolved.semantic_floor) else None
    if best.similarity < float(resolved.semantic_floor):
        return log(
            OUTCOME_MISS,
            f"最近邻相似度 {best.similarity:.4f} 低于兜底线 {resolved.semantic_floor}",
            similarity=best.similarity,
            nearest=nearest,
        )
    if best.similarity < float(resolved.semantic_threshold):
        # **灰区**：字面相近但不敢答。它是「阈值偏高」与「阈值偏低」的分界可观测处。
        return log(
            OUTCOME_MISS,
            f"相似度 {best.similarity:.4f} 在灰区（floor {resolved.semantic_floor} ~ threshold "
            f"{resolved.semantic_threshold}），按保守口径不命中",
            similarity=best.similarity,
            nearest=nearest,
        )

    # ④⑤ 时效与版本闸（对相似命中的那条）
    entry = best.entry
    if entry.is_expired(now=moment):
        return log(
            OUTCOME_STALE,
            f"相似命中但 TTL 已过（{entry.age_seconds(now=moment):.0f}s > {entry.ttl_seconds}s）",
            similarity=best.similarity,
            entry=entry,
            nearest=nearest,
        )
    if not entry.is_fresh(revision=int(request.revision) or None):
        return log(
            OUTCOME_STALE,
            f"相似命中但知识库版本变了（写入时 {entry.revision} → 现在 {request.revision}）",
            similarity=best.similarity,
            entry=entry,
            nearest=nearest,
        )
    return log(OUTCOME_HIT, f"语义命中（相似度 {best.similarity:.4f}）", entry=entry, similarity=best.similarity, nearest=nearest)


def build_entry(
    request: LookupRequest,
    *,
    answer: str,
    embedding: Sequence[float],
    citations: Sequence[str] = (),
    meta: dict[str, Any] | None = None,
    settings: CacheSettings | None = None,
    created_at: float | None = None,
) -> CacheEntry:
    """把「一次生成的结果」包成要写进缓存的条目（TTL 与版本都来自 `request`）。"""
    resolved = settings or CacheSettings()
    citations = tuple(str(item) for item in citations)[: max(0, int(resolved.max_citations))]
    return new_entry(
        context=ScopeContext(scope=request.scope, fingerprint=request.fingerprint),
        question=request.question,
        raw_question=request.raw_question,
        embedding=embedding,
        answer=answer,
        citations=citations,
        meta=dict(meta or {}),
        revision=int(request.revision),
        policy=request.decision.tier,
        ttl_seconds=int(request.decision.ttl_seconds),
        created_at=float(created_at) if created_at is not None else time.time(),
    )


def with_answer(entry: CacheEntry, *, revision: int) -> CacheEntry:
    """把条目的版本改成当前版本（**只在「内容确实没变」时用**）。

    用途很小但要留出口：知识库版本变了，而某个问题的答案其实**与原文档无关**
    （例如「你们几点上班」），把这条从「失效」救回来是合理的。
    默认**不做**这件事 —— 版本失效的第一版就是宁可多清、不可漏清。
    """
    return replace(entry, revision=int(revision))


__all__ = [
    "DEFAULT_CONTEXT_TURNS",
    "OUTCOME_BYPASSED",
    "OUTCOME_DISABLED",
    "OUTCOME_ERROR",
    "OUTCOME_HIT",
    "OUTCOME_MISS",
    "OUTCOME_STALE",
    "POLICY_RULES",
    "PROMPT_VERSION",
    "TIME_SENSITIVE_RULES",
    "CacheDecision",
    "Embedder",
    "LookupRequest",
    "assemble_question",
    "build_entry",
    "classify_cache_intent",
    "decide",
    "fingerprint_for",
    "is_time_sensitive",
    "judge",
    "with_answer",
]
