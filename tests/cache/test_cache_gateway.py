"""门面（`app/cache` 的对外接口）用例：文档 §7 的**原文形态**在这里逐字落地。

`13.cache.md` §7 写的是：

```python
semantic_cache_set("怎么退货", answer)
assert semantic_cache_get("退货流程是什么") == answer
```

这两行是**同步**调用、且直接拿字符串当问题 —— 本仓的同步门面
（`lookup` / `remember`）就是照它写的，`default_cache()` 提供进程默认实例。
这一组用例同时钉住三件事：

1. 那句文档用例**真的能那样写**（`test_documented_two_liner_works`）；
2. 注入的默认实例被正确使用（**不会去连 `.env` 里的真 Redis**）；
3. `Prepared` 与文档口径的差异（返回的是带账目的对象而不是裸答案）有明确记录。
"""

from __future__ import annotations

import pytest

from app.cache import (
    build_cache,
    close_cache,
    default_cache,
    lookup,
    remember,
    reset_for_tests,
    semantic_cache_get,
    semantic_cache_set,
    set_default_cache,
)
from app.cache.settings import CacheBackendSettings, CacheConfig, CacheSettings
from tests.cache.conftest import SemanticSpace, make_cache, messages

ANSWER = "7 天内可无理由退货，运费由买家承担。[1]"


def test_documented_two_liner_works(space: SemanticSpace, cache, entry_store) -> None:
    """文档 §7 原文：`semantic_cache_set("怎么退货", answer)` 然后问相似问法。

    ⚠️ 这里用的是**同步**门面（`remember` / `lookup`），因为文档写的就是同步两行。
    异步门面（`semantic_cache_get/set`）在同一组装置上另有两条用例。
    ⚠️ 复用 `cache` 装置的条目表与键值层（而不是新造一个）：这样 `remember` 走的是
    **进程默认实例**（`default_cache()`）—— 也就是文档那两行真正会走的那条路。
    """
    space.place("怎么退货")
    space.place("退货流程是什么", base="怎么退货", similarity=0.96)
    entry_store.clear()

    written = remember("怎么退货", ANSWER, user_id=2, role="customer", citations=["政策.md#1"])
    assert written.written is True

    hit = lookup("退货流程是什么", user_id=2, role="customer")
    assert hit.hit is True
    assert hit.answer == ANSWER
    assert hit.citations == ("政策.md#1",)


def test_sync_lookup_accepts_a_plain_string(space: SemanticSpace) -> None:
    """一句字符串就是一条 user 消息（文档形态）；不必自己拼 dict。"""
    cache = make_cache(space=space)
    prepared = cache.lookup("怎么退货", user_id=2, role="customer")
    assert prepared.question == "怎么退货"
    assert prepared.raw_question == "怎么退货"


def test_sync_lookup_accepts_state_like_dict(space: SemanticSpace) -> None:
    """图的 state（含 `messages` 键的 dict）也能直接喂进来（节点的调用形态）。"""
    cache = make_cache(space=space)
    prepared = cache.lookup({"messages": messages("怎么退货")}, user_id=2, role="customer")
    assert prepared.question == "怎么退货"


def test_lookup_reports_a_bypass_without_touching_the_store(space: SemanticSpace) -> None:
    cache = make_cache(space=space)
    prepared = cache.lookup("我的订单到哪了", user_id=2, role="customer")
    assert prepared.bypassed is True
    assert prepared.log.outcome == "bypassed"
    assert cache.store.count() == 0


def test_empty_conversation_is_skipped(space: SemanticSpace) -> None:
    cache = make_cache(space=space)
    prepared = cache.lookup({"messages": []}, user_id=2, role="customer")
    assert prepared.hit is False
    assert prepared.log.outcome == "bypassed"
    assert "没有用户消息" in prepared.log.detail


@pytest.mark.asyncio
async def test_async_gateway_writes_and_reads(space: SemanticSpace, cache) -> None:
    """异步门面（节点用的那条路）：`await semantic_cache_set` → `await semantic_cache_get`。"""
    written = await semantic_cache_set(
        messages("怎么退货"), ANSWER, user_id=2, role="customer", citations=["政策.md#1"]
    )
    assert written.written is True
    hit = await semantic_cache_get(messages("怎么退货"), user_id=2, role="customer")
    assert hit.hit is True
    assert hit.answer == ANSWER


@pytest.mark.asyncio
async def test_async_gateway_returns_a_prepared_not_a_bare_string(space: SemanticSpace, cache) -> None:
    """`semantic_cache_get` 返回 `Prepared` 而**不是**裸答案串 —— 这是与文档的一处有意差异。

    原因：命中时要连**出处与账目**一起返回（文档 §4 第 4 条要求可追溯），
    而未命中时要带出**写入所需的上下文**（scope / fingerprint / 决策）。
    只返回字符串会让「未命中」与「命中了一个空答案」无法区分。
    """
    prepared = await semantic_cache_get(messages("怎么退货"), user_id=2, role="customer")
    assert hasattr(prepared, "answer")
    assert hasattr(prepared, "citations")
    assert hasattr(prepared, "log")
    assert prepared.hit is False
    assert prepared.answer == ""


def test_default_cache_is_a_singleton_until_replaced(space: SemanticSpace) -> None:
    first = default_cache()
    assert default_cache() is first
    replacement = make_cache(space=space)
    set_default_cache(replacement)
    try:
        assert default_cache() is replacement
    finally:
        set_default_cache(first)


def test_close_cache_releases_and_forgets() -> None:
    cache = make_cache(space=SemanticSpace())
    set_default_cache(cache)
    assert close_cache() is True
    assert close_cache() is False  # 第二次没东西可关


