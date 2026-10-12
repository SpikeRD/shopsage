"""结构契约：**分层、出口清单、单一真相**（离线、不依赖任何外部服务）。

这一组对应 `13.cache.md` §2 的「对外接口」与分层纪律，用 AST/源码断言而不是读代码：

| 契约 | 违反后的症状 |
|---|---|
| 判定内核（`core.py`）不 import Redis/SQL/observability | 想单测阈值就得先起一套依赖栈 |
| SQL 只出现在 `stores.py` / `schema.py` / 迁移 | 换存储时要满仓库找 SQL |
| 指标名只在 `observability/metrics.py` 定义 | 同一个量两个出口，必然漂移 |
| `scope` 一定含角色 | 内部查询的答案被客户命中（越权） |
| key 的构造只在一处 | 两处构造 = 迟早不一致 = 跨用户命中 |
| 迁移与 schema 的维度/表名一致 | 「测试绿、生产红」 |

⚠️ 判据要写成**语法特征**（`text(f"`、`redis`、`select `），不能图省事写单词：
第 10 步为同一件事踩过——`"vector" in source` 会把函数名 `vector_only_search` 算成越界
（第一版返回了 11 条假阳性）。
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

CACHE_DIR = pathlib.Path("app/cache")


def source_of(module: str) -> str:
    return inspect.getsource(__import__(module, fromlist=["*"]))


def read(name: str) -> str:
    return (CACHE_DIR / name).read_text(encoding="utf-8")


def imports_of(source: str) -> set[str]:
    """一份源码里 `import` 的全部模块名（含 `from x import y` 的 `x`）。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


# ── 分层 ────────────────────────────────────────────────────────────────────


def test_core_does_not_import_the_transport_layers() -> None:
    """判定内核（`core.py`）不许 import Redis / SQLAlchemy / observability。

    它只吃「已经拿到手的候选」与「策略」—— 那正是「阈值可以被十来条纯逻辑用例
    逐点钉住」的前提（与第 12 步 `queue/core.py` 同一条纪律）。
    """
    banned = ("redis", "sqlalchemy", "psycopg", "fastapi", "app.observability", "app.db")
    found = imports_of(read("core.py"))
    offenders = [name for name in found if any(name.startswith(prefix) for prefix in banned)]
    assert offenders == [], f"core.py 不该 import {offenders}（判定内核必须离线可测）"


def test_core_does_not_import_the_store_implementations() -> None:
    """`core.py` 只依赖 `stores` 里的**数据类型**，不依赖具体实现。

    判据：它 import 了 `app.cache.stores` 是可以的（那是契约与数据形状），
    但源码里不出现 `MemoryEntryStore` / `PostgresEntryStore` / `RedisEntryStore`。
    """
    source = read("core.py")
    for name in ("MemoryEntryStore", "PostgresEntryStore", "RedisEntryStore", "RedisKeyValue"):
        assert name not in source, f"core.py 不该提到具体实现 {name}"


def test_cache_package_does_not_import_fastapi() -> None:
    """缓存包不许 import FastAPI：API 是接线处，反过来 import 就是分层倒了。"""
    for path in CACHE_DIR.glob("*.py"):
        found = imports_of(path.read_text(encoding="utf-8"))
        assert not any(name.startswith("fastapi") for name in found), path.name


def test_sql_only_lives_in_the_store_and_schema_modules() -> None:
    """SQL 只允许出现在三个地方：`stores.py`（条目 DML）、`corpus.py`（版本表单行）、
    `schema.py`（DDL）。其余模块（判定、门面、失效编排、结果缓存）一律不许碰 SQL。

    判据写成**SQL 语法特征**（`text(f"` / `select count` / `insert into` /
    `on conflict` / `delete from` / `update x set`），而不是「出现 `select` 这个词」——
    后者会把注释里的「selected」也算进去（第 10 步的假阳性就是这么来的）。

    ⚠️ `corpus.py` 为什么在允许名单里：`cache_corpus_versions` 是**单行版本表**，
    它的读写（`select corpus_revision` / `update ... returning`）就是「第三级失效」
    本身的实现。把它挪进 `stores.py` 会让那个文件同时管「条目」与「版本」两件事 ——
    而这两张表的生命周期完全不同（见 `schema.py` 的说明）。
    """
    allowed = {"stores.py", "schema.py", "corpus.py"}
    #: ⚠️ 模式里**不含**裸的 `delete from`（大小写不敏感）—— 那会把注释里的
    #: 「删行 vs 递增版本」那段话里的英文样例也算进去。只认真正的调用形态。
    pattern = re.compile(
        r"(text\(\s*\n?\s*f?\"|select\s+count\(|insert into \w|on conflict \(|"
        r"exec_driver_sql\(\s*f?\"|alter table \w)",
        re.IGNORECASE,
    )
    offenders: dict[str, list[str]] = {}
    for path in CACHE_DIR.glob("*.py"):
        if path.name in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        # 去掉注释行再判（注释里讨论 SQL 是允许的，也是鼓励的）
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        hits = pattern.findall(code)
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, f"SQL 出现在不该出现的模块里：{offenders}"


