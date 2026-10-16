"""真库那一组：`PostgresEntryStore`（pgvector 的相似度查询 + upsert + 显式失效）。

依赖不可用时整组 `skip`（`tests/conftest.py` 的 `db` 标记装置），
与第 10 步 `tests/rag/test_rag_store_db.py` 同一口径。

**为什么这一组非要有真库**：本 store 的全部价值在于**一条 SQL**：

```sql
order by embedding <=> cast(:query as vector) limit 5
```

「HNSW 索引吃不吃得到」「`1 - distance` 是不是余弦相似度」「upsert 的唯一键对不对」
这三件事**只有在真 PostgreSQL + pgvector 上才能回答**。用假库测它等于什么都没测。
"""

from __future__ import annotations

import pytest

from app.cache.schema import (
    ENTRIES_TABLE,
    VERSIONS_TABLE,
    create_statements,
    drop_statements,
)
from app.cache.stores import PostgresEntryStore, ScopeContext, new_entry

pytestmark = pytest.mark.db

TEST_ENTRIES_TABLE = "test_cache_semantic_entries"
TEST_VERSIONS_TABLE = "test_cache_corpus_versions"


def _embedding(seed: float, dim: int = 1024) -> tuple[float, ...]:
    """一个确定性向量（第一维给 seed，其余给一点同向分量 —— 保证单位化后可比）。"""
    values = [seed] + [1.0 - seed] * (dim - 1)
    norm = sum(value * value for value in values) ** 0.5 or 1.0
    return tuple(value / norm for value in values)


@pytest.fixture
def store(pg_ready) -> PostgresEntryStore:
    """临时表上的 store（用完删表）—— 与第 10 步的 `scratch_store` 同一套做法。

    ⚠️ **建表之后要给只读角色补一次授权**（本仓实测踩到）：本 store 的**读路径**
    走 `get_read_engine()`（= `shopsage_readonly`），而临时表是刚建的，
    只读角色默认一张也读不了 —— 症状是每条用例都红在
    `permission denied for table test_cache_semantic_entries`。
    这与生产上的处置完全一样（`scripts/db_readonly.py --regrant-only`，
    它在每次完整迁移回环之后都要重放一次）：**GRANT 是挂在表对象上的，
    表被重建就消失**。所以这里不是「测试的后门」，而是把那条运维动作复制了一份。
    """
    from app.db.session import get_read_engine, get_write_engine

    engine = get_write_engine()
    statements = create_statements(
        1024, entries_table=TEST_ENTRIES_TABLE, versions_table=TEST_VERSIONS_TABLE
    )
    drops = drop_statements(entries_table=TEST_ENTRIES_TABLE, versions_table=TEST_VERSIONS_TABLE)
    with engine.begin() as conn:
        for statement in drops:
            conn.exec_driver_sql(statement)
        for statement in statements:
            conn.exec_driver_sql(statement)
    _grant_readonly(TEST_ENTRIES_TABLE, TEST_VERSIONS_TABLE)

    instance = PostgresEntryStore(
        engine=engine,
        read_engine=get_read_engine(),
        entries_table=TEST_ENTRIES_TABLE,
        versions_table=TEST_VERSIONS_TABLE,
        dimension=1024,
    )
    try:
        yield instance
    finally:
        with engine.begin() as conn:
            for statement in drops:
                conn.exec_driver_sql(statement)


def _grant_readonly(*tables: str) -> None:
    """给只读角色补 `SELECT`（与 `scripts/db_readonly.py` 的同名动作一致）。"""
    from app.db.session import get_write_engine

    role = "shopsage_readonly"
    with get_write_engine().begin() as conn:
        for table in tables:
            conn.exec_driver_sql(f'GRANT SELECT ON TABLE "{table}" TO {role}')


def entry(question: str, *, vector: tuple[float, ...] | None = None, scope: str = "customer:2", ttl: int = 3600, revision: int = 1):
    return new_entry(
        context=ScopeContext(scope=scope, fingerprint="fp"),
        question=question,
        raw_question=question,
        embedding=vector if vector is not None else _embedding(0.9),
        answer=f"{question} 的答案 [1]",
        citations=("知识库/退货政策.md#第1条",),
        revision=revision,
        policy="normal",
        ttl_seconds=ttl,
    )


