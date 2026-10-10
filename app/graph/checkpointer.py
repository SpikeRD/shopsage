"""checkpointer：**把节点状态落盘，让「断点重放」成立**（`8.graph.md` §4 第 2 条、A2）。

三种后端，取舍如下（都在本机实测过，见 runbook §4 与 §12）：

| backend | 进程重启后还在？ | 多副本共享？ | 本机可用性 | 用途 |
|---|---|---|---|---|
| `memory` | ❌ | ❌ | ✅ 总是可用 | 单测、演示、图结构验证（默认） |
| `postgres` | ✅ | ✅ | ✅ 已验证（`shopsage` 库） | **生产口径**：状态进业务库，和审计同一处 |
| `redis` | ✅（可配 TTL） | ✅ | ⚠️ **需要 RedisJSON + RediSearch 模块** | 低延迟会话态（本机 dev 的 redis 镜像没有这两个模块，见 §10.1 坑 5） |

三个刻意的决定：

1. **默认 `memory`，不默认 Postgres**。默认值会被复制到所有调用方（含测试与脚本），
   一个「默默连生产库」的默认值是事故的开始。要持久化就显式写 `backend="postgres"`，
   而这个字符串出现在调用处，review 时看得见。
2. **`memory` 不是「假后端」**：`InMemorySaver` 与两个外部后端**同一个接口**，
   所以「断点重放」「thread 隔离」「幂等」这些性质在单测里就能验，
   不必等依赖栈起来（这正是第 7 步「离线可测」在同一步的延续）。
3. **能力探测放在前面**（`_require_redis_modules`）：Redis checkpointer 依赖
   RediSearch/RedisJSON，缺模块时 `from_conn_string` 会在**进入上下文时就抛**
   （实测：`RedisSearchError: unknown command 'FT.INFO'`），而且是在 `__aenter__` 里抛，
   想用 `except` 包住调用点根本包不住。提前探测让报错落在**一句能照做的话**上。

## Windows 上的那个坑（实测，必须知道）

psycopg 的异步模式**不能用 Windows 默认的 `ProactorEventLoop`**：

```
psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode.
```

这不是本仓的缺陷，是 psycopg 的既定限制，而 Windows 上 `asyncio.run()` 默认就是 Proactor。
所以本模块提供 `ensure_compatible_event_loop()`：**图相关的异步代码在启动时调一次**，
之后 `asyncio.run` / pytest-asyncio 新建的循环都是 Selector 循环。
生产（Linux）上它是**空操作** —— 这不是「为 Windows 特判业务逻辑」，
而是「在入口处把运行环境的差异收敛成一个显式调用」（§10.1 坑 1 有完整复现）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

logger = logging.getLogger(__name__)

#: 后端取值域
CheckpointBackend = Literal["memory", "postgres", "redis"]

#: 三个后端的名字（供 CLI/文档/断言共用，避免各处写字符串）
BACKENDS: Final[tuple[str, ...]] = ("memory", "postgres", "redis")

#: 缺模块时 Redis 该装什么（报错里直接给命令，别让人去查文档）
REDIS_STACK_HINT: Final[str] = (
    "改用 `redis/redis-stack-server` 镜像（或 `redis/redis-stack`），"
    "它自带 RediSearch + RedisJSON；同时把 REDIS_URL 指向它"
)

#: LangGraph checkpointer 在 Postgres 里建的四张表（`AsyncPostgresSaver.setup()` 的产物）。
#:
#: 为什么要把它写成常量并对外暴露：这四张表**不属于业务模型**，但它们会进
#: `Base.metadata`（`langgraph-checkpoint-postgres` 自己把表定义注册进了 SQLAlchemy 的
#: 全局 metadata）。于是 `alembic revision --autogenerate` 与
#: `tests/test_db_migrations.py::test_no_pending_migration_against_models` 都会
#: 拿着「库里有、模型里没有」的差异要求**删掉这四张表** —— 那会把 checkpoint 数据一起删掉。
#: 实测症状：跑完图相关的真库用例之后，那条迁移一致性用例开始红（runbook §10.1 坑 12）。
#:
#: 处置方式：在**两处**显式排除它们 —— `alembic/env.py` 的 `include_object`（人工迁移）
#: 与本文件的 `CHECKPOINT_TABLES`（测试）。**不**把它们写进迁移：
#: 它们的结构由 `langgraph-checkpoint-postgres` 的版本决定，写进我们的迁移会让
#: 「升级 langgraph」变成「手写迁移」，而那正是 `saver.setup()` 存在的意义。
CHECKPOINT_TABLES: Final[frozenset[str]] = frozenset(
    {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
)


class CheckpointUnavailable(RuntimeError):
    """checkpointer 后端不可用（连不上库 / 缺模块 / 配置不全）。

    单独一个异常类型：调用方要能区分「后端没起来」（可用性问题，第 9 步该降级到 memory
    并告警）与「checkpoint 数据坏了」（一致性问题，必须停下来查）。
    """


def ensure_compatible_event_loop() -> str:
    """把 Windows 的事件循环策略换成 Selector（Linux 上是空操作）；返回当前策略名。

    什么时候必须调：**在创建任何事件循环之前**。因为策略是「新建循环时读一次」的全局量，
    循环已经建好之后再改，对已有循环无效 —— 而 psycopg 检查的正是**当前正在跑的循环**。
    """
    if sys.platform != "win32":
        return type(asyncio.get_event_loop_policy()).__name__
    if isinstance(asyncio.get_event_loop_policy(), asyncio.WindowsSelectorEventLoopPolicy):
        return "WindowsSelectorEventLoopPolicy"
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return "WindowsSelectorEventLoopPolicy"


@asynccontextmanager
async def open_checkpointer(
    backend: CheckpointBackend = "memory",
    *,
    conn_string: str | None = None,
    setup: bool = True,
) -> AsyncIterator[BaseCheckpointSaver]:
    """打开一个 checkpointer，**生命周期由调用方管**（`async with`）。

    为什么是上下文管理器而不是「建好返回来」：三个后端的连接都必须在关闭时释放，
    而「谁负责关」在返回式 API 里永远说不清（调用方忘了关 = 连接泄漏；
    库里替你关 = 你拿着一个已关闭的对象）。`async with` 把这个约定变成语法。

    `conn_string` 缺省时按后端从 `Settings` 取（`DATABASE_URL` / `REDIS_URL`）：
    连接串只有一处真相，且**不落进代码**（`.env` 里那份）。
    """
    ensure_compatible_event_loop()

    if backend == "memory":
        logger.info("checkpointer=memory（进程内；重启即丢，仅用于单测与演示）")
        yield InMemorySaver()
        return

    if backend == "postgres":
        url = conn_string or _postgres_url()
        async with _open_postgres(url, setup=setup) as saver:
            yield saver
        return

    if backend == "redis":
        url = conn_string or _redis_url()
        async with _open_redis(url, setup=setup) as saver:
            yield saver
        return

    raise CheckpointUnavailable(
        f"未知的 checkpointer 后端 {backend!r}：只能是 {', '.join(BACKENDS)} 之一"
    )


@asynccontextmanager
async def _open_postgres(url: str, *, setup: bool) -> AsyncIterator[BaseCheckpointSaver]:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    logger.info("checkpointer=postgres（状态落库，进程重启/多副本都不丢）")
    try:
        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            if setup:
                # 建表是一次幂等操作（`checkpoint_migrations` 表记版本）；
                # 生产由迁移负责，这里兜底是因为「第一次跑起来」不该需要额外命令。
                await saver.setup()
            yield saver
    except CheckpointUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一翻译成一句能照做的报错
        raise CheckpointUnavailable(
            f"Postgres checkpointer 打不开：{type(exc).__name__}: {exc}。"
            "检查 DATABASE_URL 是否可达（runbook §1.1），以及该账号是否有建表权限"
            "（checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations）"
        ) from exc


@asynccontextmanager
async def _open_redis(url: str, *, setup: bool) -> AsyncIterator[BaseCheckpointSaver]:
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    await _require_redis_modules(url)
    logger.info("checkpointer=redis（状态落 Redis，可配 TTL）")
    try:
        async with AsyncRedisSaver.from_conn_string(url) as saver:
            if setup:
                await saver.setup()
            yield saver
    except CheckpointUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CheckpointUnavailable(
            f"Redis checkpointer 打不开：{type(exc).__name__}: {exc}。"
            f"最常见的原因是镜像里没有 RediSearch/RedisJSON：{REDIS_STACK_HINT}"
        ) from exc


async def _require_redis_modules(url: str) -> None:
    """探测 Redis 的两个必需模块；缺了就直接给一句能照做的报错。

    ⚠️ 探测用 **MODULE LIST**（HELLO 的模块信息），不用 `FT.INFO`：
    `FT.INFO` 在不存在的索引上也会报「unknown command」，两者在报错里长得一样，
    会把「模块没装」误判成「索引没建」—— 而这两种情况的修法完全不同。
    """
    try:
        import redis.asyncio as aioredis
    except ImportError as exc:  # pragma: no cover - redis 已在依赖里
        raise CheckpointUnavailable(
            "缺少 redis 客户端库：`uv add redis`（langgraph-checkpoint-redis 的依赖）"
        ) from exc

    client = aioredis.from_url(url)
    try:
        # 逐条试探要比 MODULE LIST 稳：某些托管 Redis 禁用了 MODULE 命令，
        # 但真的支持 JSON/Search（于是 MODULE 拿不到、功能却可用）。
        missing = []
        for command in ("JSON.SET", "FT._LIST"):
            try:
                if command == "JSON.SET":
                    await client.execute_command("JSON.SET", "__shopsage_probe__", "$", '"1"')
                    await client.delete("__shopsage_probe__")
                else:
                    await client.execute_command("FT._LIST")
            except Exception as exc:  # noqa: BLE001 - 探测失败即「不可用」
                missing.append(f"{command}（{type(exc).__name__}）")
        if missing:
            raise CheckpointUnavailable(
                f"Redis 缺少 langgraph-checkpoint-redis 必需的模块：{', '.join(missing)}。"
                f"{REDIS_STACK_HINT}"
            )
    finally:
        await client.aclose()


def _postgres_url() -> str:
    from app.config import get_settings

    url = get_settings().database_url
    if not url:
        raise CheckpointUnavailable(
            "DATABASE_URL 为空：Postgres checkpointer 需要连接串（.env 里配置，见 3.db-runbook.md §1）"
        )
    return url


def _redis_url() -> str:
    from app.config import get_settings

    url = get_settings().redis_url
    if not url:
        raise CheckpointUnavailable(
            "REDIS_URL 为空：Redis checkpointer 需要连接串（.env 里配置，见 2.infra-runbook.md）"
        )
    return url


def thread_config(thread_id: str, **extra: Any) -> dict[str, Any]:
    """造 `config={"configurable": {"thread_id": ...}}`（`8.graph.md` §4 第 3 条）。

    单独一个函数是为了让「`thread_id` 从哪来」只有一处答案：
    **它就是会话 id**，由第 9 步的鉴权结果提供（`user_id + 会话` 派生），
    绝不由模型或请求体里的任意字符串决定 —— 否则任何人都能读写别人的 checkpoint。
    """
    # ⚠️ 先拦**非字符串**再 strip：`str(None)` 是 `"None"`（四个字符、非空），
    # 于是「忘了传 thread_id」会静默变成一个叫 `None` 的会话 ——
    # 所有忘记传的请求都会共用这一份 checkpoint，而症状是「不同用户的回答互相串」。
    if not isinstance(thread_id, str):
        raise ValueError(
            f"thread_id 必须是字符串（收到 {type(thread_id).__name__}）："
            "它是 checkpoint 的隔离键（= 会话 id），由入口的鉴权结果提供"
        )
    if not thread_id.strip():
        raise ValueError(
            "thread_id 不能为空：它是 checkpoint 的隔离键（= 会话 id），"
            "空值会让所有会话共用一份状态"
        )
    configurable = {"thread_id": thread_id.strip()}
    configurable.update(extra)
    return {"configurable": configurable}


__all__ = [
    "BACKENDS",
    "CHECKPOINT_TABLES",
    "REDIS_STACK_HINT",
    "CheckpointBackend",
    "CheckpointUnavailable",
    "ensure_compatible_event_loop",
    "open_checkpointer",
    "thread_config",
]
