"""FastAPI 依赖注入：**鉴权、身份、会话键、限流、图与依赖的取用**（`9.api.md` §5 第一条）。

`9.api.md` §5 说「依赖注入做鉴权：逻辑一处定义、处处复用」。本模块就是那一处。
路由函数只写 `actor: Actor = Depends(require_actor)`，不自己解析令牌。

## 三个刻意的设计决定

### ① 身份对象是 `Actor`，不是 `Principal`

`Principal`（`app/security/auth.py`）只回答「令牌没被改、没过期、是谁」；
`Actor` 在它之上补了「**这次调用属于哪条路径**」：

| 字段 | 来源 | 用途 |
|---|---|---|
| `user_id` / `role` | 令牌（已验签） | 写进图 state（`agents.state.inject_identity`）、审计、限流分桶 |
| `thread_id` | `user_id` + 请求里的 `session_id` | checkpoint 隔离键 |
| `session_id` | 请求里的（或服务端生成） | 回给前端，让下一轮接上同一个会话 |
| `internal` | `rbac.has_permission(role, read:all_orders)` | 决定「调试载荷给不给」「审批闸门开不开」 |

为什么 `internal` 用权限点判断而不是写 `role != "customer"`：角色表是可扩展的
（`ROLE_PERMISSIONS` 随时会加一行），而「是不是内部人」的判据应该是**能力**而不是身份名字。
多写一处 `!= "customer"`，就是多一处会在新增角色时悄悄判错的地方。

### ② `thread_id` 由服务端派生，`user_id` 前缀是**越权防线**

`thread_id = f"u{user_id}:{session_id}"`。为什么不能让客户端直接给 `thread_id`
（`8.graph.md` §4 第 3 条已经埋了这条要求）：checkpoint 里装的是**完整的对话状态**，
能任选 `thread_id` 就等于能读写别人的会话 —— 那不是「越权查询一行数据」，
而是「读到别人上一轮的原话」。

前缀之外还有一道：`session_id` 的字符集**不允许** `:`（见 `schemas.SESSION_ID_PATTERN`），
否则用户可以提交 `session_id="1:foo"` 造出 `u2:1:foo`，而 user 1 的桶形状是 `u1:...`，
看起来像但拼不出来 —— 这类「形状相似」的字符串是最容易在后续某次重构里真的撞上的。
两道叠加，才叫「派生规则只有一处」。

### ③ WebSocket 的令牌走**子协议**，不走查询串

浏览器的 WebSocket API 不允许自定义请求头，所以令牌只有三个去处：

| 去处 | 问题 |
|---|---|
| URL 查询串 `?token=...` | **会进 access log、会留在浏览器历史、会被 Referer 带出去** |
| `Sec-WebSocket-Protocol` 子协议 | 唯一干净的通道（本模块选它） |
| cookie | 需要 CSRF 配套，且与本项目的 JWT 口径不一致 |

代价是「令牌会出现在握手的请求头里」，但那本来也是 `Authorization` 的位置。
⚠️ 子协议**必须**同时带上 `shopsage.v1`：服务端要 `accept(subprotocol="shopsage.v1")`
才能让浏览器接受握手，而浏览器要求服务端选的那个值必须是客户端提出的之一。
"""

from __future__ import annotations

import logging
import secrets
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from fastapi import HTTPException, Request, WebSocket, status

from app.api.ratelimit import RateLimitDecision, RateLimitRuntime
from app.api.schemas import (
    REQUEST_ID_HEADER,
    SESSION_ID_PATTERN,
    WS_TOKEN_PREFIX,
)
from app.security.auth import AuthError, Principal, bearer_token_from_header, parse_token
from app.security.rbac import PERM_READ_ALL_ORDERS, has_permission

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注：避免运行期把 graph 全量 import 进来
    from app.graph import GraphRuntime

logger = logging.getLogger(__name__)

#: 会话 id 的校验（与 `schemas.SESSION_ID_PATTERN` **同一份**正则 —— 两处漂移会让
#: 「OpenAPI 里说能过、服务端却拒绝」这种最没必要的联调问题出现）
SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(SESSION_ID_PATTERN)

#: 没带 `session_id` 时服务端生成的会话 id 前缀。
#:
#: ⚠️ 生成的会话 id **只在本次响应的 `session_id` 字段里回给前端**，服务端不留映射：
#: 客户端下一轮带着它回来就接上同一个 thread，不带就开新会话。
#: 「服务端替客户端记住会话」需要另一份存储（那是会话服务的事），
#: 而本项目的无状态要求（架构第 8 节）恰恰是不留这种进程内状态。
GENERATED_SESSION_PREFIX: Final[str] = "s"


@dataclass(frozen=True)
class Actor:
    """一次调用的身份与派生上下文（路由函数只该看这个对象）。"""

    user_id: int
    role: str
    session_id: str
    thread_id: str
    internal: bool
    request_id: str = ""

    def state_fields(self) -> dict[str, Any]:
        """要写进图 state 的字段（与 `tests/graph_helpers.customer_state` 同口径）。"""
        from app.agents.state import inject_identity

        return dict(inject_identity(self.user_id, self.role))


