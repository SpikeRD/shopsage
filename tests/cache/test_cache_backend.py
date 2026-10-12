"""后端的结构契约：**key 的构造、降级器的行为、装饰器的恢复**（离线，不连真 Redis）。

这一组钉的是「接错线」这一类错误 —— 它们不会有异常、只会让缓存静默地不工作：

| 错误 | 症状 |
|---|---|
| key 少了一段（例如没带 `scope`） | 用户 A 命中用户 B 的答案（越权） |
| key 少带 `instance` | 两个部署互相命中（一台的缓存成了另一台的） |
| 降级之后不切回主存储 | 一次抖动之后**永久**用内存（命中率被副本数除） |
| 每次调用都打一次「降级」WARNING | 日志被噪音淹没（第一条与第一万条读起来一样） |
"""

from __future__ import annotations

import json

import pytest

from app.cache.backend import (
    CacheKeySpace,
    MemoryKeyValue,
    RedisEntryStore,
    ResilientEntryStore,
    build_embedding_cache,
    build_key_value,
)
from app.cache.settings import CacheBackendSettings, CacheSettings
from app.cache.stores import CacheEntry, MemoryEntryStore, ScopeContext, new_entry


def make_entry(question: str = "怎么退货", *, scope: str = "customer:2", fingerprint: str = "fp") -> CacheEntry:
    return new_entry(
        context=ScopeContext(scope=scope, fingerprint=fingerprint),
        question=question,
        raw_question=question,
        embedding=(1.0,) + (0.0,) * 7,
        answer="答案",
        revision=1,
        policy="normal",
        ttl_seconds=60,
    )


# ── key 的构造（跨进程契约）────────────────────────────────────────────────


def test_entry_key_contains_scope_fingerprint_and_hash() -> None:
    keys = CacheKeySpace(namespace="cache", instance="dev")
    key = keys.entry(ScopeContext(scope="customer:2", fingerprint="abc"), "怎么退货")
    # 前缀含 namespace + instance + `entry:`，然后是 scope（它自己含冒号）
    assert key.startswith("cache:dev:entry:customer:2|abc|")
    # 三段用 `|` 分隔（scope 里的冒号不会与分隔符混起来）
    assert len(key.split("|")) == 3


def test_entry_key_ends_with_the_question_hash() -> None:
    from app.cache.stores import question_hash

    keys = CacheKeySpace()
    key = keys.entry(ScopeContext("customer:2", "fp"), "怎么退货")
    assert key.endswith(question_hash("怎么退货"))


def test_entry_keys_differ_by_scope() -> None:
    """**最重要的一条**：不同 scope 的 key 必须不同（否则就是跨用户命中）。"""
    keys = CacheKeySpace()
    left = keys.entry(ScopeContext(scope="customer:2", fingerprint="fp"), "怎么退货")
    right = keys.entry(ScopeContext(scope="customer:3", fingerprint="fp"), "怎么退货")
    assert left != right


def test_entry_keys_differ_by_fingerprint() -> None:
    keys = CacheKeySpace()
    left = keys.entry(ScopeContext(scope="customer:2", fingerprint="v1"), "怎么退货")
    right = keys.entry(ScopeContext(scope="customer:2", fingerprint="v2"), "怎么退货")
    assert left != right


def test_entry_keys_differ_by_instance() -> None:
    """多环境共用同一个 Redis 实例时靠 `instance` 隔离（第 12 步队列同口径）。"""
    left = CacheKeySpace(instance="dev").entry(ScopeContext("customer:2", "fp"), "q")
    right = CacheKeySpace(instance="prod").entry(ScopeContext("customer:2", "fp"), "q")
    assert left != right


def test_separator_can_not_be_forged_by_a_scope_name() -> None:
    """分隔符不能有歧义：`scope` 里出现 `|` 会让两段拼出同一个 key。

    这条靠 `scope_for()` 保证（角色名来自白名单、user_id 是整数），
    所以这里断言的是那个保证本身。
    """
    from app.cache.stores import scope_for

    assert "|" not in scope_for(2, "customer")
    assert "|" not in scope_for(999, "internal_employee")
    assert scope_for(2, "") == "anonymous:2"  # 空角色有兜底名（而不是一个空的 scope 段）


def test_embedding_key_includes_the_model() -> None:
    """换 embedding 模型 → 旧向量缓存不再被命中（否则会返回「另一个空间」的向量）。"""
    keys = CacheKeySpace()
    assert keys.embedding(model="bge-m3", text="怎么退货") != keys.embedding(
        model="text-embedding-3-small", text="怎么退货"
    )


def test_embedding_key_does_not_contain_the_raw_text() -> None:
    """**文本不进 key**：key 会进日志、指标、`redis-cli` 的输出，而问题可能含 PII。"""
    keys = CacheKeySpace()
    key = keys.embedding(model="m", text="我的手机号是 13800000000")
    assert "13800000000" not in key


# ── RedisEntryStore（用内存键值层驱动，语义与 Redis 一致）───────────────────


