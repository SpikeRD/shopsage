"""Alembic 运行环境。

三处关键点（照抄别人的 env.py 最容易漏的就是这三条）：
1. **必须 `import app.db.models`**：`target_metadata` 只认「已经定义过的表」，
   不 import 模型模块时 metadata 是空的 → `--autogenerate` 静默生成**空迁移**。
2. **连接串从 `Settings` 注入**，不写在 `alembic.ini`：
   写两遍必然漂移（.env 改了 ini 没改），而且 ini 里的密码容易误提交。
3. **池用 NullPool**：迁移是一次性短连接，用池只会白占连接。

   想临时指定库（例如在 Linux 上跑迁移）：`alembic -x db_url=postgresql://... upgrade head`
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings

# 这一行是「autogenerate 能不能看见表」的开关（见模块 docstring 第 1 条）
import app.db.models  # noqa: F401  （仅为把模型注册进 Base.metadata）
from app.db.base import Base
from app.db.urls import to_sqlalchemy_url

def _process_revision_directives(context, revision, directives) -> None:  # noqa: ANN001
    """两道护栏，防止「静默生成一个没用的迁移」。

    ① **空迁移**：`--autogenerate` 在「库已经跟模型一致」时会生成一个 upgrade/downgrade
       都是 `pass` 的文件。无害但会污染迁移历史（评审要逐条看），直接拦掉并说明原因。
    ② **库不是 head**：数据库停在旧版本时，autogenerate 会拿**旧结构**跟新模型比，
       生成一堆错误 diff（甚至重复建表）。这时应该先 `alembic upgrade head`。

    显式绕过：`alembic -x allow_empty=true revision --autogenerate -m "..."`
    """
    # ⚠️ 这里的 `context` 是 MigrationContext（不是模块级那个 EnvironmentContext）：
    # 所以只能用 `get_current_heads()` / `.script`，没有 `get_x_argument()`/`get_context()`。
    # 这两个 API 的名字很像，混用就会得到 AttributeError。
    head = context.script.get_current_head()
    current_heads = context.get_current_heads()
    if current_heads and head not in current_heads:
        raise SystemExit(
            f"目标数据库不在 head（当前 {current_heads}，head 是 {head}）："
            "先执行 `alembic upgrade head`，再生成新的迁移。"
        )
    if X_ARGS.get("allow_empty") == "true":
        return
    for directive in directives:
        if directive.upgrade_ops.is_empty() and directive.downgrade_ops.is_empty():
            directives[:] = []  # 丢弃这个空迁移
            raise SystemExit(
                "没有检测到模型与数据库的差异 —— 不生成空迁移。"
                "若确实需要一个空迁移，请加 `-x allow_empty=true`。"
            )


config = context.config

if config.config_file_name is not None:
    # ⚠️ `disable_existing_loggers=False` 是必须的（踩过）：
    # fileConfig() 默认 **True**，会把「此刻已存在的所有 logger」强制 `disabled=True`，
    # 并把 root logger 的 handler 换成 ini 里的 console handler ——
    # 也就是说它是一次**全局日志重配置**，不只影响 alembic。
    # 实测到的后果（本仓）：`tests/test_db_migrations.py` 在 import 期就 import 了本模块，
    # 于是该文件之后的所有用例里 `caplog` 再也收不到任何日志，
    # `tests/test_llm_client_retry.py` 那些「断言退避日志」的用例在**全量跑时红、单独跑时绿**。
    # 关掉它之后：alembic/sqlalchemy 的日志照旧按 ini 生效（我们只关心这两族），
    # 而其它 logger（app.*、pytest 的 caplog 等）不再被无辜关停。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# `-x key=value` 的命令行参数：只有 env 这一层的 context（EnvironmentContext）能读，
# MigrationContext 没有这个 API —— 用它来做「护栏的显式绕过开关」。
X_ARGS = context.get_x_argument(as_dictionary=True)

# 迁移 URL：`-x db_url=...` > 环境变量 > .env（get_settings 的优先级本来就是这样）。
# 刻意不 import app.db.session —— 那个模块在 import 期就会建连接池。
DB_URL = to_sqlalchemy_url(
    context.get_x_argument(as_dictionary=True).get("db_url") or get_settings().database_url
)


def _configure(connection=None, url: str | None = None, **extra: object) -> None:
    """统一的 context.configure：离线/在线两条路共用，选项不会漂移。

    ⚠️ `literal_binds` / `dialect_opts` **只能在离线模式**给：在线模式（含
    `--autogenerate`）下 Alembic 会直接报
    `Can't use literal_binds setting without as_sql mode`。
    """
    options: dict[str, object] = {
        "target_metadata": target_metadata,
        # 类型变更（如 VARCHAR(16) → VARCHAR(32)）也纳入 diff，否则改列类型要手写迁移
        "compare_type": True,
        # 服务端默认值变更也纳入 diff；PG 会重写 now() 这类表达式，
        # 若 diff 噪音过大（反复提示 now() 有变化），把这项关掉即可。
        "compare_server_default": True,
        "include_schemas": False,
        # 迁移脚本里按 Base.metadata 的命名约定渲染约束名，与模型保持一致
        "render_as_batch": False,
        # 生成迁移时的护栏（空迁移 / 库不是 head），见 _process_revision_directives
        "process_revision_directives": _process_revision_directives,
    }
    if connection is None:  # 离线模式：把参数直接拼进 SQL 文本
        options["literal_binds"] = True
        options["dialect_opts"] = {"paramstyle": "named"}

    options.update(extra)  # 例如离线模式的 output_buffer（见 run_migrations_offline）
    context.configure(connection=connection, url=url, **options)


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 文本，不连库（`alembic upgrade head --sql`）。

    `output_buffer` 显式透传 `config.output_buffer`：
    MigrationContext 默认把 SQL 写到 `sys.stdout`，只有显式给了 output_buffer
    才会改写它 —— 这样程序化调用（比如测试里捕获离线 SQL）不必去 monkeypatch stdout。
    """
    options: dict[str, object] = {}
    if config.output_buffer is not None:
        options["output_buffer"] = config.output_buffer
    _configure(url=DB_URL, **options)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：真的连库执行。"""
    connectable = create_engine(DB_URL, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()