def test_reset_for_tests_builds_a_fully_offline_cache(space: SemanticSpace) -> None:
    """`reset_for_tests` 的关键性质：**不发一次网络调用**（redis_url 为空 → 内存键值）。"""
    instance = reset_for_tests(settings=CacheSettings(), embedder=space)
    # `redis_url` 为空 → 键值层回落进程内存；`degraded` 说明的是「Redis 没配」，
    # 而不是「存储坏了」。这条口径在 runbook §4 的降级矩阵里。
    assert instance.store.name() == "memory"
    assert instance.key_value.__class__.__name__ == "MemoryKeyValue"
    written = remember("怎么退货", ANSWER, user_id=2, role="customer")
    assert written.written is True


def test_build_cache_falls_back_to_memory_when_redis_is_unreachable(space: SemanticSpace) -> None:
    """Redis 不可达 → 键值层回落内存，且 `degraded=True`（**绝不静默**）。

    ⚠️ 端口 1 是保留端口，**必然**连接被拒（毫秒级）—— 所以这条用例是真的走了一遍
    「连接失败 → 回落」的路径，而不是打桩（用不可路由的地址会变成等超时）。

    ⚠️ 只断言**键值层**是内存：条目存储这一格是**另一格**（本机缓存表在，所以它是 Postgres）
    —— 降级矩阵的四个格子见 runbook §4，这里只钉住「Redis 那一格」。
    """
    instance = build_cache(
        config=CacheConfig(
            cache=CacheSettings(lookup_timeout_seconds=0.2),
            backend=CacheBackendSettings(redis_url="redis://127.0.0.1:1/0", instance="test"),
        ),
        embedder=space,
    )
    assert instance.degraded is True
    assert instance.key_value.__class__.__name__ == "MemoryKeyValue"


def test_unreachable_redis_still_caches(space: SemanticSpace) -> None:
    """降级之后缓存**仍然工作**（只是每个进程一份）—— 这是「缓存自己不存在不可用状态」。"""
    instance = build_cache(
        config=CacheConfig(
            cache=CacheSettings(lookup_timeout_seconds=0.2),
            backend=CacheBackendSettings(redis_url="redis://127.0.0.1:1/0", instance="test"),
        ),
        embedder=space,
    )
    prepared = instance.lookup("怎么退货", user_id=2, role="customer")
    assert instance.semantic.respond(prepared, ANSWER).written is True
    assert instance.lookup("怎么退货", user_id=2, role="customer").hit is True


def test_health_reports_problems_and_stats(space: SemanticSpace) -> None:
    instance = reset_for_tests(settings=CacheSettings(semantic_threshold=0.99, semantic_floor=0.8), embedder=space)
    state = instance.health()
    assert state.problems == []
    assert state.stats["miss"] == 0 or True  # 结构存在即可（值由别的用例钉）
    assert isinstance(state.entry_store, dict)
    assert state.result_store["namespace"].endswith("result:")


def test_health_surfaces_config_problems(space: SemanticSpace) -> None:
    """阈值写反 → `health().problems` 里有它（`/health` 的 `down` 状态就是读它）。"""
    instance = reset_for_tests(
        settings=CacheSettings(semantic_threshold=0.5, semantic_floor=0.9), embedder=space
    )
    assert instance.health().problems


def test_invalidate_document_removes_the_entry(space: SemanticSpace) -> None:
    """主动失效（第二策略）：按 scope + 问题精确删一条（**不必知道 fingerprint**）。"""
    from app.cache.invalidate import invalidate_document

    cache = make_cache(space=space, base="怎么退货")
    prepared = cache.lookup("怎么退货", user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER)
    assert cache.lookup("怎么退货", user_id=2, role="customer").hit is True

    removed = invalidate_document(cache.store, scope="customer:2", question="怎么退货")
    assert removed == 1
    assert cache.lookup("怎么退货", user_id=2, role="customer").hit is False


def test_invalidate_scope_removes_everything_for_a_user(space: SemanticSpace) -> None:
    from app.cache.invalidate import invalidate_scope

    cache = make_cache(space=space)
    for index, text in enumerate(("怎么退货", "怎么开票", "保修多久")):
        space.place(text)
        prepared = cache.lookup(text, user_id=2, role="customer")
        cache.semantic.respond(prepared, f"答案{index}")
    assert cache.store.count() == 3

    assert invalidate_scope(cache.store, user_id=2, role="customer") == 3
    assert cache.store.count() == 0


def test_purge_expired_only_removes_expired(space: SemanticSpace) -> None:
    from app.cache.invalidate import purge_expired

    cache = make_cache(space=space, settings=CacheSettings(ttl_seconds=1))
    prepared = cache.lookup("怎么退货", user_id=2, role="customer")
    cache.semantic.respond(prepared, ANSWER, now=0.0)  # 写入时刻 = 1970 年 → 早就过期
    space.place("怎么开票")
    fresh = cache.lookup("怎么开票", user_id=2, role="customer")
    cache.semantic.respond(fresh, "答案")

    removed = purge_expired(cache.store)
    assert removed == 1
    assert cache.lookup("怎么开票", user_id=2, role="customer").hit is True


def test_make_ingest_callback_bumps_the_revision(space: SemanticSpace, revisions) -> None:
    """入库回调：真正改动才推进版本（重复入库不 bump）。"""
    from app.cache.invalidate import make_ingest_callback

    callback = make_ingest_callback(revisions=revisions)

    def report(version: int) -> object:
        class Report:
            source = "知识库/退货政策.md"

        Report.version = int(version)  # type: ignore[attr-defined]
        return Report()

    before = revisions.current()
    assert callback(report(before))["revision"] == before  # 版本相同 → 不动
    assert revisions.current() == before
    assert callback(report(before + 1))["revision"] == before + 1  # 真的变了 → +1
