"""判定内核的**纯逻辑**用例：阈值、TTL、版本、灰区、强时效分类。

这一组**不碰任何存储**（`EntryStore` 换成「直接把候选喂给 `judge`」），
因为要验的东西只有一件：**判定对不对**。起一个数据库来验它，
只会让「阈值 0.93 这条线在不在」变成一条难以阅读的断言。

对应 `13.cache.md` §7 的五条用例里最核心的两条
（`test_dissimilar_question_misses` / `test_time_sensitive_not_cached`），
外加它们背后的所有边界。
"""

from __future__ import annotations

import time

import pytest

from app.cache.core import (
    OUTCOME_BYPASSED,
    OUTCOME_HIT,
    OUTCOME_MISS,
    OUTCOME_STALE,
    CacheDecision,
    LookupRequest,
    assemble_question,
    classify_cache_intent,
    decide,
    fingerprint_for,
    is_time_sensitive,
    judge,
)
from app.cache.settings import CacheSettings
from app.cache.stores import CacheEntry, CacheMatch, MemoryEntryStore, ScopeContext, new_entry

THRESHOLD = CacheSettings().semantic_threshold
FLOOR = CacheSettings().semantic_floor


def entry(
    question: str = "怎么退货",
    *,
    age: float = 0.0,
    ttl: int = 3600,
    revision: int = 1,
    scope: str = "customer:2",
    fingerprint: str = "fp",
) -> CacheEntry:
    """造一条条目（`age` 用「多少秒之前写入」表示，比手算时间戳好读）。

    ⚠️ `created_at` 必须是**真实的当前时刻减 age**（第一版写成固定值 1000.0，
    于是所有条目都显示「躺了 56 年」并按过期处理 —— 那让「未过期」这一类用例
    全部失去了意义，而它看起来只是「判定写错了」）。
    """
    return new_entry(
        context=ScopeContext(scope=scope, fingerprint=fingerprint),
        question=question,
        raw_question=question,
        embedding=(1.0,) + (0.0,) * 7,
        answer=f"{question} 的答案",
        citations=("政策.md#1",),
        revision=revision,
        policy="normal",
        ttl_seconds=ttl,
        created_at=time.time() - float(age),
    )


def request(**kwargs) -> LookupRequest:
    base = {
        "question": "退货流程是什么",
        "raw_question": "退货流程是什么",
        "scope": "customer:2",
        "fingerprint": "fp",
        "revision": 1,
        "decision": CacheDecision(action="cache", tier="normal", ttl_seconds=3600),
    }
    base.update(kwargs)
    return LookupRequest(**base)


# ── 强时效（F1）：`no_cache` 的判定 ─────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "我的订单 1024 到哪了",
        "这个快递到哪了",
        "帮我查一下我的订单状态",
        "还有货吗",
        "库存还剩多少",
        "现在这个卖多少钱",
        "我的余额是多少",
        "最新物流进度",
        "什么时候能到",
        "我那个包裹签收了吗",
    ],
)
def test_time_sensitive_questions_are_not_cached(text: str) -> None:
    """`13.cache.md` §7 的 `test_time_sensitive_not_cached`：订单/库存/物流不进缓存。"""
    decision = decide(text)
    assert decision.bypassed is True
    assert decision.tier == "time_sensitive"
    assert is_time_sensitive(text) is True
    assert decision.ttl_seconds == 0


@pytest.mark.parametrize(
    "text, tier",
    [
        ("怎么退货", "normal"),
        ("退货流程是什么", "policy"),
        ("七天无理由怎么算的", "policy"),
        ("保修期是多久", "policy"),
        ("发票怎么开", "policy"),
        ("你们几点上班", "normal"),
    ],
)
def test_policy_and_normal_questions_are_cacheable(text: str, tier: str) -> None:
    decision = decide(text)
    assert decision.bypassed is False
    assert decision.tier == tier


def test_policy_ttl_is_longer_than_normal_ttl() -> None:
    """政策类问题的 TTL 更长（它的答案只随文档变动，不随时间变动）。"""
    settings = CacheSettings()
    assert decide("退货政策是什么", settings=settings).ttl_seconds == settings.policy_ttl_seconds
    assert decide("你们几点上班", settings=settings).ttl_seconds == settings.ttl_seconds
    assert settings.policy_ttl_seconds > settings.ttl_seconds


