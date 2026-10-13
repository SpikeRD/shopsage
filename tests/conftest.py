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

from tests.api.conftest import (  # noqa: F401 - 这个 import 是**有意的**，见下
    app,
    app_factory,
    client,
    customer_token,
    internal_token,
    metrics,
)
DB_MARKER = "db"


@pytest.fixture
def queue():
    """第 12 步的内存队列（**放在根 conftest**，理由见下面那段注释）。

    `app_factory` 默认就注入它，于是 `POST /api/tasks` 的契约能离线验完 ——
    与「默认注入假模型」是同一条纪律：测试不该依赖（也不该影响）真实依赖栈。
    不注入的话 `create_app` 会去连 `REDIS_URL`（本机的 `.env` 里有一个真地址）。
    """
    from tests.queue.fake_queue import MemoryJobQueue

    return MemoryJobQueue()

# ⚠️ **为什么把 `tests/api/conftest.py` 的装置导进根 conftest**（第 11 步的取舍）：
#
# `tests/observability/` 的接线用例要「跑一轮真 app」（假模型 + 内存 checkpointer），
# 而 pytest 的装置可见性是**向下**的：子目录能用父目录的，父目录用不了子目录的。
# `tests/api/` 的装置写得很具体（`app_factory` 默认注入假模型、小额度限流、
# `patched_query`），正是那些用例需要的东西。
#
# 三个选项与取舍：
#   ① 把装置复制一份到 `tests/observability/conftest.py` —— **不可选**：
#      两份装置必然漂移（`DATA_REPLIES` 改一处忘一处，症状是「只有某一批用例
#      拿着旧回执」，而它们看起来都在正常跑）；
#   ② 搬到根 conftest 并让 `tests/api/` 也用它 —— 改动面大（第 9 步的 138 条用例
#      都依赖那些装置的名字与默认值），而本步不该动第 9 步的验收基线；
#   ③ **在这里 re-export**（本实现）—— 装置只有一处实现，`tests/api/` 完全不受影响，
#      代价是根 conftest 多了一个「看着像越界」的 import，所以这段注释必须写清楚。
#
# 另一条纪律（第 9 步就定下的）也在这里被遵守：**只有用到 `TestClient` 的目录才付
# 「读 .env、可能连 Redis」的代价**。上面这几个装置本身是惰性的（fixture 不调用就不执行），
# 所以根 conftest 只是「登记了名字」，并不会让 700+ 条既有用例多出任何副作用。


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        f"{DB_MARKER}: 需要真实 PostgreSQL 的用例（连不上会自动跳过；用 -m 'not db' 排除）",
    )
    config.addinivalue_line(
        "markers",
        "rag_live: 需要真实 embedding/rerank 模型（~20s 加载）的端到端用例；依赖缺失会自动跳过",
    )
    config.addinivalue_line(
        "markers",
        "llm_live: 需要真实 LLM API key 的探活用例（花钱；CI 与日常用 `-m 'not llm_live'` 排除）",
    )
    config.addinivalue_line(
        "markers",
        "mcp_live: 需要真库/真检索的 MCP 端到端用例（默认跳过，用 --mcp-live 开门）",
    )


def pytest_addoption(parser) -> None:
    """`--rag-live`（第 10 步）：显式打开真模型用例。

    ⚠️ 必须注册在**根 conftest** 里，不能只写在 `tests/rag/conftest.py` 里（本仓踩过）：
    pytest 报 `error: unrecognized arguments: --rag-live` —— 因为根 conftest 的加载发生在
    参数解析期间，而子目录 conftest 要到「收集该目录的用例」时才加载。
    于是 `uv run pytest -q -m "not llm_live" --rag-live`（全量 + 真模型）这条命令
    会直接启动失败，而 `uv run pytest -q tests/rag --rag-live`（只跑子目录）却是好的 ——
    这种「只有某种用法可用」的开关最容易被误认为「参数没生效」。

    `--mcp-live`（第 15 步）同理：它必须在**这一层**注册，理由与上面完全一样。
    """
    group = parser.getgroup("rag")
    group.addoption(
        "--rag-live",
        action="store_true",
        default=False,
        help="跑需要真实 embedding/rerank 模型的用例（~20s 加载 + 真算力）",
    )
    parser.getgroup("mcp").addoption(
        "--mcp-live",
        action="store_true",
        default=False,
        help="跑 MCP 的真依赖端到端用例（真 PostgreSQL + 真检索 + 真写工单）",
    )


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """防御性装置：把被**库代码**关停的 logger 打开。

    背景（本仓实测）：`alembic/env.py` 会执行 `logging.config.fileConfig()`，
    且它的默认行为是 `disable_existing_loggers=True` —— 一次**全局**日志重配置。
    `tests/test_db_migrations.py` 在 import 期触发它，于是它之后的用例里 `caplog` 收不到日志：
    典型症状是「断言日志的用例单独跑绿、全量跑红」，非常难查。

    根因已修（`alembic/env.py` 显式传 `disable_existing_loggers=False`），
    这里再兜一道：**任何**库/工具关停日志都不该让别的用例静默失效。
    `disabled` 是每个 logger 上的显式开关（与 level 无关），清掉它不会改变日志级别策略。
    """
    import logging

    for name in list(logging.root.manager.loggerDict):
        logger = logging.getLogger(name)
        if isinstance(logger, logging.Logger) and logger.disabled:
            logger.disabled = False
    logging.getLogger().disabled = False
    yield


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
    """需要种子数据的用例声明它：行数不达标就现场补灌（幂等，可安全重复跑）。

    ⚠️ 只按 `MIN_COUNTS`（= 8 张业务表）判断，**不看基础设施表**
    （第 12 步的 `queue_dead_letters`）：`snapshot()` 也会数它，但「库里没有死信」
    是**健康**形态，不是缺数据。第一版写成遍历 `snap.counts` 去找 `MIN_COUNTS[t]`，
    于是新增那张表之后本装置直接 `KeyError: 'queue_dead_letters'` ——
    而它的表现是「所有依赖 `seeded` 的用例在 setup 阶段成片 ERROR」
    （50 个 error，看起来像工具层坏了，见 runbook §10.1）。
    """
    from scripts.db_check import MIN_COUNTS, snapshot
    from scripts.seed import seed

    snap = snapshot()
    below = {t: n for t, n in snap.counts.items() if t in MIN_COUNTS and n < MIN_COUNTS[t]}
    missing_business = [t for t in MIN_COUNTS if t not in snap.tables]
    if below or missing_business:
        seed()
