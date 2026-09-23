"""pytest 全局装置。

做三件事：

1. 注册 `db` 标记（需要真库的用例）。
2. **`db` 标记的用例在依赖不可用时自动跳过**，并说明原因 —— 本机没有 Docker，
   依赖栈在 VMware-Linux 上，连不上时整批 `db` 用例应当 skip 而不是红：
   `uv run pytest -q` 在没起依赖时也应该是「绿 + 若干 skip」。
3. 提供 `seeded` 装置：需要种子数据的用例声明它，数据不达标就现场补灌（幂等）。

依赖不可用时只跑纯逻辑用例：`uv run pytest -q -m "not db"`。

注意：`tests/test_config.py` 里那 6 条用 `tmp_path` 的用例，在受限环境（沙箱）里会在
setup 阶段报 `PermissionError` —— 那是环境限制，不是代码问题（见 `1.config-guide.md` §3.5）。
"""

from __future__ import annotations

import pytest

DB_MARKER = "db"


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        f"{DB_MARKER}: 需要真实 PostgreSQL 的用例（连不上会自动跳过；用 -m 'not db' 排除）",
    )


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """每条用例前后清掉 `get_settings()` 的 lru_cache。

    必要性：`get_settings()` 是进程内单例，而 db 模块（`app.db.session` 等）在 **import 期**
    就调用它建连接池。于是「哪个测试文件先被收集」会决定单例里装的是哪份配置 ——
    用 `monkeypatch.setenv` 改配置的用例（如 `test_prod_requires_llm_key`）就会变成
    「顺序依赖」：跑全量时绿、单独跑时红，或者反过来。

    这里统一在用例边界清缓存，让每条用例都从「干净的单例」出发，与收集顺序无关。
    """
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def pytest_collection_modifyitems(config, items) -> None:
    """把「依赖不可用」变成 skip：只对打了 `db` 标记的用例生效。"""
    needs_db = [item for item in items if item.get_closest_marker(DB_MARKER)]
    if not needs_db:
        return

    from tests.db_helpers import database_available, tables_exist

    if not database_available():
        reason = (
            "PostgreSQL 不可达：检查 .env 的 DATABASE_URL host"
            "（本机无 Docker，要填 Linux VM 的 IP；见 docs/runbook/3.db-runbook.md §1）"
        )
    elif not tables_exist():
        reason = "业务表不存在：先跑 `uv run alembic upgrade head`"
    else:
        return

    skip = pytest.mark.skip(reason=reason)
    for item in needs_db:
        item.add_marker(skip)


@pytest.fixture(scope="session")
def seeded() -> None:
    """需要种子数据的用例声明它：行数不达标就现场补灌（幂等，可安全重复跑）。"""
    from scripts.db_check import BUSINESS_TABLES, MIN_COUNTS, snapshot
    from scripts.seed import seed

    snap = snapshot()
    below = {t: n for t, n in snap.counts.items() if n < MIN_COUNTS[t]}
    if below or len(snap.counts) < len(BUSINESS_TABLES):
        seed()