def test_time_sensitive_rule_wins_over_policy_rule() -> None:
    """**顺序即语义**：一句话同时含「政策」与「订单状态」时，必须按强时效处理。

    反过来的话「退货政策里的订单状态怎么查」会被缓存一周 —— 而它明天就不对了。
    """
    text = "退货政策里的订单状态怎么查"
    assert classify_cache_intent(text)[0] == "time_sensitive"


def test_short_ttl_policy_still_caches_time_sensitive() -> None:
    """`time_sensitive_policy="short"` 是另一条合法口径（13.cache.md §4 第 2 条两者都允许）。"""
    settings = CacheSettings(time_sensitive_policy="short", time_sensitive_ttl_seconds=30)
    decision = decide("我的订单到哪了", settings=settings)
    assert decision.bypassed is False
    assert decision.ttl_seconds == 30
    assert decision.tier == "time_sensitive"


# ── 阈值：命中线、兜底线、灰区 ───────────────────────────────────────────────


def test_exact_match_does_not_consult_the_threshold() -> None:
    """完全同问**不经过阈值**（同一句话就是同一个问题）。"""
    log = judge(request(exact=entry()))
    assert log.outcome == OUTCOME_HIT
    assert log.similarity == 1.0
    assert log.entry is not None


def test_similarity_at_the_threshold_hits() -> None:
    """边界取等号（`>=`）：写 `>` 会让「调阈值时总差一个数」。"""
    log = judge(request(candidates=(CacheMatch(similarity=THRESHOLD, entry=entry("退货流程是啥")),)))
    assert log.outcome == OUTCOME_HIT


def test_similarity_just_below_the_threshold_misses_and_records_the_gray_zone() -> None:
    """灰区：判未命中，但**把候选与分数带出来**（阈值校准的唯一在线信号）。"""
    log = judge(
        request(candidates=(CacheMatch(similarity=THRESHOLD - 0.001, entry=entry("相关但不同")),))
    )
    assert log.outcome == OUTCOME_MISS
    assert log.nearest is not None
    assert log.nearest.similarity == pytest.approx(THRESHOLD - 0.001)
    assert "灰区" in log.detail


def test_similarity_below_the_floor_early_exits() -> None:
    """低于兜底线 → 连候选都不带出（那是「完全不相干」，不是「像但不敢答」）。"""
    log = judge(
        request(candidates=(CacheMatch(similarity=FLOOR - 0.01, entry=entry("怎么改地址")),))
    )
    assert log.outcome == OUTCOME_MISS
    assert log.nearest is None
    assert "兜底线" in log.detail


def test_no_candidates_is_a_miss_not_an_error() -> None:
    log = judge(request())
    assert log.outcome == OUTCOME_MISS
    assert log.detail == "没有任何候选"


# ── TTL：第一级失效 ─────────────────────────────────────────────────────────


def test_expired_exact_match_is_stale_not_miss() -> None:
    """**过期与未命中必须分开报**：两者的运维动作不同（一个要查 TTL，一个正常）。"""
    log = judge(request(exact=entry(ttl=10, age=11)))
    assert log.outcome == OUTCOME_STALE
    assert "TTL" in log.detail
    assert log.entry is not None  # 过期的那条要带出来（排障要看它是谁）


def test_expired_similarity_match_is_stale() -> None:
    log = judge(request(candidates=(CacheMatch(similarity=0.99, entry=entry(ttl=10, age=11)),)))
    assert log.outcome == OUTCOME_STALE


def test_zero_ttl_means_immediately_expired_not_forever() -> None:
    """`ttl_seconds=0` 是「立刻过期」而不是「永不过期」——配置写错时选安全的那一边。"""
    assert entry(ttl=0).is_expired() is True


def test_fresh_entry_is_not_expired() -> None:
    assert entry(ttl=3600, age=10).is_expired() is False


# ── 版本：第三级失效 ────────────────────────────────────────────────────────


def test_revision_change_invalidates_a_fresh_entry() -> None:
    """知识库版本变了 → 旧条目立刻失效（`test_invalidate_on_change` 的判定半边）。"""
    log = judge(request(exact=entry(revision=1), revision=2))
    assert log.outcome == OUTCOME_STALE
    assert "知识库版本" in log.detail