def new_session_id() -> str:
    """造一个服务端会话 id（短、无连字符、足够不可猜）。"""
    return f"{GENERATED_SESSION_PREFIX}{secrets.token_hex(8)}"


def _session_id_error(raw: Any) -> Exception:
    """会话 id 非法时的统一错误（**用 `ApiError` 而不是 `HTTPException`**）。

    为什么不用 `HTTPException`：这两个校验函数被 **HTTP 与 WebSocket 两条路径**共用，
    而 WebSocket 侧需要的是一条 `error` 帧（`chat.py` 里 `except ApiError` 才接得住）。
    在 WS 消息循环里抛 `HTTPException` 的结果是异常穿出 `chat_stream` ——
    连接直接断掉，而客户端连一句原因都拿不到（本仓实测踩到过，见 runbook §10.1 坑 6）。
    `ApiError` 由 `errors.install_exception_handlers` 在 HTTP 侧翻成同样的错误信封，
    所以两条路径共用一个异常类型，响应形状也一致。
    """
    from app.api.errors import ApiError

    return ApiError(
        422,
        "invalid_session_id",
        f"session_id 非法：只允许字母/数字/下划线/连字符/点，长度 1~64（收到 {raw!r}）",
    )


def thread_id_for(user_id: int, session_id: str) -> str:
    """派生 checkpoint 隔离键：`u{user_id}:{session_id}`。

    ⚠️ 校验顺序不能反：**先验格式再拼**。先拼的话，`session_id="1:foo"` 会被接受成一个
    「看起来属于 user 1」的 thread —— 而那是别人的会话（见模块 docstring ②）。
    """
    value = str(session_id or "").strip()
    if not SESSION_ID_RE.match(value):
        raise _session_id_error(session_id)
    return f"u{int(user_id)}:{value}"


def resolve_session_id(raw: str | None) -> str:
    """请求里的 `session_id` → 最终使用的会话 id（缺省时生成一个）。"""
    if raw is None or not str(raw).strip():
        return new_session_id()
    value = str(raw).strip()
    if not SESSION_ID_RE.match(value):
        raise _session_id_error(raw)
    return value


def restore_session_id(thread_id: str, *, user_id: int) -> str:
    """`u1:abc` → `abc`（并**校验它确实属于这个用户**）。

    为什么需要反向解析：批准通道收到的只有 `thread_id`，而回给前端的响应信封里要有
    `session_id` —— 不做反向解析就得把 session_id 另存一份在别处（多一份状态就多一处漂移）。

    ⚠️ 前缀校验不能省：`u9:abc` 传给 user 1 时必须报错（而不是「照用」）——
    否则这条路径就成了「任选别人的会话」的旁路，而那正是 `thread_id_for` 想堵住的事。
    """
    from app.api.errors import ApiError

    prefix = f"u{int(user_id)}:"
    text = str(thread_id or "")
    if not text.startswith(prefix):
        raise ApiError(
            422,
            "invalid_thread",
            "thread_id 不属于当前用户：请原样回传响应里的 thread_id，不要自己拼",
        )
    session = text[len(prefix) :]
    if not SESSION_ID_RE.match(session):
        raise ApiError(422, "invalid_thread", f"thread_id 里的 session 段非法（收到 {session!r}）")
    return session


def actor_from_principal(
    principal: Principal, *, session_id: str, request_id: str = ""
) -> Actor:
    """把「已验签的身份」+「会话 id」组装成 `Actor`。"""
    return Actor(
        user_id=principal.user_id,
        role=principal.role,
        session_id=session_id,
        thread_id=thread_id_for(principal.user_id, session_id),
        internal=has_permission(principal.role, PERM_READ_ALL_ORDERS),
        request_id=request_id,
    )


# ── FastAPI 依赖：HTTP ───────────────────────────────────────────────────────


