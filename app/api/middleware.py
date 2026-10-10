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

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("app.api.access")

#: 需要限流的**接口方法 + 路径**（显式登记，不做前缀脑补）。
#:
#: 只登记业务写/贵的接口，理由见模块 docstring；`/health` 与 `/metrics` 刻意不在里面
#: （探针与抓取不该吃配额，否则依赖抖动时会连带把探针打成 429 —— 那会把「服务正常」
#: 误报成「服务不可用」）。
LIMITED_SCOPES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("POST", "/api/chat"),
        ("POST", "/api/chat/approve"),
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

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # ⚠️ `finally` 而不是「正常路径」：下游抛异常时（500）也必须留下这条访问日志 ——
            # 恰恰是出错的那次最需要它。计的是**真实**状态码（`send_wrapper` 里记的）。
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
        """访问日志 + 指标（一处出口，HTTP 与 429 都从这里记）。"""
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
        metrics = self._metrics_of(scope)
        if metrics is None:
            return
        from app.api.metrics import status_class

        labels = {"endpoint": f"{method} {path}", "status_class": status_class(status)}
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