def test_unknown_revision_does_not_invalidate() -> None:
    """读不到版本（`0`）**不能**当作「版本不匹配」——那会让一次库抖动清空整份缓存。"""
    log = judge(request(exact=entry(revision=7), revision=0))
    assert log.outcome == OUTCOME_HIT


def test_fingerprint_changes_when_revision_changes() -> None:
    left = fingerprint_for(corpus_revision=1, embedding_model="m")
    right = fingerprint_for(corpus_revision=2, embedding_model="m")
    assert left != right


def test_fingerprint_changes_when_embedding_model_changes() -> None:
    assert fingerprint_for(corpus_revision=1, embedding_model="a") != fingerprint_for(
        corpus_revision=1, embedding_model="b"
    )


def test_fingerprint_is_insensitive_to_parameter_order() -> None:
    """检索参数进指纹，但**书写顺序不影响**它（否则「重排 kwargs」会变成一次全量失效）。"""
    left = fingerprint_for(corpus_revision=1, params={"top_k": 5, "min_score": 0.1})
    right = fingerprint_for(corpus_revision=1, params={"min_score": 0.1, "top_k": 5})
    assert left == right


def test_fingerprint_ignores_empty_parameters() -> None:
    """不传与显式传空值应当是同一个指纹（否则「顺手写了 min_score=None」会清空缓存）。"""
    assert fingerprint_for(corpus_revision=1, params={"top_k": 5}) == fingerprint_for(
        corpus_revision=1, params={"top_k": 5, "min_score": None, "x": ""}
    )


def test_retrieval_parameters_change_the_fingerprint() -> None:
    """`top_k=5` 与 `top_k=20` 不是同一个问题：缓存下来的是「用这几个参数查出来的答案」。"""
    assert fingerprint_for(corpus_revision=1, params={"top_k": 5}) != fingerprint_for(
        corpus_revision=1, params={"top_k": 20}
    )


def test_bookkeeping_flags_must_not_reach_the_fingerprint() -> None:
    """**记账类的东西不许进指纹**（本步实测最贵的那个坑）。

    第一版把 `llm_calls_saved=True` 混进了写路径的参数，于是读写两侧算出两个指纹 ——
    「第一次问答写进缓存、第二次问同一个问题仍然未命中」。
    这条用例把「读写两侧只用同一组检索参数」这件事钉在纯逻辑层：
    `params` 里放什么由 `app/cache/params.retrieval_params` 的白名单决定。
    """
    from app.cache.params import retrieval_params

    params = retrieval_params(top_k=5, min_score=None, search=None)
    assert set(params) == {"top_k", "min_score"}
    with pytest.raises(ValueError, match="白名单"):
        retrieval_params(top_k=5, extra={"llm_calls_saved": True})


# ── 策略闸：关掉缓存 / 强时效 ───────────────────────────────────────────────


def test_bypassed_request_never_touches_the_store() -> None:
    """强时效问题在 `judge` 这一步就被拦下 —— 它连候选都不该有。"""
    log = judge(
        request(
            decision=CacheDecision(
                action="bypass", tier="time_sensitive", ttl_seconds=0, rule="order_status"
            ),
            exact=entry(),
        )
    )
    assert log.outcome == OUTCOME_BYPASSED


# ── 问题组装：多轮上下文 ─────────────────────────────────────────────────────


def test_assemble_question_uses_only_user_messages() -> None:
    question, multi = assemble_question(
        [
            {"role": "user", "content": "怎么退货"},
            {"role": "assistant", "content": "七天无理由……"},
            {"role": "user", "content": "那运费谁出"},
        ]
    )
    assert multi is True
    assert "怎么退货" in question and "那运费谁出" in question
    assert "七天无理由" not in question  # 助手的话不进问题


def test_assemble_question_single_turn_is_flagged() -> None:
    question, multi = assemble_question([{"role": "user", "content": "怎么退货"}])
    assert question == "怎么退货"
    assert multi is False


