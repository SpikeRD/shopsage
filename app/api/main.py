"""ASGI 应用的组装：中间件 + 路由 + 生命周期（`9.api.md` §6 第 4 步）。

```powershell
uv run uvicorn app.main:app --reload --port 8000       # 开发
uv run uvicorn app.main:app --workers 4 --port 8000    # 多副本（无状态，可直接加）
```

## 生命周期里做三件事，顺序不能换

1. **`ensure_compatible_event_loop()`** —— 必须在**任何事件循环被创建之前**调
   （Windows 上 psycopg 的异步模式不能在 `ProactorEventLoop` 上跑，见 `8.graph-runbook.md` §10.1 坑 1）。
   ⚠️ 这里的 `lifespan` 已经在事件循环里了 —— 所以 `create_app()` 在**模块 import 期**
   就调了它一次（`app = create_app()` 在 import 时执行，那时还没有循环），
   `lifespan` 里再调一次是幂等的兜底（Linux 上是空操作）。
2. **`auth.ensure_usable()`** —— prod 缺 `JWT_SECRET` 时**启动即失败**，而不是等第一个请求进来才 401。
3. **装配依赖**（限流器、指标、图工厂）并挂到 `app.state`。

## 无状态：多副本可以直接加

`--workers 4` 之所以能用，是因为进程里只有两类状态，且都不影响正确性：

| 状态 | 多副本下的影响 |
|---|---|
| `MetricsRegistry`（计数） | 每个副本各报一份 —— Prometheus 抓每个副本后 `sum()` 即可（标准做法） |
| `MemoryRateLimitStore`（限流计数） | 额度被放大到「副本数 × limit」—— **所以生产要 Redis**（`ratelimit.open_rate_limiter` 会告警） |

会话状态（对话历史、断点）全在 checkpointer 里（Postgres/Redis），
**不在**进程内 —— 这正是架构第 8 节「无状态化」的落地方式。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.api import config as api_config
from app.api import deps
from app.api.chat import router as chat_router
from app.api.errors import install_exception_handlers
from app.api.health import router as infra_router
from app.api.metrics import MetricsRegistry
from app.api.middleware import ApiMiddleware
from app.api.ratelimit import open_rate_limiter
from app.api.scope import SCOPE_BY_ROUTE
from app.graph import GraphRuntime, compile_graph, ensure_compatible_event_loop, open_checkpointer
from app.graph.checkpointer import CheckpointBackend
from app.graph.config import GATED_ROLES, SENSITIVE_ROUTES
from app.observability import setup_observability
from app.observability.langfuse_cb import langfuse_handler
from app.observability.tracing import reset_request_root, set_request_root

logger = logging.getLogger(__name__)

#: 服务名（日志与 `/health` 都用它，避免各处写字符串）
SERVICE_NAME = "shopsage-api"

#: 探活超时的默认值（可被 `API_PROBE_TIMEOUT` 覆盖）
DEFAULT_PROBE_TIMEOUT = 0.5


def _default_checkpointer_factory(backend: CheckpointBackend) -> Callable[[], Any]:
    """默认的 checkpointer 工厂：一次调用返回一个 `async with` 上下文管理器。

    `create_app(checkpointer_factory=...)` 是**注入点**（不是「测试后门」）：
    离线跑图要内存 saver、真库跑图要 Postgres saver —— 这是**运行环境的选择**，
    应该由装配方显式决定，而不是让生产代码去猜「现在是测试吗」。
    """

    def factory() -> Any:
        return open_checkpointer(backend)

    return factory


def make_graph_factory(checkpointer: Any) -> Callable[[GraphRuntime, deps.Actor], Any]:
    """造「每请求一张图」的工厂（闭包持有 checkpointer 的生命周期）。

    为什么每请求新建：`compile_graph()` 很轻（建节点与边），而共享一个编译好的图
    会逼着我们回答「多个请求的 config 会不会互相污染」这种不该存在的问题。
    真正的状态在 checkpointer 里（`thread_id` 隔离），图对象本身**无状态**。

    ⚠️ 参数 `actor` **刻意不用**：闸门策略（对客开、内部关）已经由调用方
    （`app/api/chat.py::_runtime_with_policy`）算好并注入到 `runtime` 里了。
    图不知道 HTTP、不知道 JWT，它只知道「这次跑图用什么依赖、有没有闸门」——
    这一层边界是 `9.api.md` §4 第 4 条「API 层无业务逻辑」的另一面。
    """

    def factory(runtime: GraphRuntime, actor: deps.Actor) -> Any:
        del actor  # 见上面的说明
        return compile_graph(checkpointer, runtime)

    return factory


def _request_id_middleware() -> type:
    """请求 id 兜底中间件：`ApiMiddleware` 已经把 id 放进 `scope["state"]`，
    这里只是保证「即使有人绕过 `ApiMiddleware` 直接挂路由」时 `request.state.request_id` 仍存在。

    纯 ASGI 实现（20 行），刻意不用 `BaseHTTPMiddleware`（理由见 `middleware` 模块 docstring）。
    """

    class _RequestIdFallback:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope.get("type") in ("http", "websocket"):
                state = scope.setdefault("state", {})
                state.setdefault("request_id", "")
            await self.app(scope, receive, send)

    return _RequestIdFallback


def create_app(
    *,
    settings: Any = None,
    backend: CheckpointBackend = "memory",
    checkpointer_factory: Callable[[], Any] | None = None,
    graph_factory: Callable[[GraphRuntime, deps.Actor], Any] | None = None,
    runtime: GraphRuntime | None = None,
    rate_limiter: Any = None,
    metrics: Any = None,
    telemetry: Any = None,
    probe_timeout_value: float | None = None,
) -> FastAPI:
    """装配 FastAPI 应用（**所有外部依赖都可注入** —— 这是离线可测的前提）。

    参数说明（默认值就是生产口径）：

    | 参数 | 默认 | 什么时候要改 |
    |---|---|---|
    | `backend` | `memory` | 生产要用 `postgres`（从 `CHECKPOINT_BACKEND` 读） |
    | `checkpointer_factory` | 按 `backend` 开 | 测试注入内存 saver，避免真连库 |
    | `graph_factory` | 每请求编译一张图 | 测试注入固定图 |
    | `runtime` | `GraphRuntime()` | 测试注入假模型（`llm=` / `search=`） |
    | `rate_limiter` | 按 `REDIS_URL` 自动选 | 测试注入小额度来验 429 |
    | `metrics` | 新 `MetricsRegistry` | 测试注入共享注册表来断言指标 |
    | `telemetry` | 按 `settings` 新建 | 测试注入共享 telemetry 来断言 span（第 11 步） |

    ⚠️ **不把 `llm`/`search` 做成环境变量**：它们是「本次跑图用什么依赖」的语义选择
    （评测要注入假模型、对客要强制闸门），塞进全局配置会让「测试环境」与「生产环境」的行为
    靠环境变量对齐 —— `8.graph.md` §5 已经为同一件事给过理由。
    """
    ensure_compatible_event_loop()  # 必须在建事件循环之前（import 期执行）

    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    chosen_backend: CheckpointBackend = backend
    factory = checkpointer_factory or _default_checkpointer_factory(chosen_backend)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from app.security.auth import ensure_usable, is_dev_secret

        ensure_compatible_event_loop()
        ensure_usable()  # prod 缺 JWT_SECRET → 这里就失败（而不是第一个请求时 401）

        app.state.settings = settings
        app.state.metrics = metrics if metrics is not None else MetricsRegistry()
        app.state.graph_runtime = runtime if runtime is not None else GraphRuntime()
        app.state.probe_timeout = (
            probe_timeout_value if probe_timeout_value is not None else api_config.probe_timeout(settings)
        )
        app.state.rate_limiter = (
            rate_limiter
            if rate_limiter is not None
            else open_rate_limiter(redis_url=str(getattr(settings, "redis_url", "") or ""), rules=api_config.rate_limit_rules(settings))
        )

        # ── 第 11 步：可观测性在**这里**装配（一次），而不是 import 期或每请求 ──
        # 三件事同时被这一行定死：
        #   ① trace 的归属（`TracerProvider` 是进程级对象，按 app 建一份）；
        #   ② 指标进哪个 registry（把 app 的注册表交给它 → `/metrics` 一次抓全）；
        #   ③ Langfuse client 的构造（没配密钥就完全不建，理由见 langfuse_cb 模块 docstring）。
        # 放在 lifespan 而不是 import 期的硬理由：`create_app()` 在**模块 import 期**执行
        # （`app = create_app()`），那时建 TracerProvider/读配置会让「import 一个模块」
        # 变成「起一套监控」，而测试收集期的 import 就会连带发生这件事。
        obs = telemetry if telemetry is not None else setup_observability(settings, metrics=app.state.metrics)
        app.state.telemetry = obs
        telemetry_token = set_request_root(obs)

        masked = getattr(settings, "dump_for_logging", lambda: {})()
        logger.info(
            "API 启动：env=%s prefix=%r backend=%s 限流=%s 调试载荷=%s 开发密钥=%s",
            getattr(settings, "env", "dev"),
            api_config.api_prefix(settings),
            chosen_backend,
            app.state.rate_limiter.backend_name(),
            api_config.debug_payload_enabled(settings),
            is_dev_secret(),
        )
        logger.info(
            "可观测性就绪：enabled=%s endpoint=%r langfuse=%s",
            obs.enabled,
            obs.otel_endpoint or "(未配，只留内存 span)",
            ("开" if langfuse_handler().enabled else f"关（{langfuse_handler().disabled_reason}）"),
        )
        logger.debug("生效配置（已掩码）：%s", masked)

        try:
            async with factory() as checkpointer:
                app.state.graph_factory = graph_factory or make_graph_factory(checkpointer)
                logger.info(
                    "图已就绪：checkpointer=%s（thread_id 的隔离键由入口的鉴权结果派生）",
                    type(checkpointer).__name__,
                )
                yield
        finally:
            # ⚠️ `finally` 而不是平铺在 `yield` 之后：启动中途抛错时（例如 checkpointer
            #    连不上）也要把 span 与 telemetry 根收干净 —— 否则「启动失败」会留下一份
            #    活跃的 telemetry 根，让下一个 app（测试里很常见）的埋点写进上一个人的注册表。
            obs.flush()  # 把还在缓冲里的 span 推出去（关服前的最后一件事）
            langfuse_handler().flush()
            reset_request_root(telemetry_token)
            logger.info("API 关闭：checkpointer 已释放，telemetry 已 flush")

    app = FastAPI(
        title="ShopSage API",
        version="0.1.0",
        description=(
            "电商企业智能助手（第 9 步：HTTP → 图 → Text-to-SQL → 回答）。"
            "鉴权：`Authorization: Bearer <JWT>`；流式：`WS /api/chat/stream`"
            "（子协议 `shopsage.v1, bearer.<token>`）。"
        ),
        lifespan=lifespan,
    )

    # ── 中间件（**先加的内层、后加的外层**：FastAPI 的 add_middleware 是往栈顶插）──
    app.add_middleware(_request_id_middleware())
    # ⚠️ 不给 `limiter=`：限流器在 `lifespan` 里才建好，中间件在请求期从 `app.state` 取
    #    （理由见 `middleware.ApiMiddleware` 的注释）。
    app.add_middleware(ApiMiddleware)

    install_exception_handlers(app)

    prefix = api_config.api_prefix(settings)
    app.include_router(chat_router, prefix=prefix)
    app.include_router(infra_router)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, Any]:
        return {
            "service": SERVICE_NAME,
            "version": app.version,
            "docs": "/docs",
            "health": "/health",
            "endpoints": {
                "chat": f"POST {prefix}/chat",
                "stream": f"WS {prefix}/chat/stream",
                "approve": f"POST {prefix}/chat/approve",
            },
            "rate_limited": [f"{method} {path}" for method, path in sorted(SCOPE_BY_ROUTE)],
        }

    # 让「哪些路径吃配额」「哪些角色要过闸门」在启动日志里可见
    # （新增接口/角色时最容易忘，日志比文档更容易被看到）
    logger.info(
        "路由就绪：prefix=%r 限流路径=%s 闸门角色=%s 敏感分支=%s",
        prefix,
        sorted(f"{method} {path}" for method, path in SCOPE_BY_ROUTE),
        sorted(GATED_ROLES),
        sorted(SENSITIVE_ROUTES),
    )
    return app


__all__ = [
    "DEFAULT_PROBE_TIMEOUT",
    "SERVICE_NAME",
    "create_app",
    "make_graph_factory",
]