def test_redis_only_lives_in_the_backend_module() -> None:
    """`redis` 只允许出现在 `backend.py`（传输层）。

    ⚠️ 排除 `app.observability.metrics` 之类的**间接**提及：判据只看 `import redis`。
    """
    for path in CACHE_DIR.glob("*.py"):
        if path.name == "backend.py":
            continue
        found = imports_of(path.read_text(encoding="utf-8"))
        assert "redis" not in found, f"{path.name} 不该 import redis（传输层只有 backend.py）"


# ── 出口清单 ────────────────────────────────────────────────────────────────


def test_documented_interface_names_exist() -> None:
    """`13.cache.md` §2 的四个名字**必须**能从 `app.cache` 直接 import。"""
    import app.cache as cache

    for name in (
        "semantic_cache_get",
        "semantic_cache_set",
        "result_cache_get",
        "result_cache_set",
    ):
        assert hasattr(cache, name), f"文档 §2 点名的接口 {name} 不在门面上"
        assert name in cache.__all__


def test_facade_exports_everything_in_all() -> None:
    """`__all__` 里的每个名字都真的存在（这份清单是给读者看的目录）。"""
    import app.cache as cache

    missing = [name for name in cache.__all__ if not hasattr(cache, name)]
    assert missing == []


def test_facade_does_not_export_sql_or_redis_internals() -> None:
    """门面不该把「怎么连 Redis」「SQL 怎么写」暴露出去。"""
    import app.cache as cache

    for name in cache.__all__:
        assert "cursor" not in name.lower()
        assert "engine" not in name.lower()


def test_metric_names_are_defined_in_observability() -> None:
    """本模块用的指标名**全部**来自 `observability/metrics.py`（一个量一个写入方）。"""
    from app.observability import metrics as M

    used = {name for name in dir(M) if name.startswith("CACHE_")}
    assert {
        "CACHE_RESULTS",
        "CACHE_LOOKUP_DURATION",
        "CACHE_SAVED_CALLS",
        "CACHE_DEGRADED",
        "CACHE_ENTRIES",
        "CACHE_NEAR_MISS",
    } <= used
    source = read("semantic.py")
    # 六条指标名都以常量出现（不允许手写字符串字面量 —— 那样改名字会漏一处）
    for name in ("CACHE_RESULTS", "CACHE_LOOKUP_DURATION", "CACHE_SAVED_CALLS", "CACHE_NEAR_MISS"):
        assert f"M.{name}" in source, f"semantic.py 应当用 M.{name} 而不是手写指标名"
    assert "shopsage_cache" not in source, "指标名不许手写（必须引用 observability 的常量）"


def test_cache_span_attributes_are_whitelisted() -> None:
    """写进 span 的四个 `cache_*` 属性必须在白名单里（否则会被静默丢弃）。"""
    from app.observability.tracing import ALLOWED_ATTRIBUTES

    for key in ("cache_outcome", "cache_policy", "cache_hit", "cache_fingerprint"):
        assert key in ALLOWED_ATTRIBUTES


def test_span_never_carries_the_question_text() -> None:
    """**问题文本绝不进 span**（它可能含 PII，而 span 会出网）。"""
    source = read("semantic.py")
    assert "cache_question" not in source
    assert "set_attribute(\"question\"" not in source


# ── 单一真相 ────────────────────────────────────────────────────────────────