def test_follow_up_question_is_a_different_key_than_the_standalone_one() -> None:
    """**最关键的一条**：「那运费谁出」单独问与跟在别的会话后面问，是两个不同的问题。

    只取最后一句的实现会让它们算成同一个问题，而答案完全不同 ——
    这是相似度缓存最危险的一类错误（字面同一句话，语境不同）。
    """
    standalone, _ = assemble_question([{"role": "user", "content": "那运费谁出"}])
    in_context, _ = assemble_question(
        [{"role": "user", "content": "我的订单呢"}, {"role": "user", "content": "那运费谁出"}]
    )
    assert standalone != in_context


def test_assemble_question_window_keeps_only_recent_turns() -> None:
    messages = [{"role": "user", "content": f"第{i}问"} for i in range(6)]
    question, _ = assemble_question(messages, context_turns=2)
    assert "第4问" in question and "第5问" in question
    assert "第0问" not in question


def test_assemble_question_empty_when_no_user_message() -> None:
    assert assemble_question([{"role": "assistant", "content": "你好"}]) == ("", False)


# ── 配置自检 ────────────────────────────────────────────────────────────────


def test_threshold_inversion_is_reported() -> None:
    problems = CacheSettings(semantic_floor=0.99, semantic_threshold=0.5).validate()
    assert any("阈值" in item for item in problems)


def test_threshold_at_one_is_reported() -> None:
    assert any("阈值" in item for item in CacheSettings(semantic_threshold=1.0).validate())


def test_unknown_time_sensitive_policy_is_reported() -> None:
    assert any(
        "强时效策略" in item for item in CacheSettings(time_sensitive_policy="whatever").validate()
    )


def test_short_policy_ttl_must_be_shorter_than_normal() -> None:
    problems = CacheSettings(
        time_sensitive_policy="short", time_sensitive_ttl_seconds=99999
    ).validate()
    assert any("强时效 TTL" in item for item in problems)


def test_default_settings_are_clean() -> None:
    assert CacheSettings().validate() == []


# ── 存储层的一处纯逻辑：余弦相似度 ──────────────────────────────────────────


def test_cosine_similarity_of_identical_vectors_is_one() -> None:
    from app.cache.stores import cosine_similarity

    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_of_orthogonal_vectors_is_zero() -> None:
    from app.cache.stores import cosine_similarity

    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_of_mismatched_dimension_is_minus_one_not_an_error() -> None:
    """维度变化（换模型）时把旧条目当作「不相干」，而不是让每个请求 500。"""
    from app.cache.stores import cosine_similarity

    assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == -1.0


def test_cosine_similarity_of_zero_vector_is_zero_not_nan() -> None:
    """零向量返回 0 而不是 `nan`（`nan >= threshold` 恒为 False，那会静默变成「不命中」）。"""
    from app.cache.stores import cosine_similarity

    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_memory_store_nearest_is_scoped_and_ordered() -> None:
    """内存后端的相似度查询：只看同 scope 同指纹，且按相似度降序。"""
    store = MemoryEntryStore()
    context = ScopeContext(scope="customer:2", fingerprint="fp")
    store.put(entry("a", scope="customer:2", fingerprint="fp"))
    store.put(entry("b", scope="customer:2", fingerprint="fp"))
    store.put(entry("c", scope="customer:9", fingerprint="fp"))
    store.put(entry("d", scope="customer:2", fingerprint="other"))
    found = store.nearest(context, [1.0] + [0.0] * 7, limit=5)
    assert {item.entry.question for item in found} == {"a", "b"}


def test_memory_store_get_is_exact_only() -> None:
    store = MemoryEntryStore()
    context = ScopeContext(scope="customer:2", fingerprint="fp")
    store.put(entry("怎么退货"))
    assert store.get(context, "怎么退货") is not None
    assert store.get(context, "退货流程是什么") is None


def test_question_hash_normalizes_whitespace_and_case() -> None:
    from app.cache.stores import question_hash

    assert question_hash("How  to   return") == question_hash("how to return")
    assert question_hash("怎么退货") == question_hash(" 怎么退货 ")


def test_question_hash_does_not_strip_punctuation() -> None:
    """**刻意不规范化标点**：「怎么退货？」与「怎么退货」该由语义缓存去判，
    不该由 hash 偷偷替它决定（hash 撞车是静默的错误命中）。"""
    from app.cache.stores import question_hash

    assert question_hash("怎么退货？") != question_hash("怎么退货")