def require_principal(request: Request) -> Principal:
    """HTTP 侧的身份依赖（401 + `WWW-Authenticate: Bearer`）。

    实现上直接复用第 4 步的 `auth.bearer_token_from_header` + `parse_token`：
    那两处已经钉住了「算法固定 HS256 / `exp`/`iat`/`sub` 必填 / 类型必须是 access」四条口径，
    这里再写一遍解析就多了一处可以漂移的真相。

    ⚠️ **不要**写成 `Depends(auth.get_current_user)`：那个函数的第一个参数
    （`authorization: str | None`）会被 FastAPI 当成**查询参数** —— 症状是
    「带 Authorization 头却拿不到身份」，而报错完全看不出是依赖声明的问题
    （`4.security-runbook.md` §4.4 已经记过这条）。
    """
    try:
        token = bearer_token_from_header(request.headers.get("authorization"))
        return parse_token(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_actor(request: Request) -> Actor:
    """HTTP 侧：身份 + 会话（会话来自请求体，由路由显式传入 `resolve_session_id` 之后的结果）。

    ⚠️ 本依赖**不碰请求体**（那需要把 body 读一遍，而 body 只能被消费一次）。
    所以路由的写法是两段：先 `Depends(require_principal)` 拿到身份，
    再用 `actor_from_principal(principal, session_id=resolve_session_id(body.session_id))`。
    合并成一个依赖的写法要求中间件先把 body 缓存下来 —— 那会让「每个请求都多一次 JSON 解析」，
    不值得。
    """
    principal = require_principal(request)
    return actor_from_principal(
        principal,
        session_id=new_session_id(),
        request_id=getattr(request.state, "request_id", ""),
    )


# ── FastAPI 依赖：WebSocket ──────────────────────────────────────────────────


def ws_token(websocket: WebSocket) -> str:
    """从握手的子协议里取令牌（`Sec-WebSocket-Protocol: shopsage.v1, bearer.<jwt>`）。

    解析规则（与 `schemas` 里的常量同源）：
    * 逗号分隔的多个子协议；
    * 前缀 `bearer.` 的那一个就是令牌（**令牌里可能有 `.`**，所以用「去掉前缀」而不是 split）。

    为什么要求「令牌放在子协议里」而不是自己发明一个 `X-Token` 头：
    浏览器不会把自定义头发出去，所以那条路只对非浏览器客户端（curl/脚本）有效 ——
    一个「只在测试里能用」的鉴权通道比没有更糟。
    """
    raw = websocket.headers.get("sec-websocket-protocol") or ""
    for item in raw.split(","):
        candidate = item.strip()
        if candidate.lower().startswith(WS_TOKEN_PREFIX):
            return candidate[len(WS_TOKEN_PREFIX) :].strip()
    raise AuthError(
        "WebSocket 缺少令牌：请把子协议写成 "
        f"`shopsage.v1, {WS_TOKEN_PREFIX}<你的 access token>`（浏览器无法自定义请求头）"
    )


def ws_subprotocols(websocket: WebSocket) -> list[str]:
    """客户端提出的子协议列表（服务端要选其中一个回给浏览器）。"""
    from app.api.schemas import WS_SUBPROTOCOL

    offered = [item.strip() for item in (websocket.headers.get("sec-websocket-protocol") or "").split(",")]
    return [item for item in offered if item == WS_SUBPROTOCOL]


def ws_actor(websocket: WebSocket) -> Actor:
    """WebSocket 侧的身份（未握手时调；**不要在 accept 之后调**，那时已无法回 401 语义）。"""
    return actor_from_principal(parse_token(ws_token(websocket)), session_id=new_session_id())


# ── 限流 ────────────────────────────────────────────────────────────────────


def rate_limiter(request: Request) -> RateLimitRuntime:
    """取当前 app 的限流器（由 `main.create_app` 在启动时挂到 `app.state` 上）。

    ⚠️ 从 `app.state` 取而不是模块级单例：测试要能按用例换掉它
    （`tests/api/conftest.py` 就注入了一个「额度=3」的限流器来验 429），
    而模块级单例会让「同一进程里跑两个不同配置的 app」变成不可能。
    """
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:  # pragma: no cover - 正常情况下 lifespan 一定装好了
        raise HTTPException(status_code=503, detail="限流器未初始化（应用未走 lifespan 启动）")
    return limiter


def graph_runtime(request: Request) -> "GraphRuntime":
    """取图侧运行参数（模型/检索注入点 + 闸门策略）。"""
    runtime = getattr(request.app.state, "graph_runtime", None)
    if runtime is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="GraphRuntime 未初始化（应用未走 lifespan 启动）")
    return runtime


def caller_ip(request: Request) -> str:
    """来源 IP（限流分桶用）。

    ⚠️ **不信任** `X-Forwarded-For`：那是个客户端可以随便写的头，
    用它分桶等于「攻击者每次换一个假 IP 就换一个桶」（限流形同虚设）。
    生产在反向代理后面时，正确做法是在网关层做限流（或把代理的可信 IP 段配进来）——
    那是部署编排的事（第 19 步），不在应用里猜。
    """
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "")


def websocket_ip(websocket: WebSocket) -> str:
    client = getattr(websocket, "client", None)
    return str(getattr(client, "host", "") or "")


def rate_limit_headers(decision: RateLimitDecision) -> dict[str, str]:
    """放行时挂在成功响应上的限流头（前端可据此提前减速，少踩 429）。"""
    return decision.headers()


# 便于路由文件 `from app.api import deps` 后用 `deps.require_actor` 之类的写法，
# 同时保留 `Depends(require_actor)` 这种最常见的直接引用。
__all__ = [
    "GENERATED_SESSION_PREFIX",
    "REQUEST_ID_HEADER",
    "SESSION_ID_RE",
    "Actor",
    "actor_from_principal",
    "caller_ip",
    "graph_runtime",
    "new_session_id",
    "rate_limit_headers",
    "rate_limiter",
    "require_actor",
    "require_principal",
    "resolve_session_id",
    "restore_session_id",
    "thread_id_for",
    "websocket_ip",
    "ws_actor",
    "ws_subprotocols",
    "ws_token",
]