def test_scope_always_carries_the_role() -> None:
    """`scope_for` 必须带上角色 —— 少了它，内部查询的答案会被客户命中（越权）。"""
    from app.cache.stores import scope_for

    left = scope_for(7, "customer")
    right = scope_for(7, "internal_employee")
    assert left != right
    assert "customer" in left and "internal_employee" in right


def test_every_key_is_built_by_the_key_space() -> None:
    """Redis key 的构造只有一处：`CacheKeySpace`。

    判据：`backend.py` 之外不许出现手写的 `cache:` 前缀拼接。
    """
    for path in CACHE_DIR.glob("*.py"):
        if path.name == "backend.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert 'f"cache:' not in source, f"{path.name} 手写了 Redis key 前缀（应当走 CacheKeySpace）"


def test_settings_is_the_only_place_with_threshold_defaults() -> None:
    """阈值 / TTL 的默认值只在 `settings.py` 定义一次（两处默认值必然漂移）。"""
    from app.cache.settings import (
        DEFAULT_POLICY_TTL_SECONDS,
        DEFAULT_SIM_FLOOR,
        DEFAULT_SIM_THRESHOLD,
        DEFAULT_TTL_SECONDS,
    )

    assert 0 < DEFAULT_SIM_FLOOR <= DEFAULT_SIM_THRESHOLD < 1
    assert DEFAULT_POLICY_TTL_SECONDS > DEFAULT_TTL_SECONDS
    source = read("core.py")
    assert "0.93" not in source, "core.py 不该再写一遍阈值默认值（它属于 settings.py）"


def test_prompt_version_is_a_single_constant() -> None:
    """prompt 版本只有一个常量（它进指纹，写两处会让「改了 prompt 但缓存没失效」）。"""
    from app.cache.core import PROMPT_VERSION

    assert PROMPT_VERSION.startswith("p")
    assert read("core.py").count("PROMPT_VERSION: Final") == 1


def test_no_cache_rules_are_declared_once() -> None:
    """强时效规则只有一张表（`TIME_SENSITIVE_RULES`），`decide` 与 `is_time_sensitive` 共用它。"""
    source = read("core.py")
    assert source.count("TIME_SENSITIVE_RULES: Final") == 1
    assert "is_time_sensitive" in source
    # `is_time_sensitive` 走 `classify_cache_intent`（一条实现），不另写一遍正则
    from app.cache.core import classify_cache_intent, is_time_sensitive

    assert is_time_sensitive("我的订单到哪了") == (classify_cache_intent("我的订单到哪了")[0] == "time_sensitive")


# ── 迁移与 schema 对账 ──────────────────────────────────────────────────────


def _migration_source() -> str:
    versions = sorted(pathlib.Path("alembic/versions").glob("*_add_cache_tables.py"))
    assert versions, "找不到第 13 步的迁移文件"
    return versions[-1].read_text(encoding="utf-8")


def test_migration_does_not_import_app_code() -> None:
    """迁移不许 import 本仓代码（否则 `alembic history` 会拉起整条依赖链）。"""
    found = imports_of(_migration_source())
    assert not any(name.startswith("app.") for name in found), found


def test_migration_vector_dim_matches_schema() -> None:
    from app.cache.schema import VECTOR_DIM

    assert f"VECTOR_DIM = {VECTOR_DIM}" in _migration_source()


def test_migration_table_names_match_schema() -> None:
    from app.cache.schema import ENTRIES_TABLE, VERSIONS_TABLE

    source = _migration_source()
    assert f'ENTRIES_TABLE = "{ENTRIES_TABLE}"' in source
    assert f'VERSIONS_TABLE = "{VERSIONS_TABLE}"' in source


def test_migration_downgrade_does_not_drop_the_extension() -> None:
    """回滚**只删缓存表**：`vector` 扩展是集群级对象，`rag_chunks` 也在用它。"""
    source = _migration_source()
    downgrade = source.split("DOWNGRADE_DDL = [", 1)[1].split("]", 1)[0]
    assert "drop table" in downgrade
    assert "drop extension" not in downgrade