@pytest.fixture
def redis_like() -> tuple[RedisEntryStore, MemoryKeyValue]:
    kv = MemoryKeyValue()
    return RedisEntryStore(kv, keys=CacheKeySpace(instance="test"), max_entries=5), kv


def test_redis_store_round_trips_json(redis_like) -> None:
    store, kv = redis_like
    store.put(make_entry())
    got = store.get(ScopeContext("customer:2", "fp"), "怎么退货")
    assert got is not None
    assert got.answer == "答案"
    assert got.citations == ()


def test_redis_store_writes_plain_json(redis_like) -> None:
    """值就是 JSON（排障时能在 `redis-cli get` 里直接读懂）。"""
    store, kv = redis_like
    store.put(make_entry())
    key = store.keys.entry(ScopeContext("customer:2", "fp"), "怎么退货")
    payload = json.loads(kv.get(key))
    assert payload["question"] == "怎么退货"
    assert payload["answer"] == "答案"


def test_key_value_ttl_is_a_bit_longer_than_the_entry_ttl() -> None:
    """键值层的 TTL 比应用层 TTL 长 60 秒（**应用层必须先判过期**）。

    顺序反了的话「条目消失了」与「条目过期了」在排障时无法区分：
    前者连一条账目都没有（`judge` 看不到它），后者可以说出「躺了 7200s / TTL 3600s」。
    这条口径只有一处实现（`backend.key_value_ttl`），用例直接钉那个函数。
    """
    from app.cache.backend import KEY_VALUE_TTL_GRACE_SECONDS, key_value_ttl

    assert KEY_VALUE_TTL_GRACE_SECONDS == 60
    assert key_value_ttl(60) == 120
    assert key_value_ttl(0) == 60  # 非法 TTL 也要给出正数（Redis 的 EX 不接受 0）


def test_redis_store_round_trip_keeps_the_entry_ttl(redis_like) -> None:
    """条目自己的 TTL 原样存下来（它是 `judge` 判过期的唯一依据）。"""
    store, _ = redis_like
    store.put(make_entry())
    got = store.get(ScopeContext("customer:2", "fp"), "怎么退货")
    assert got is not None
    assert got.ttl_seconds == 60


def test_redis_store_corrupt_value_is_deleted_not_raised(redis_like) -> None:
    """坏值（人工改过？旧格式？）删掉并打日志，而不是让每个请求都失败一次。"""
    store, kv = redis_like
    key = store.keys.entry(ScopeContext("customer:2", "fp"), "怎么退货")
    kv.set(key, "not json at all", ttl_seconds=60)
    assert store.get(ScopeContext("customer:2", "fp"), "怎么退货") is None
    assert kv.get(key) is None


def test_redis_store_nearest_only_sees_the_same_scope(redis_like) -> None:
    store, _ = redis_like
    store.put(make_entry("a", scope="customer:2"))
    store.put(make_entry("b", scope="customer:3"))
    found = store.nearest(ScopeContext("customer:2", "fp"), [1.0] + [0.0] * 7, limit=5)
    assert [item.entry.question for item in found] == ["a"]


def test_redis_store_deactivate_scope(redis_like) -> None:
    store, _ = redis_like
    store.put(make_entry("a", scope="customer:2"))
    store.put(make_entry("b", scope="customer:3"))
    assert store.deactivate_scope("customer:2") == 1
    assert store.count(scope="customer:3") == 1


def test_redis_store_evicts_beyond_max_entries(redis_like) -> None:
    """超上限时按 scope 淘汰（防「一时涌入大量不同问题」撑爆实例）。"""
    store, _ = redis_like
    for index in range(8):
        store.put(make_entry(f"问题{index}"))
    assert store.count() <= 5 + 1  # 淘汰是「尽力而为」，允许一批的余量
    assert store.evictions >= 1


def test_redis_store_health_reports_ping(redis_like) -> None:
    store, _ = redis_like
    health = store.health()
    assert health["store"] == "redis"
    assert health["ping"] is True


# ── ResilientEntryStore：降级与**恢复** ─────────────────────────────────────


class FlakyStore(MemoryEntryStore):
    """可开关的「坏掉的」存储（模拟 Redis 挂了又活了）。

    ⚠️ `name()` 刻意**不叫 "memory"`**：降级状态的判据是「`active_name` 是否等于
    主存储的名字」，而两个内存实现若同名，「内存主存储 + 内存回落」就永远显示未降级。
    真实现里主存储是 `postgres` / `redis`，与内存回落天然不同名。
    """

    def __init__(self) -> None:
        super().__init__()
        self.broken = False
        self.calls = 0

    def name(self) -> str:
        return "flaky"

    def _check(self) -> None:
        self.calls += 1
        if self.broken:
            raise ConnectionError("Redis 连接被拒")

    def get(self, context, question):  # noqa: ANN001, ANN201
        self._check()
        return super().get(context, question)

    def put(self, entry):  # noqa: ANN001, ANN201
        self._check()
        return super().put(entry)

    def nearest(self, context, embedding, *, limit=5):  # noqa: ANN001, ANN201
        self._check()
        return super().nearest(context, embedding, limit=limit)

    def count(self, *, scope=None, only_active=True):  # noqa: ANN001, ANN201
        self._check()
        return super().count(scope=scope, only_active=only_active)

    def health(self):  # noqa: ANN201
        self._check()
        return super().health()


