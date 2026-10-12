"""语义缓存的**端到端**语义（内存后端 + 可控假 embedding + 内存版本表）。

这一组对应 `13.cache.md` §7 的五条用例，逐条落地：

| 文档里的用例 | 本文件里的名字 |
|---|---|
| `test_similar_question_hits_cache` | `test_similar_question_hits_cache` |
| `test_dissimilar_question_misses` | `test_dissimilar_question_misses` |
| `test_time_sensitive_not_cached` | `test_time_sensitive_question_is_not_cached` |
| `test_ttl_expires` | `test_ttl_expires` |
| `test_invalidate_on_change` | `test_invalidate_on_change` |

外加文档 §4 第 4 条（「缓存值存答案 + 溯源 + 时间戳」）与 §5 第 3 条
（「缓存放在 LLM 调用之前」）的落点。
"""

from __future__ import annotations

import time

import pytest

from app.cache.core import PROMPT_VERSION, fingerprint_for
from app.cache.settings import CacheSettings
from app.cache.stores import ScopeContext, question_hash
from tests.cache.conftest import SemanticSpace, make_cache, messages

ANSWER = "自签收之日起 7 天内、商品完好可申请无理由退货；无理由退货的运费由买家承担。[1]"
CITATIONS = ("知识库/退货政策.md#第1条",)


def build(space: SemanticSpace, **kwargs):
    """把「怎么退货」放到基准方向上，再造一个离线缓存。

    ⚠️ 用例想挂「相似问法」时必须在调用它**之后**（`build()` 里才放基准）——
    顺序反了 `SemanticSpace.place` 会直接 `KeyError` 报出来，
    而不是静默换一个方向（后者会让「相似度 0.97 该命中」这条断言以
    「0.0 低于兜底线」的形态失败，看上去像判定写错了）。
    """
    space.place("怎么退货")
    return make_cache(space=space, **kwargs)


# ── 文档 §7 的第一条：语义命中 ──────────────────────────────────────────────


def test_similar_question_hits_cache(space: SemanticSpace) -> None:
    """`13.cache.md` §7 原文：写「怎么退货」，问「退货流程是什么」要命中。

    ⚠️ 这里用的**不是**文档里的同步两行（`semantic_cache_set` / `semantic_cache_get`），
    而是 `prepare` / `respond` 两段式 —— 两条路径共用同一个 `SemanticCache`，
    而同步门面（`Cache.remember` / `Cache.lookup`）在
    `test_semantic_gateway.py` 里另有直接对应文档形态的用例。
    """
    cache = build(space)
    space.place("退货流程是什么", base="怎么退货", similarity=0.97)

    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    assert prepared.hit is False
    cache.semantic.respond(prepared, ANSWER, citations=CITATIONS)

    again = cache.lookup(messages("退货流程是什么"), user_id=2, role="customer")
    assert again.hit is True
    assert again.answer == ANSWER
    assert again.citations == CITATIONS
    assert again.log.similarity == pytest.approx(0.97, abs=1e-6)


def test_hit_carries_citations_and_age(space: SemanticSpace) -> None:
    """文档 §4 第 4 条：缓存值存「答案 + 溯源 + 时间戳」，命中时连出处一起返回。"""
    cache = build(space)
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER, citations=CITATIONS)

    hit = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    assert hit.hit is True
    assert hit.citations == CITATIONS
    assert hit.age_seconds() >= 0.0
    assert hit.log.entry is not None
    assert hit.log.entry.created_at > 0


def test_dissimilar_question_misses(space: SemanticSpace) -> None:
    """文档 §7 原文：「怎么改地址」与退货无关 → 不命中。"""
    cache = build(space)
    space.place("怎么改地址", base="怎么退货", similarity=0.62)
    cache = build(space)
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER, citations=CITATIONS)

    other = cache.lookup(messages("怎么改地址"), user_id=2, role="customer")
    assert other.hit is False
    assert other.answer == ""
    assert other.log.outcome == "miss"


def test_gray_zone_similarity_does_not_hit(space: SemanticSpace) -> None:
    """灰区（像但不敢答）：这是「阈值保守」这条设计的可观测落点。"""
    cache = build(space)
    space.place("退货流程是啥", base="怎么退货", similarity=0.90)  # 默认阈值 0.93
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER)

    near = cache.lookup(messages("退货流程是啥"), user_id=2, role="customer")
    assert near.hit is False
    assert near.log.similarity == pytest.approx(0.90, abs=1e-3)
    assert near.log.nearest is not None  # 灰区候选被带出来了（阈值校准的依据）


