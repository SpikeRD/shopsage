"""真 Redis 那一组：**我们对 Redis 的假设是错的吗**（默认 skip，`SHOPSAGE_TEST_REDIS=1` 开门）。

与第 12 步的 `tests/queue/test_queue_redis.py` 同一道门、同一个理由：
「没起 Redis 就红一片」会让本步的验收基线不可用。这里验的是**本模块对 Redis 的用法**：

| 假设 | 错了会怎样 |
|---|---|
| `SET key value EX n` 真的会到点消失 | 条目永久不过期（缓存讲老政策） |
| `SCAN MATCH` 能扫到我们写的 key（含 `|` 与中文） | `--purge` / 计数静默返回 0 |
| `DEL` 多 key 一次删掉（我们用它做按 scope 失效） | 按 scope 清缓存只清掉一部分 |
| 二进制安全：值里有中文/JSON 也能原样读回 | 读回来的是乱码（而"没命中"看起来一样） |
| 我们的 key 用 `scan_iter` 能清干净 | 跑一轮留下一堆测试 key（本仓的老毛病） |

⚠️ 门关时**整份文件 skip**（不是「失败」）—— 与第 12 步同一个取舍。
"""

from __future__ import annotations

import os
import time

import pytest

from app.cache.backend import (
    CacheKeySpace,
    RedisEntryStore,
    RedisKeyValue,
    key_value_ttl,
)
from app.cache.stores import ScopeContext, new_entry

pytestmark = pytest.mark.skipif(
    os.environ.get("SHOPSAGE_TEST_REDIS") != "1",
    reason="需要真 Redis：设 `SHOPSAGE_TEST_REDIS=1` 才跑（与第 12 步同一道门）",
)

#: 专用命名空间实例名 —— 与业务 key（`cache:<ENV>:...`）隔开，
#: 跑完按**已知前缀**精确清理（不用 `KEYS *`，也不动别人的数据）。
TEST_INSTANCE = "cachetest"


@pytest.fixture(scope="module")
def kv() -> RedisKeyValue:
    from app.config import get_settings

    url = str(get_settings().redis_url or "")
    if not url:
        pytest.skip("REDIS_URL 为空 —— 先在 .env 里填好（见 2.infra-runbook.md）")
    client = RedisKeyValue(url=url, timeout_seconds=1.0)
    if not client.ping():
        pytest.skip(f"Redis 不可达：{url.split('@')[-1]}")
    yield client
    client.close()


@pytest.fixture
def store(kv: RedisKeyValue) -> RedisEntryStore:
    keys = CacheKeySpace(namespace="cache", instance=TEST_INSTANCE)
    instance = RedisEntryStore(kv, keys=keys, max_entries=100)
    yield instance
    # **按已知前缀精确清理**（不用 `KEYS *`：那会在大实例上阻塞 Redis，也会清掉别人的 key）
    leftovers = kv.scan_keys(f"{keys.prefix()}*")
    if leftovers:
        kv.delete(*leftovers)


def entry(question: str = "怎么退货", *, scope: str = "customer:2", ttl: int = 60):
    return new_entry(
        context=ScopeContext(scope=scope, fingerprint="fp"),
        question=question,
        raw_question=question,
        embedding=(1.0,) + (0.0,) * 7,
        answer="7 天内可无理由退货。[1]",
        citations=("政策.md#1",),
        revision=1,
        policy="normal",
        ttl_seconds=ttl,
    )


def test_set_get_round_trip_with_chinese(kv: RedisKeyValue, store: RedisEntryStore) -> None:
    """二进制安全 + UTF-8：值里有中文、JSON、`|`，读回来必须**一模一样**。"""
    store.put(entry())
    got = store.get(ScopeContext("customer:2", "fp"), "怎么退货")
    assert got is not None
    assert got.answer == "7 天内可无理由退货。[1]"
    assert got.citations == ("政策.md#1",)


def test_redis_key_layout_is_the_documented_one(kv: RedisKeyValue, store: RedisEntryStore) -> None:
    """真 Redis 里能 `scan` 到我们写的 key，且形态与 `CacheKeySpace` 一致。"""
    store.put(entry())
    expected = store.keys.entry(ScopeContext("customer:2", "fp"), "怎么退货")
    assert expected in kv.scan_keys(f"{store.keys.prefix()}*")