def test_schema_ddl_is_idempotent_by_construction() -> None:
    """每条 DDL 都是幂等的（`if not exists` / `on conflict do nothing`）。"""
    from app.cache.schema import create_statements

    for index, statement in enumerate(create_statements(1024)):
        lowered = statement.lower().strip()
        assert (
            "if not exists" in lowered or "on conflict do nothing" in lowered
        ), f"第 {index} 条 DDL 不幂等：{lowered[:80]}"


def test_create_statements_reject_a_missing_dimension() -> None:
    """维度缺失时**建表前就报错**（`vector(0)` 合法，会静默建出一张空向量的表）。"""
    from app.cache.schema import create_statements

    with pytest.raises(ValueError, match="维度"):
        create_statements(0)


def test_schema_rejects_bad_table_names() -> None:
    from app.cache.schema import create_statements

    with pytest.raises(ValueError):
        create_statements(1024, entries_table="bad name; drop table x")


def test_cache_tables_are_excluded_from_autogenerate() -> None:
    """两张缓存表必须排除在 alembic autogenerate 之外（否则会生成 `drop_table`）。"""
    from app.cache.schema import CACHE_TABLES
    from app.db.excluded import EXCLUDED_TABLES

    for table in CACHE_TABLES:
        assert table in EXCLUDED_TABLES


def test_cache_tables_are_infra_not_business() -> None:
    """缓存表属于**基础设施表**（`db_check.INFRA_TABLES`），不进业务表计数。"""
    from app.cache.schema import CACHE_TABLES
    from scripts.db_check import BUSINESS_TABLES, INFRA_TABLES

    for table in CACHE_TABLES:
        assert table in INFRA_TABLES
        assert table not in BUSINESS_TABLES


def test_readonly_role_清单_includes_the_cache_tables() -> None:
    """只读角色的授权清单里必须有这两张表（否则缓存读路径全线 `permission denied`）。"""
    from app.cache.schema import CACHE_TABLES
    from scripts.db_readonly import CACHE_TABLES as SCRIPT_TABLES

    assert set(SCRIPT_TABLES) == set(CACHE_TABLES)


# ── 与图 / RAG 的接线 ──────────────────────────────────────────────────────


def test_graph_runtime_has_a_cache_injection_point() -> None:
    from app.graph.config import GraphRuntime

    runtime = GraphRuntime()
    assert runtime.cache is None, "默认不注入缓存（第 10 步的行为必须逐行不变）"
    marker = object()
    from dataclasses import replace

    assert replace(runtime, cache=marker).cache is marker


def test_rag_node_checks_the_cache_before_retrieving() -> None:
    """缓存闸必须在**检索之前**（`13.cache.md` §5 最后一条的落点）。

    判据用源码顺序：`cache.semantic_get` 出现在 `retrieve_docs(` 之前 ——
    顺序反了的话「命中就整条链路都不走」这句话就不成立了（检索已经发生了）。
    """
    source = inspect.getsource(__import__("app.agents.rag", fromlist=["*"]))
    assert source.index("cache.semantic_get") < source.index("docs = retrieve_docs(")


def test_synthesis_uses_the_cached_answer_before_calling_the_model() -> None:
    """`cached_answer` 非空时必须**在 `generate_json` 之前返回**（省下的才是那上千 token）。"""
    source = inspect.getsource(__import__("app.agents.synthesis", fromlist=["*"]))
    assert source.index('state.get("cached_answer")') < source.index("await generate_json(")


def test_ingest_calls_the_change_callback_only_after_a_real_change() -> None:
    """`app/rag/ingest.py` 只在 `report.doc_changed` 时调回调（否则同步一次白清一次缓存）。"""
    source = inspect.getsource(__import__("app.rag.ingest", fromlist=["*"]))
    assert "on_change is not None and report.doc_changed" in source


def test_state_has_the_cache_fields() -> None:
    """state 里有 `cached_answer` 与 `cache_info`（没有它们，「这一轮用了缓存」不可断言）。"""
    from app.agents.state import GraphState

    annotations = GraphState.__annotations__
    assert "cached_answer" in annotations
    assert "cache_info" in annotations


def test_health_has_a_cache_component() -> None:
    """`/health` 里有一个 cache 组件（降级必须可见）。"""
    source = inspect.getsource(__import__("app.api.health", fromlist=["*"]))
    assert "cache_component" in source
    assert "cache_component(request)" in source