def test_exact_repeat_hits_without_embedding(space: SemanticSpace) -> None:
    """完全同问走 Redis 的快路径：**第二次连向量都不该算**。

    这是「缓存放在 LLM 之前」在成本上最直接的一步 ——
    用户刷新页面时，连一次 30ms 的 embedding 都不该发生。
    """
    cache = build(space)
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER)
    calls_after_write = len(space.calls)

    again = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    assert again.hit is True
    assert again.log.detail == "完全同问"
    assert len(space.calls) == calls_after_write, "完全同问不该触发 embedding"


# ── 隔离：身份、角色、会话 ──────────────────────────────────────────────────


def test_cache_is_per_user(space: SemanticSpace) -> None:
    """用户 A 的答案不能给用户 B —— 这关系到 A2（越权）。"""
    cache = build(space)
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER)

    other_user = cache.lookup(messages("怎么退货"), user_id=3, role="customer")
    assert other_user.hit is False


def test_cache_is_per_role(space: SemanticSpace) -> None:
    """同一个 user_id 换个角色**不能**命中：内部员工看到的范围与客户不同（第 4 步 RBAC）。"""
    cache = build(space)
    prepared = cache.lookup(messages("怎么退货"), user_id=7, role="customer")
    cache.semantic.respond(prepared, ANSWER)

    internal = cache.lookup(messages("怎么退货"), user_id=7, role="internal_employee")
    assert internal.hit is False


def test_multi_turn_context_does_not_cross_hit(space: SemanticSpace) -> None:
    """**本模块最难的一处**：同一句追问在不同上下文里是两个问题。

    「那退货运费谁出」单独问，与跟在「我的订单呢」后面问，语义完全不同 ——
    只取最后一句的实现会让它们互相命中（字面同一句话，答案不一样）。
    """
    space.place("那退货运费谁出")
    cache = build(space)
    standalone = cache.lookup(messages("那退货运费谁出"), user_id=2, role="customer")
    cache.semantic.respond(standalone, "运费由买家承担。")

    in_context = cache.lookup(
        messages("我的订单 1024 呢", "已发货", "那退货运费谁出"), user_id=2, role="customer"
    )
    assert in_context.hit is False


def test_same_conversation_repeat_hits(space: SemanticSpace) -> None:
    """反过来：**同一个会话里**重复问同一句要命中（键完全相同）。"""
    cache = build(space)
    conversation = messages("我的订单呢", "已发货", "那退货运费谁出")
    first = cache.lookup(conversation, user_id=2, role="customer")
    cache.semantic.respond(first, "运费由买家承担。")

    again = cache.lookup(conversation, user_id=2, role="customer")
    assert again.hit is True
    assert again.answer == "运费由买家承担。"


# ── F1：强时效不进缓存 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["我的订单 1024 到哪了", "还有货吗", "物流进度怎么样"])
def test_time_sensitive_question_is_not_cached(space: SemanticSpace, text: str) -> None:
    """文档 §7 的第三条：订单/库存/物流类问题不走语义缓存（`no_cache`）。

    两条断言都很重要：**读**不命中（哪怕有人手工写进去过），**写**也不发生。
    """
    cache = build(space)
    prepared = cache.lookup(messages(text), user_id=2, role="customer")
    assert prepared.hit is False
    assert prepared.bypassed is True
    assert prepared.log.outcome == "bypassed"

    written = cache.semantic.respond(prepared, "已发货，预计明天到。")
    assert written.written is False
    # 写都没写，读当然也不命中
    assert cache.lookup(messages(text), user_id=2, role="customer").hit is False
    assert cache.store.count() == 0


def test_time_sensitive_bypass_does_not_touch_the_store(space: SemanticSpace) -> None:
    """跳过缓存时**连存储都不碰**（这是「为什么不放在第一道」的实现证据）。"""

    class ExplodingStore:
        def name(self) -> str:
            return "exploding"

        def __getattr__(self, item: str):  # noqa: ANN401 - 任何一次调用都算失败
            raise AssertionError(f"强时效问题不该调用存储的 {item}")

    cache = make_cache(space=space, store=ExplodingStore())
    prepared = cache.lookup(messages("我的订单到哪了"), user_id=2, role="customer")
    assert prepared.bypassed is True


