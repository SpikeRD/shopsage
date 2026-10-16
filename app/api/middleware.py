"""ASGI 中间件：**请求 id + 访问日志 + 限流**（`9.api.md` §4 第 3 条）。

## 为什么是纯 ASGI 中间件，而不是 `BaseHTTPMiddleware`

`BaseHTTPMiddleware` 写起来短，但它对**响应体做了一次流式包装**：每个响应都会
经过一层 anyio 的任务组。在本项目里有两个具体代价：

1. WebSocket 与普通 HTTP 混在同一个 app 里时，`BaseHTTPMiddleware` 会参与非 HTTP 的
   scope 分发 —— 一次升级路径上的异常会让症状表现为「WS 握手莫名其妙失败」；
2. 它对 `BackgroundTask` / 流式响应的时序有影响，而第 16 步的 Web 端要用流式。

纯 ASGI 版只有 `__call__` 里那几行，代价是多写 20 行 —— 换来的是「中间件不做任何
看不见的事」。这符合本项目的一条纪律：**能看见的机制优先**（`8.graph-runbook.md`
用同样的理由放弃了 LangGraph 的 `error_handler=`）。

## 三件事都必须**在**中间件里做（而不是靠路由各自记得）

| 职责 | 若放在路由里会怎样 |
|---|---|
| 请求 id | 每个路由各写一份生成逻辑 → 排障时有的接口有 id、有的没有 |
| 限流 | 新增一个接口就漏一个限流（默认拒绝在路由层表达不出来） |
| 访问日志 | 日志格式漂移；且 4xx/5xx 的分支里容易忘了记 |

限流的**规则表**是白名单式的显式登记（`LIMITED_SCOPES`）：没登记的接口**不**限流，
但 `/metrics`、`/docs` 这类不算业务出口的路径也不该吃配额 —— 所以这里用「显式登记」
而不是「前缀匹配」。新增业务接口时，登记限流这件事会写在路由文件的顶部注释里。

## 限流结果也写进响应头

放行时把 `X-RateLimit-*` 挂上去（`/health`、`/metrics` 之外），前端据此提前减速，
少踩 429。这是 GitHub / Stripe 的事实标准头名，前端现成的组件能直接吃。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Final

from app.api.ratelimit import RateLimitRuntime, caller_bucket
from app.observability.instrument import record_request, trace_request_span

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("app.api.access")

#: 状态码 → 低基数标签。**在本模块导入期取一次**（而不是在请求里 `import`）：
#: 请求路径上的 import 会持有 import 锁，在多线程首请求时表现为「莫名慢一下」。
from app.api.metrics import status_class  # noqa: E402 - 见上（刻意放在 logger 之后）

#: 需要限流的**接口方法 + 路径**（显式登记，不做前缀脑补）。
#:
#: 只登记业务写/贵的接口，理由见模块 docstring；`/health` 与 `/metrics` 刻意不在里面
#: （探针与抓取不该吃配额，否则依赖抖动时会连带把探针打成 429 —— 那会把「服务正常」
#: 误报成「服务不可用」）。
LIMITED_SCOPES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("POST", "/api/chat"),
        ("POST", "/api/chat/approve"),
        # 第 12 步：提交长任务。**必须在这里登记**，否则 `POST /api/tasks` 会
        # 完全不限流 —— 而 `scope.SCOPE_BY_ROUTE` 里有没有它、`ratelimit.DEFAULT_RULES`
        # 有没有额度，都不会因此报错（症状是「额度配了但不生效」，见 runbook §10.1）。
        # 三道登记缺一不可：本常量（中间件要不要限）、`scope.py`（用哪个 scope）、
        # `ratelimit.DEFAULT_RULES`（默认额度是多少）。
        ("POST", "/api/tasks"),
        # ── 变更 001 的账号接口（**issue 003 修复**）─────────────────────────────
        # ⚠️ 这两条曾经漏了这里：`scope.SCOPE_BY_ROUTE` 有 `auth.login`、
        # `ratelimit.DEFAULT_RULES` 也配了 10/300s，但**中间件根本不会去问限流器** ——
        # 因为下面 `__call__` 只在 `(method, path) in LIMITED_SCOPES` 时才判定。
        # 症状是「登录额度配了却完全不起作用」：实测连续 40 次错误登录全是 401、
        # 一次 429 都没有（对照 `POST /api/chat` 第 31 次起正常 429）。
        # 登录是唯一能用「猜」换到令牌的入口，漏登记等于给暴力破解无限次尝试。
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/refresh"),
    }
)

#: 请求 id 的响应头名（与 `schemas.REQUEST_ID_HEADER` 同源；这里再导一次是为了让
#: 中间件不依赖 pydantic 模型，保持「中间件只处理裸 ASGI」的边界）
REQUEST_ID_HEADER: Final[str] = "X-Request-Id"

#: 请求 id 的入参长度上限：客户端可以带自己的 id（链路追踪），但**不能无限长**
#: （否则它进了日志就是一个日志注入面）。超长就忽略、改用服务端生成的。
MAX_INBOUND_REQUEST_ID: Final[int] = 64


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def _headers_dict(scope: dict[str, Any]) -> dict[str, str]:
    """把 ASGI 的 `headers`（bytes 对）翻成小写键的 dict（只留我们关心的几个）。

    ⚠️ ASGI 的 header 名**已经是小写**，但值可能重复（例如多个 `X-Request-Id`）。
    这里取第一条，理由是「先出现的那个是离客户端最近的代理写的」——
    与本项目「不信任 X-Forwarded-For」的口径一致：请求头是输入，不是事实。
    """
    out: dict[str, str] = {}
    for key, value in scope.get("headers") or ():
        name = key.decode("latin-1").lower()
        if name in out:
            continue
        out[name] = value.decode("latin-1")
    return out


def _client_ip(scope: dict[str, Any]) -> str:
    client = scope.get("client")
    return str(getattr(client, "host", "") or (client[0] if client else "") or "")


def resolve_request_id(headers: dict[str, str]) -> str:
    """客户端给的 id 能用就用（链路追踪），但不能是任意长的东西。"""
    inbound = str(headers.get(REQUEST_ID_HEADER.lower(), "") or "").strip()
    if inbound and len(inbound) <= MAX_INBOUND_REQUEST_ID and inbound.isprintable():
        return inbound
    return new_request_id()


async def _send_json(send: Any, *, status: int, payload: dict[str, Any], headers: dict[str, str]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    raw: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json; charset=utf-8")]
    raw.extend((k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items())
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


class ApiMiddleware:
    """请求 id → 限流 → 调用下游 → 访问日志（顺序即理由，见下）。

    ⚠️ 限流器与指标注册表都**在请求期从 `app.state` 取**（而不是构造时定死）：
    中间件在 `create_app()` 里就要挂上（import 期），而限流器要读 `REDIS_URL`、
    可能要连 Redis —— 那些都不该发生在 import 期（否则「跑一条用例」会变成「连一次 Redis」）。
    更实际的理由：`app.state` 让测试能按用例换掉限流器（见 `tests/api/conftest.py`）。
    """

    def __init__(self, app: Any, *, metrics: Any = None) -> None:
        self.app = app
        self._metrics = metrics

    def _limiter(self, scope: dict[str, Any]) -> Any:
        """取限流器：从 ASGI 的 scope 里拿 app（`scope["app"]` 由 Starlette 注入）。"""
        app = scope.get("app")
        return getattr(getattr(app, "state", None), "rate_limiter", None)

    def _metrics_of(self, scope: dict[str, Any]) -> Any:
        if self._metrics is not None:
            return self._metrics
        app = scope.get("app")
        return getattr(getattr(app, "state", None), "metrics", None)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            # WebSocket 的限流在**路由里**做（那里才有身份），这里只放行。
            await self.app(scope, receive, send)
            return

        headers = _headers_dict(scope)
        request_id = resolve_request_id(headers)
        scope.setdefault("state", {})["request_id"] = request_id

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        started = time.perf_counter()

        # ── 限流（在调用下游**之前**，理由见模块 docstring 的表）────────────────
        decision = None
        if (method, path) in LIMITED_SCOPES:
            decision = self._check_limit(scope, headers, method, path, ip=_client_ip(scope))
            if decision is not None and not decision.allowed:
                await self._record(scope, method, path, 429, started)
                await _send_json(
                    send,
                    status=429,
                    payload={
                        "error": {
                            "code": "rate_limited",
                            "message": f"请求过于频繁：每 {int(decision.window_seconds)} 秒最多 {decision.limit} 次",
                            "detail": {"limit": decision.limit, "window_seconds": decision.window_seconds},
                        },
                        "request_id": request_id,
                    },
                    headers={**decision.headers(), REQUEST_ID_HEADER: request_id},
                )
                return

        extra_headers = dict(decision.headers()) if decision is not None else {}
        extra_headers[REQUEST_ID_HEADER] = request_id
        status_holder = {"status": 500}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
                raw = list(message.get("headers") or ())
                existing = {k.decode("latin-1").lower() for k, _ in raw}
                for name, value in extra_headers.items():
                    if name.lower() not in existing:
                        raw.append((name.lower().encode("latin-1"), value.encode("latin-1")))
                message = {**message, "headers": raw}
            await send(message)

        # ── 第 11 步：根 span 必须**包住整个请求**（否则就不是一棵树）───────────
        #
        # ⚠️ 这是本步实测踩到的一个**看起来对、其实完全没接上**的写法：
        # 第一版在 `finally` 里调 `record_request_span(...)` —— 它开一条新 span、
        # 立刻关掉，于是：
        #   ① 它是**请求结束之后**才开的，不可能是任何节点 span 的父亲；
        #   ② 每个节点 span 自己成了根 → **每个节点一个 trace**，
        #      「按 trace 查一次请求」这条排障路径彻底失效（而每个节点看起来都埋了点）。
        # 正确做法：在这里**持有**一个 span 直到请求结束，让下游所有 span 都挂在它下面
        # （OTEL 用 contextvars 传递「当前 span」，所以 `await self.app(...)` 期间
        # 下游一切埋点自动成为子 span，不需要它们知道中间件的存在）。
        with trace_request_span(method, path) as request_span:
            if request_id:
                request_span.set_attribute("request_id", request_id)
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                # ⚠️ `finally` 而不是「正常路径」：下游抛异常时（500）也必须留下这条访问日志 ——
                # 恰恰是出错的那次最需要它。计的是**真实**状态码（`send_wrapper` 里记的）。
                request_span.set_attribute("status_class", status_class(status_holder["status"]))
                await self._record(scope, method, path, status_holder["status"], started)

    def _check_limit(
        self, scope: dict[str, Any], headers: dict[str, str], method: str, path: str, *, ip: str
    ) -> Any:
        """做一次限流判定。**判定失败不阻断请求**（限流是保护，不是准入）。"""
        from app.api.scope import scope_name

        limiter = self._limiter(scope)
        if limiter is None:
            logger.warning("限流器未初始化（应用未走 lifespan 启动？）：本次请求不限流")
            return None
        caller = self._caller(headers, ip)
        scope_key = scope_name(method, path)
        try:
            return limiter.check(caller, scope_key)
        except Exception as exc:  # noqa: BLE001 - 限流后端故障不能把整站打成 500
            logger.warning("限流判定异常（本次放行）：%s", exc)
            return None

    def _caller(self, headers: dict[str, str], ip: str) -> str:
        """桶键的「谁」那一段：**优先解析令牌**，拿不到就退回来源 IP。

        ⚠️ 这里**不放行/拒绝任何请求**，只是分桶。真正的鉴权在路由的依赖里做
        （401 语义、`WWW-Authenticate` 头都在那边）。所以「令牌解析失败」在这里
        只意味着「按匿名桶计」，不意味着「这个请求可以不鉴权」。
        """
        token = ""
        raw = headers.get("authorization", "")
        scheme, _, value = raw.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
        if token:
            try:
                from app.security.auth import parse_token

                principal = parse_token(token)
                return caller_bucket(user_id=principal.user_id, role=principal.role)
            except Exception:  # noqa: BLE001 - 无效令牌按匿名处理（路由会 401）
                logger.debug("令牌无法解析（按匿名桶限流）")
        return caller_bucket(user_id=None, client_ip=ip)

    async def _record(self, scope: dict[str, Any], method: str, path: str, status: int, started: float) -> None:
        """访问日志 + 埋点（一处出口，HTTP 与 429 都从这里记）。

        ## 第 11 步：为什么埋点分两处写（**这是有意的**）

        | 写什么 | 写去哪 | 为什么 |
        |---|---|---|
        | 指标（计数/耗时） | `app.state.metrics` | 那是 `create_app(metrics=...)` 的注入点：**一个 app 一份注册表**，测试注入干净的一份，`/metrics` 渲染同一份 |
        | 根 span（`http.<方法 路径>`） | `app.observability.tracing` | trace 属于**请求上下文**，它的归属由 lifespan 设的 telemetry 根决定（不是中间件能决定的） |

        第 9 步的设计在这里正好用上：中间件**只依赖 `counter()` / `observe()` 两个写入口**，
        所以「实现从手写注册表换成 prometheus_client」对它是透明的（见 `api/metrics.py`）。

        访问日志仍然写在这里（日志不属于可观测性三件套的任何一件，它是第四件事：
        「出事时最先看的东西」），且 `finally` 保证 4xx/5xx 也留下记录。
        """
        duration = time.perf_counter() - started
        request_id = str((scope.get("state") or {}).get("request_id", ""))
        access_logger.info(
            "%s %s -> %d %.0fms req=%s",
            method,
            path,
            status,
            duration * 1000,
            request_id,
        )
        # ⚠️ `endpoint` 用**折叠后**的 `方法 路径`：原始路径带 id 时
        # （`/api/orders/12345`）每个 id 都是一条新时间序列（基数爆炸）。
        # 折叠规则只有一处（这里），因为它只有在中间件里才拿得到「完整路径」。
        endpoint = f"{method} {path}"
        # 请求计数：写**两个名字**（`shopsage_requests_total` 是第 11 步的通用名，
        # `shopsage_api_requests_total` 是第 9 步的对外契约名）。
        # 同一次调用、同一个耗时值 → 两个名字不会漂移成两个数字；而
        # 「第 9 步已经配好的看板/告警」与「第 11 步的通用出口」都能拿到数据。
        # ⚠️ 写入方只有**这一处**（`instrument.record_request`）—— 一个量一个出口，
        # 否则两份数据必然漂移，而漂移的看板比没有看板更危险。
        record_request(endpoint=endpoint, seconds=duration, status=status, request_id=request_id)
        metrics = self._metrics_of(scope)
        if metrics is None:
            return
        labels = {"endpoint": endpoint, "status_class": status_class(status)}
        metrics.counter("shopsage_api_requests_total", labels)
        metrics.observe("shopsage_api_request_duration_seconds", duration, labels)


__all__ = [
    "LIMITED_SCOPES",
    "MAX_INBOUND_REQUEST_ID",
    "REQUEST_ID_HEADER",
    "ApiMiddleware",
    "new_request_id",
    "resolve_request_id",
]