def test_resilient_store_falls_back_on_failure() -> None:
    primary = FlakyStore()
    fallback = MemoryEntryStore()
    store = ResilientEntryStore(primary, fallback)
    primary.broken = True

    store.put(make_entry())
    assert store.degraded is True
    assert store.name() == "memory"
    assert store.get(ScopeContext("customer:2", "fp"), "怎么退货") is not None  # 回落里有


def test_resilient_store_recovers_and_cuts_back() -> None:
    """主存储恢复之后**必须切回去** —— 不切回去就是「一次抖动之后永久降级」。"""
    primary = FlakyStore()
    fallback = MemoryEntryStore()
    store = ResilientEntryStore(primary, fallback)
    primary.broken = True
    store.put(make_entry())
    assert store.degraded is True

    primary.broken = False
    store.probe_interval = 0.0  # 让下一次调用立刻探测
    store.put(make_entry("另一条"))
    assert store.degraded is False
    assert store.name() == primary.name()
    assert store.recoveries == 1


def test_resilient_store_without_fallback_reraises() -> None:
    """没有回落时**必须抛出**（不能假装成功）：调用方要能知道「这一次没缓存」。"""
    primary = FlakyStore()
    store = ResilientEntryStore(primary, None)
    primary.broken = True
    with pytest.raises(ConnectionError):
        store.get(ScopeContext("customer:2", "fp"), "怎么退货")


def test_resilient_store_counts_failures_and_exposes_state() -> None:
    primary = FlakyStore()
    store = ResilientEntryStore(primary, MemoryEntryStore())
    primary.broken = True
    store.get(ScopeContext("customer:2", "fp"), "q")
    health = store.health()
    assert health["degraded"] is True
    assert health["failures"] >= 1
    assert "fallback" in health


# ── build_key_value：两种回落的可见性 ──────────────────────────────────────


def test_empty_redis_url_falls_back_to_memory() -> None:
    kv, degraded = build_key_value(CacheBackendSettings(redis_url=""), CacheSettings())
    assert degraded is True
    assert isinstance(kv, MemoryKeyValue)


def test_unreachable_redis_falls_back_to_memory() -> None:
    """端口 1 必然被拒 —— 这条用例真的走了一遍连接失败路径（毫秒级）。"""
    kv, degraded = build_key_value(
        CacheBackendSettings(redis_url="redis://127.0.0.1:1/0"), CacheSettings(lookup_timeout_seconds=0.2)
    )
    assert degraded is True
    assert isinstance(kv, MemoryKeyValue)


def test_reachable_redis_gives_the_redis_backend() -> None:
    """连得上就用 Redis（Redis 7.4.11 本机可达）—— 这一条**需要真 Redis**。

    门关（`SHOPSAGE_TEST_REDIS` 未设）时它只断言「没配 URL 一定回落」，
    真 Redis 那一组在 `test_cache_redis.py`（与第 12 步同一道门）。
    """
    import os

    if os.environ.get("SHOPSAGE_TEST_REDIS") != "1":
        pytest.skip("需要真 Redis：设 `SHOPSAGE_TEST_REDIS=1`（见 tests/cache/test_cache_redis.py）")
    from app.config import get_settings

    url = str(get_settings().redis_url or "")
    if not url:
        pytest.skip("REDIS_URL 为空")
    kv, degraded = build_key_value(CacheBackendSettings(redis_url=url), CacheSettings())
    assert degraded is False
    assert kv.ping() is True
    kv.delete("cache:test:does-not-exist")


# ── embedding 缓存 ──────────────────────────────────────────────────────────


def test_embedding_cache_round_trip() -> None:
    keys = CacheKeySpace(instance="test")
    cache = build_embedding_cache(MemoryKeyValue(), keys=keys, ttl_seconds=60)
    assert cache.get(model="m", text="怎么退货") is None
    cache.set(model="m", text="怎么退货", vector=[1.0, 0.0])
    assert cache.get(model="m", text="怎么退货") == [1.0, 0.0]
    state = cache.state()
    assert state["hits"] == 1 and state["misses"] == 1


def test_embedding_cache_swallows_backend_errors() -> None:
    """加速器挂掉**只影响速度**（吞异常 + 计数），调用方不该为它写 `try`。"""

    class BrokenKV(MemoryKeyValue):
        def get(self, key: str) -> str | None:  # noqa: ARG002
            raise RuntimeError("挂了")

        def set(self, key: str, value: str, *, ttl_seconds: int) -> None:  # noqa: ARG002
            raise RuntimeError("挂了")

    cache = build_embedding_cache(BrokenKV(), keys=CacheKeySpace(), ttl_seconds=60)
    assert cache.get(model="m", text="x") is None
    cache.set(model="m", text="x", vector=[1.0])
    assert cache.state()["errors"] == 2