def test_short_ttl_policy_caches_time_sensitive_with_a_short_ttl(space: SemanticSpace) -> None:
    """`time_sensitive_policy="short"` 时**可以**缓存，但 TTL 必须短。"""
    cache = build(space, settings=CacheSettings(time_sensitive_policy="short", time_sensitive_ttl_seconds=30))
    prepared = cache.lookup(messages("我的订单到哪了"), user_id=2, role="customer")
    assert prepared.bypassed is False
    written = cache.semantic.respond(prepared, "已发货。")
    assert written.written is True
    assert written.ttl_seconds == 30


# ── TTL（第一级失效）────────────────────────────────────────────────────────


def test_ttl_expires(space: SemanticSpace) -> None:
    """文档 §7 的第四条：超过 TTL 之后不再命中（重新生成）。"""
    cache = build(space, settings=CacheSettings(ttl_seconds=10))
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER)

    # 显式的时间旅行：`prepare(now=...)` 而不是 `sleep(10)` ——
    # 后者会让用例慢 10 秒，且与「TTL 判定」这件事毫无关系。
    later = cache.lookup(messages("怎么退货"), user_id=2, role="customer", now=time.time() + 11)
    assert later.hit is False
    assert later.log.outcome == "stale"
    assert "TTL" in later.log.detail


def test_ttl_boundary_is_kept_for_a_while(space: SemanticSpace) -> None:
    """TTL 之内仍然命中（否则「过期」与「从不缓存」无法区分）。"""
    cache = build(space, settings=CacheSettings(ttl_seconds=3600))
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER)
    hit = cache.lookup(messages("怎么退货"), user_id=2, role="customer", now=time.time() + 3599)
    assert hit.hit is True


def test_policy_question_gets_the_long_ttl(space: SemanticSpace) -> None:
    space.place("退货政策是什么")
    cache = build(space)
    prepared = cache.lookup(messages("退货政策是什么"), user_id=2, role="customer")
    written = cache.semantic.respond(prepared, "七天无理由。")
    assert written.ttl_seconds == CacheSettings().policy_ttl_seconds


# ── 版本（第三级失效）──────────────────────────────────────────────────────


def test_invalidate_on_change(space: SemanticSpace, revisions) -> None:
    """文档 §7 的第五条：知识库更新后旧缓存被主动失效。

    失效的机制是**版本号**（不是删行）：`fingerprint` 里含着版本，
    版本一变，旧条目在 `judge` 里一眼被判失效。
    所以这里断言两件事：① 不再命中；② 账目说的是 `stale` 而不是 `miss`
    （后者会让人以为「缓存本来就没写过」）。
    """
    cache = build(space, revisions=revisions)
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER)
    assert cache.lookup(messages("怎么退货"), user_id=2, role="customer").hit is True

    revisions.bump(by="test:ingest", force=True)  # 知识库变了

    after = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    assert after.hit is False
    assert after.log.outcome == "miss"  # 指纹变了 → 对新指纹来说**根本没有候选**


def test_invalidation_is_explainable_by_the_fingerprint(space: SemanticSpace, revisions) -> None:
    """指纹**随版本变化**（这是「失效不需删行」的全部依据）。"""
    cache = build(space, revisions=revisions)
    before = cache.lookup(messages("怎么退货"), user_id=2, role="customer").fingerprint
    revisions.bump(force=True)
    after = cache.lookup(messages("怎么退货"), user_id=2, role="customer").fingerprint
    assert before != after
    assert fingerprint_for(corpus_revision=1, embedding_model="fake/model") != fingerprint_for(
        corpus_revision=2, embedding_model="fake/model"
    )


def test_revision_survives_a_repeated_ingest_without_changes(space: SemanticSpace, revisions) -> None:
    """**重复入库不 bump**：否则每跑一次同步都会白清一次缓存（命中率掉一个坑）。"""
    assert revisions.bump(by="ingest:x", version=1) == 1  # 与当前版本相同 → 不动
    assert revisions.current() == 1
    assert revisions.bump(by="ingest:x", version=2) == 2  # 真的变了 → +1
    assert revisions.current() == 2


