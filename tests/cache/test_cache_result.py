"""结果缓存（`result_cache_get/set`）：TTL、键的契约、序列化的边界。

它与语义缓存的**正确性来源不同**（见 `app/cache/result.py` 的对照表）：
语义缓存靠阈值 + 溯源，结果缓存靠**调用方给的键足够精确**。
所以这一组用例的重点是「键」与「TTL」的契约，而不是相似度。
"""

from __future__ import annotations

import pytest

from app.cache.backend import MemoryKeyValue
from app.cache.result import RESULT_PREFIX, ResultCacheError, ResultStore
from app.cache.settings import CacheSettings

REPORT = {"rows": [{"order_id": 1024, "amount": "129.00"}], "total": 1}


@pytest.fixture
def store() -> ResultStore:
    return ResultStore(MemoryKeyValue(), namespace="cache", instance="test", settings=CacheSettings())


def test_set_then_get_round_trip(store: ResultStore) -> None:
    store.set(kind="report", key="u2:2026-09", value=REPORT, ttl_seconds=60)
    hit = store.get(kind="report", key="u2:2026-09")
    assert hit is not None
    assert hit.value == REPORT
    assert hit.age_seconds >= 0.0
    assert hit.kind == "report"


def test_missing_key_is_none_not_an_error(store: ResultStore) -> None:
    assert store.get(kind="report", key="nobody") is None
    assert store.misses == 1


def test_different_kinds_do_not_collide(store: ResultStore) -> None:
    """`kind` 是命名空间的一部分：`report` 与 `chart` 的同名键互不干扰。"""
    store.set(kind="report", key="u2:2026-09", value={"a": 1}, ttl_seconds=60)
    store.set(kind="chart", key="u2:2026-09", value={"b": 2}, ttl_seconds=60)
    assert store.get(kind="report", key="u2:2026-09").value == {"a": 1}
    assert store.get(kind="chart", key="u2:2026-09").value == {"b": 2}


def test_ttl_expiry_is_honoured_by_the_backend(store: ResultStore) -> None:
    """TTL 到点之后读不到（内存后端与 Redis 的 `EX` 语义一致，都是惰性过期）。"""
    store.set(kind="report", key="k", value={"a": 1}, ttl_seconds=1)
    assert store.get(kind="report", key="k") is not None
    # 直接把内存里的过期时刻挪到过去（比 sleep 1 秒快且稳定）
    for key in list(store.kv._data):  # type: ignore[attr-defined]
        value, _ = store.kv._data[key]  # type: ignore[attr-defined]
        store.kv._data[key] = (value, 0.0)  # type: ignore[attr-defined]
    assert store.get(kind="report", key="k") is None


def test_zero_ttl_is_rejected(store: ResultStore) -> None:
    """`ttl_seconds=0` **抛错**而不是「永久缓存」——让「忘了写 TTL」不可能静默变成永久。"""
    with pytest.raises(ResultCacheError, match="必须为正数"):
        store.set(kind="report", key="k", value=1, ttl_seconds=0)


def test_negative_ttl_is_rejected(store: ResultStore) -> None:
    with pytest.raises(ResultCacheError, match="必须为正数"):
        store.set(kind="report", key="k", value=1, ttl_seconds=-5)


@pytest.mark.parametrize("bad", ["", "   ", "a b", "a\nb", "x" * 201])
def test_invalid_keys_are_rejected(store: ResultStore, bad: str) -> None:
    """键**抛错而不是静默清洗**（第 12 步 `normalize_key` 同口径）。

    静默清洗的后果：`"a b"` 与 `"ab"` 会变成同一个键 —— 两个不同请求的内容互相顶替，
    而它比「拒绝写入」难查得多。
    """
    with pytest.raises(ResultCacheError):
        store.key_for("report", bad)


def test_key_layout_is_stable(store: ResultStore) -> None:
    """key 的形态是**跨进程契约**（排障时要在 redis-cli 里 `scan` 它）。"""
    assert store.key_for("report", "u2:2026-09") == "cache:test:result:report:u2:2026-09"
    assert RESULT_PREFIX == "result"


def test_datetime_is_serialized_not_rejected(store: ResultStore) -> None:
    """`datetime` 是唯一宽容处理的类型（报表里出现日期太常见，转 ISO 不丢信息）。"""
    from datetime import datetime, timezone

    moment = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    store.set(kind="report", key="k", value={"at": moment}, ttl_seconds=60)
    assert store.get(kind="report", key="k").value == {"at": "2026-10-13T12:00:00+00:00"}


def test_unserializable_value_is_rejected_loudly(store: ResultStore) -> None:
    """不可 JSON 化的值**立刻报错**（静默跳过会变成「永不命中的缓存」，查很久）。"""
    with pytest.raises(ResultCacheError, match="无法 JSON 序列化"):
        store.set(kind="report", key="k", value={"s": {1, 2, 3}}, ttl_seconds=60)


def test_delete_removes_one_entry(store: ResultStore) -> None:
    store.set(kind="report", key="k", value=1, ttl_seconds=60)
    assert store.delete(kind="report", key="k") is True
    assert store.get(kind="report", key="k") is None


def test_purge_by_kind(store: ResultStore) -> None:
    store.set(kind="report", key="a", value=1, ttl_seconds=60)
    store.set(kind="report", key="b", value=2, ttl_seconds=60)
    store.set(kind="chart", key="a", value=3, ttl_seconds=60)
    removed = store.purge(kind="report")
    assert removed == 2
    assert store.get(kind="chart", key="a") is not None


def test_state_counts_hits_and_misses(store: ResultStore) -> None:
    store.set(kind="report", key="k", value=1, ttl_seconds=60)
    store.get(kind="report", key="k")
    store.get(kind="report", key="missing")
    state = store.state()
    assert state["hits"] == 1
    assert state["misses"] == 1
    assert state["writes"] == 1
    assert state["namespace"].endswith("result:")


def test_age_uses_the_injected_now(store: ResultStore) -> None:
    """`now=` 让「这条躺了多久」可以被确定性断言（不必 sleep）。"""
    store.set(kind="report", key="k", value=1, ttl_seconds=600, now=1000.0)
    hit = store.get(kind="report", key="k", now=1042.0)
    assert hit.age_seconds == pytest.approx(42.0)


def test_gateway_functions_use_the_process_default(cache) -> None:
    """`result_cache_get` / `result_cache_set` 走**进程默认**缓存（文档 §2 的接口）。"""
    from app.cache import result_cache_get, result_cache_set

    written = result_cache_set(kind="report", key="u2:2026-09", value=REPORT, ttl_seconds=60)
    assert written > 0
    assert result_cache_get(kind="report", key="u2:2026-09") == REPORT
    assert result_cache_get(kind="report", key="unknown") is None