def test_ttl_really_expires(kv: RedisKeyValue) -> None:
    """`set(ex=...)` 真的到点消失（本用例把宽限调到 1 秒，否则要等 60 秒）。

    ⚠️ 这条是本模块对 Redis 的**核心假设**：条目不能永久躺在那里 ——
    它一旦不过期，就会在知识库更新不到的那个窗口里一直讲老政策。
    生产口径的宽限是 `KEY_VALUE_TTL_GRACE_SECONDS`（60 秒），
    这里覆盖成 1 秒只为让用例跑得快（`backend.key_value_ttl` 是唯一算法）。

    ⚠️ 用**自己的 instance 名**（不复用 `store` 装置）：两个 store 写同一个 key 时，
    后写的那次会把先写的 TTL **覆盖掉**（实测：`store` 装置的 `put` 把这条的 TTL
    从 1 秒改成 60 秒，于是「等 1.3 秒该消失」变成「还在」）。
    这类「两条命令互相盖掉对方」的干扰在真 Redis 上是静默的 —— 因为它就是 Redis 的语义。
    """
    from app.cache.backend import CacheKeySpace as _Keys

    keys = _Keys(namespace="cache", instance=f"{TEST_INSTANCE}-ttl")
    tight = RedisEntryStore(kv, keys=keys, grace_seconds=1)
    try:
        tight.put(entry(ttl=0))  # 应用层会判它过期；键值层给它 1 秒
        key = keys.entry(ScopeContext("customer:2", "fp"), "怎么退货")
        assert kv.get(key) is not None
        time.sleep(1.3)
        assert kv.get(key) is None
    finally:
        leftovers = kv.scan_keys(f"{keys.prefix()}*")
        if leftovers:
            kv.delete(*leftovers)


def test_scan_finds_keys_with_pipes_and_cjk(store: RedisEntryStore) -> None:
    """`SCAN MATCH` 能扫到含 `|` 与中文 hash 段的 key（`--purge` 靠它）。"""
    store.put(entry("怎么退货"))
    store.put(entry("怎么开票"))
    found = store.count()
    assert found == 2


def test_deactivate_scope_deletes_all_of_that_scope(store: RedisEntryStore) -> None:
    store.put(entry("a", scope="customer:2"))
    store.put(entry("b", scope="customer:2"))
    store.put(entry("c", scope="customer:9"))
    assert store.deactivate_scope("customer:2") == 2
    assert store.count(scope="customer:9") == 1


def test_nearest_scans_only_the_same_scope(store: RedisEntryStore) -> None:
    store.put(entry("a", scope="customer:2"))
    store.put(entry("b", scope="customer:9"))
    found = store.nearest(ScopeContext("customer:2", "fp"), [1.0] + [0.0] * 7, limit=5)
    assert [item.entry.question for item in found] == ["a"]


def test_embedding_cache_round_trip_on_real_redis(kv: RedisKeyValue) -> None:
    from app.cache.backend import build_embedding_cache

    keys = CacheKeySpace(instance=TEST_INSTANCE)
    cache = build_embedding_cache(kv, keys=keys, ttl_seconds=60)
    try:
        cache.set(model="fake/model", text="怎么退货", vector=[0.25, 0.5])
        assert cache.get(model="fake/model", text="怎么退货") == [0.25, 0.5]
        assert cache.get(model="other/model", text="怎么退货") is None
    finally:
        leftovers = kv.scan_keys(f"{keys.prefix()}embed:*")
        if leftovers:
            kv.delete(*leftovers)


def test_purge_removes_the_whole_namespace(store: RedisEntryStore) -> None:
    store.put(entry("a"))
    store.put(entry("b"))
    assert store.purge(only_expired=False) == 2
    assert store.count() == 0


def test_result_store_on_real_redis(kv: RedisKeyValue) -> None:
    """结果缓存也走真 Redis（`result_cache_*` 的生产路径）。"""
    from app.cache.result import ResultStore
    from app.cache.settings import CacheSettings

    results = ResultStore(kv, namespace="cache", instance=TEST_INSTANCE, settings=CacheSettings())
    try:
        results.set(kind="report", key="u2:2026-09", value={"rows": 3}, ttl_seconds=60)
        hit = results.get(kind="report", key="u2:2026-09")
        assert hit is not None and hit.value == {"rows": 3}
        assert results.purge(kind="report") == 1
    finally:
        leftovers = kv.scan_keys(f"cache:{TEST_INSTANCE}:result:*")
        if leftovers:
            kv.delete(*leftovers)


def test_cleanup_keys_are_derived_not_discovered(kv: RedisKeyValue, store: RedisEntryStore) -> None:
    """纪律：清理用的前缀**从 `CacheKeySpace` 推导**（不是 `KEYS *` 扫全库）。

    这条用源码断言钉住 —— 与第 12 步 `test_queue_redis.py` 的同名用例同一个理由：
    「跑一轮留下十几个 key」这种毛病，靠人记得清理是治不住的。
    """
    import inspect

    from app.cache import backend as module

    source = inspect.getsource(module)
    assert "scan_iter" in source
    assert "keys(" not in source.replace("scan_keys(", "")  # 不出现 `.keys(` 这种全库调用
    assert "flushdb" not in source.lower(), "绝不能 flushdb（会把同一个实例上别的库/项目全清掉）"