def test_table_has_a_hnsw_index_and_a_unique_identity(store: PostgresEntryStore) -> None:
    """三个索引都在（HNSW / 唯一键 / 活跃 scope）。少了唯一键，「同一句话写两次」会变两行。"""
    from sqlalchemy import text

    with store.read_engine.connect() as conn:
        defs = {
            row.indexname: row.indexdef
            for row in conn.execute(
                text(
                    "select indexname, indexdef from pg_indexes "
                    f"where tablename = '{TEST_ENTRIES_TABLE}'"
                )
            ).all()
        }
    assert any("hnsw" in value for value in defs.values()), defs
    # ⚠️ 判据要**按索引名**找，不能靠 `"unique" in indexdef`：
    # 主键索引的 `indexdef` 里也有 `UNIQUE`（那是它自带的），于是那条断言恒真 ——
    # 而它本该验的是「唯一键是 (scope, fingerprint, question_hash)」这件事。
    identity = defs.get(f"{TEST_ENTRIES_TABLE}_identity", "")
    assert "unique" in identity.lower(), defs
    assert "scope" in identity and "fingerprint" in identity and "question_hash" in identity, identity
    assert any("scope, expires_at" in value for value in defs.values()), defs


def test_put_then_get_round_trip(store: PostgresEntryStore) -> None:
    store.put(entry("怎么退货"))
    got = store.get(ScopeContext("customer:2", "fp"), "怎么退货")
    assert got is not None
    assert got.answer == "怎么退货 的答案 [1]"
    assert got.citations == ("知识库/退货政策.md#第1条",)
    assert got.ttl_seconds == 3600


def test_vector_search_orders_by_cosine_similarity(store: PostgresEntryStore) -> None:
    """向量腿：最近邻真的排在前面（`1 - (a <=> b)` = 余弦相似度）。"""
    store.put(entry("怎么退货", vector=_embedding(0.99)))
    store.put(entry("怎么改地址", vector=_embedding(0.01)))
    found = store.nearest(ScopeContext("customer:2", "fp"), _embedding(0.99), limit=2)
    assert [item.entry.question for item in found] == ["怎么退货", "怎么改地址"]
    assert found[0].similarity > found[1].similarity
    assert found[0].similarity == pytest.approx(1.0, abs=1e-4)


def test_nearest_is_scoped_by_scope_and_fingerprint(store: PostgresEntryStore) -> None:
    store.put(entry("a", scope="customer:2"))
    store.put(entry("b", scope="customer:9"))
    found = store.nearest(ScopeContext("customer:2", "fp"), _embedding(0.9), limit=5)
    assert [item.entry.question for item in found] == ["a"]


def test_nearest_returns_inactive_rows_too(store: PostgresEntryStore) -> None:
    """**刻意不过滤过期/失效行**（见 `PostgresEntryStore.nearest` 的说明）：
    「为什么没命中」要能看到那条过期/旧版本的候选 —— 那是排障唯一有用的信息。"""
    store.put(entry("怎么退货", ttl=1))
    found = store.nearest(ScopeContext("customer:2", "fp"), _embedding(0.9), limit=5)
    assert [item.entry.question for item in found] == ["怎么退货"]


def test_put_is_an_upsert_not_a_duplicate(store: PostgresEntryStore) -> None:
    """同一 `(scope, fingerprint, question_hash)` 写两次只有一行（唯一索引 + `do update`）。"""
    first = store.put(entry("怎么退货"))
    assert first is True  # 新建
    second = store.put(entry("怎么退货"))
    assert second is False  # 覆盖
    assert store.count() == 1


def test_upsert_refreshes_the_answer_and_keeps_the_hit_count(store: PostgresEntryStore) -> None:
    """重写会刷新答案与时间戳，但 **`hit_count` 不重置**（它是这条问题的历史命中次数）。"""
    store.put(entry("怎么退货"))
    Context = ScopeContext("customer:2", "fp")
    original = store.get(Context, "怎么退货")
    assert original is not None
    store.touch(original)
    assert store.get(Context, "怎么退货").hit_count == 1

    updated = entry("怎么退货")
    updated = type(updated)(**{**updated.__dict__, "answer": "新答案", "hit_count": 5})
    store.put(updated)
    after = store.get(Context, "怎么退货")
    assert after.answer == "新答案"
    assert after.hit_count == 5


def test_deactivate_makes_the_row_invisible_but_kept(store: PostgresEntryStore) -> None:
    """失效是 `is_active=false`（行留着）—— 与第 10 步「片段失效」同一口径。"""
    store.put(entry("怎么退货"))
    assert store.deactivate(ScopeContext("customer:2", "fp"), "怎么退货") is True
    assert store.get(ScopeContext("customer:2", "fp"), "怎么退货") is None
    assert store.count(only_active=False) == 1