# ── 写路径的六条闸 ──────────────────────────────────────────────────────────


def test_empty_answer_is_never_cached(space: SemanticSpace) -> None:
    """**空回答绝不进缓存**：它会让之后每一次命中都返回空回答。"""
    cache = build(space)
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    written = cache.semantic.respond(prepared, "   ")
    assert written.written is False
    assert "回答为空" in written.reason
    assert cache.lookup(messages("怎么退货"), user_id=2, role="customer").hit is False


def test_hit_is_not_rewritten(space: SemanticSpace) -> None:
    """**命中的那条不重写**：重写会刷新它的年龄，掩盖「它多久没被真正校验过」。"""
    cache = build(space)
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER)
    hit = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    written = cache.semantic.respond(hit, "另一个答案")
    assert written.written is False
    assert "已命中" in written.reason
    # 值没被改掉
    assert cache.lookup(messages("怎么退货"), user_id=2, role="customer").answer == ANSWER


def test_disabled_cache_does_not_read_or_write(space: SemanticSpace) -> None:
    cache = build(space, settings=CacheSettings(enabled=False))
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    assert prepared.log.outcome == "disabled"
    assert cache.semantic.respond(prepared, ANSWER).written is False


def test_embedding_failure_is_a_miss_not_an_exception(space: SemanticSpace) -> None:
    """embedding 挂了 → 缓存不可用（**未命中**），但业务必须继续。"""

    class BrokenSpace:
        def __call__(self, text: str, *, cache=None):  # noqa: ANN001, ANN204
            raise RuntimeError("模型加载失败")

        def resolve_model(self, cache=None):  # noqa: ANN001, ANN204
            return "broken"

    cache = make_cache(space=BrokenSpace())
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    assert prepared.hit is False
    assert cache.semantic.respond(prepared, ANSWER).written is False


def test_store_failure_is_a_miss_not_an_exception(space: SemanticSpace) -> None:
    """存储挂了 → fail-open（未命中），写入也 fail-open。"""

    class BrokenStore:
        def name(self) -> str:
            return "broken"

        def get(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("连接被拒绝")

        def nearest(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("连接被拒绝")

        def put(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("连接被拒绝")

        def touch(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("连接被拒绝")

        def count(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("连接被拒绝")

        def health(self):  # noqa: ANN201
            raise RuntimeError("连接被拒绝")

    cache = make_cache(space=space, store=BrokenStore())
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    assert prepared.hit is False
    assert cache.semantic.respond(prepared, ANSWER).written is False


# ── 命中之后的副作用 ────────────────────────────────────────────────────────


def test_hit_increments_counter_and_records_touch(space: SemanticSpace) -> None:
    """命中会累加 `hit_count`（它是「这条缓存救了多少次」的唯一账目）。"""
    cache = build(space)
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER)
    cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    cache.lookup(messages("怎么退货"), user_id=2, role="customer")

    entry = cache.store.get(
        ScopeContext(scope="customer:2", fingerprint=prepared.fingerprint), "怎么退货"
    )
    assert entry is not None
    assert entry.hit_count == 2


def test_stats_track_the_outcomes(space: SemanticSpace) -> None:
    """`stats` 是自检与演示脚本读的账目（与指标是两个出口，互不替代）。"""
    cache = build(space)
    prepared = cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    assert cache.semantic.stats["miss"] == 1
    cache.semantic.respond(prepared, ANSWER)
    assert cache.semantic.stats["written"] == 1
    cache.lookup(messages("怎么退货"), user_id=2, role="customer")
    assert cache.semantic.stats["hit"] == 1


def test_prompt_version_participates_in_the_fingerprint(space: SemanticSpace) -> None:
    """prompt 版本变了也要全局失效（`13.cache.md` §5 第 3 条的另一半）。"""
    assert PROMPT_VERSION in ("p13",)
    assert fingerprint_for(corpus_revision=1, prompt_version="p13") != fingerprint_for(
        corpus_revision=1, prompt_version="p14"
    )


def test_question_hash_is_stable_for_the_same_question(space: SemanticSpace) -> None:
    assert question_hash("怎么退货") == question_hash("怎么退货")