def test_deactivate_without_fingerprint_matches_any_version(store: PostgresEntryStore) -> None:
    """空指纹 = 通配（主动失效不必知道指纹，见 `invalidate.py` 的说明）。"""
    store.put(entry("怎么退货"))
    assert store.deactivate(ScopeContext(scope="customer:2", fingerprint=""), "怎么退货") is True


def test_deactivate_scope_clears_one_user(store: PostgresEntryStore) -> None:
    store.put(entry("a", scope="customer:2"))
    store.put(entry("b", scope="customer:2"))
    store.put(entry("c", scope="customer:9"))
    assert store.deactivate_scope("customer:2") == 2
    assert store.count(scope="customer:9") == 1


def test_count_only_counts_active_unexpired_rows(store: PostgresEntryStore) -> None:
    store.put(entry("a", ttl=3600))
    store.put(entry("b", ttl=1))
    # `count(only_active=True)` 用的是 `expires_at >= now()`，所以 ttl=1 还没到点
    assert store.count() == 2
    import time

    time.sleep(1.1)
    assert store.count() == 1
    assert store.count(only_active=False) == 2


def test_purge_only_removes_expired_by_default(store: PostgresEntryStore) -> None:
    """`purge` 默认只删过期（「清空缓存」必须是显式动作）。"""
    import time

    store.put(entry("old", ttl=1))
    store.put(entry("new", ttl=3600))
    time.sleep(1.1)
    assert store.purge() == 1
    assert store.count(only_active=False) == 1


def test_dimension_mismatch_is_actionable(store: PostgresEntryStore) -> None:
    """维度不符 → 报错里写清「该改哪三处」（而不是让 PostgreSQL 报一句难懂的话）。"""
    from app.cache.stores import CacheStoreError

    bad = entry("怎么退货", vector=(0.1, 0.2, 0.3))
    with pytest.raises(CacheStoreError, match="维度"):
        store.put(bad)


def test_health_reports_dimension_and_count(store: PostgresEntryStore) -> None:
    store.put(entry("怎么退货"))
    health = store.health()
    assert health["exists"] is True
    assert health["dim"] == 1024
    assert health["dim_ok"] is True
    assert health["entries"] == 1


def test_missing_table_error_is_actionable(pg_ready) -> None:
    """表不存在 → 报错里写着「先跑 `alembic upgrade head`」（而不是让驱动报一句原始的）。"""
    from app.cache.stores import CacheStoreError

    from app.db.session import get_read_engine, get_write_engine

    orphan = PostgresEntryStore(
        engine=get_write_engine(),
        read_engine=get_read_engine(),
        entries_table="test_cache_does_not_exist",
        dimension=1024,
    )
    with pytest.raises(CacheStoreError, match="alembic upgrade head"):
        orphan.get(ScopeContext("customer:2", "fp"), "怎么退货")


def test_production_tables_exist_and_are_wired(pg_ready) -> None:
    """生产的两张表真的在库里、维度与配置一致（迁移已跑过）。"""
    from app.cache.schema import current_vector_dim, table_exists
    from app.db.session import get_read_engine

    with get_read_engine().connect() as conn:
        assert table_exists(conn, ENTRIES_TABLE) is True
        assert table_exists(conn, VERSIONS_TABLE) is True
        assert current_vector_dim(conn, ENTRIES_TABLE) == 1024


def test_readonly_role_can_read_the_cache_tables(pg_ready) -> None:
    """只读角色必须能读这两张表（缓存的读路径走 `get_read_engine()`）。

    漏授权**不会报错**：读被拒 → `_safe` 吞掉 → 未命中 →
    症状只是「缓存永远不命中」（看起来像「阈值调高了」）。
    所以这条用例是必要的 —— 它把那个静默状态变成一次失败。
    """
    from app.db.session import get_read_engine

    with get_read_engine().connect() as conn:
        cur = conn.exec_driver_sql("select current_user").scalar()
        assert cur == "shopsage_readonly", f"读引擎没落在只读角色上，而是 {cur}"
        conn.exec_driver_sql(f"select count(*) from {ENTRIES_TABLE}").scalar()
        conn.exec_driver_sql(f"select corpus_revision from {VERSIONS_TABLE}").scalar()


def test_revision_store_bumps_monotonically(pg_ready) -> None:
    """版本号单调递增（并发的入库也不会拿到更小的版本）。"""
    from app.cache.corpus import PostgresRevisionStore

    revisions = PostgresRevisionStore()
    start = revisions.current()
    assert revisions.bump(by="test") == start + 1
    assert revisions.bump(by="test") == start + 2
    assert revisions.current() == start + 2
