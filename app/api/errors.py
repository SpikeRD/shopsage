"""**异常 → HTTP 的映射表**（`9.api.md` §4 第 4 条：API 层不做业务，只做翻译）。

`8.graph-runbook.md` §9 与 `7.agents-runbook.md` §11 都把这张表列成第 9 步的交付物：
第 7、8 步刻意让异常**带着字段**穿透（`PermissionDenied.missing` / `SQLSecurityError.rule` /
`AgentOutputError.raw`），就是为了在这里一次翻成正确的状态码 —— 包一层就丢了字段，
错误响应会退化成「500 未知错误」，而那等于把排障成本转嫁给调用方。

## 状态码不是随手挑的（每一行都有理由）

| 异常 | 码 | 为什么是这个码 |
|---|---|---|
| `AuthError` | 401 | 标准语义；带 `WWW-Authenticate: Bearer` 让客户端知道该怎么补 |
| `PermissionDenied` | **404** | 见下方「为什么越权是 404 而不是 403」 |
| `PromptInjectionError` | 400 | 请求本身不合规（是**用户的话**有问题，不是他没权限） |
| `SQLSecurityError` | 400 | 同上：模型生成的 SQL 不合规 = 这次请求做不成，但请求本身没被拒绝 |
| `AgentOutputError` | 502 | **上游**（模型）给了不可解析的输出 —— 这是「网关下游坏了」，不是调用方的错 |
| `ToolNotAvailableError` | 501 | 「这个能力还没做」（rag 属第 10 步）。501 = 语义明确且可永久记忆，比 503 诚实 |
| `ToolContractError` | 400 | 契约不满足 = 请求形状不对（调用方写错了） |
| `PermanentToolError` | 500 | 库/工具永久失败：不是调用方的错，也不该重试 |
| `RetryableToolError` | 503 | 临时故障；**带 `Retry-After`**：告诉调用方「等一会儿再来」，而不是「你错了」 |
| `CheckpointUnavailable` | 503 | 状态后端不可用（和上一条同义：过一会儿可能就好） |
| `MissingIdentityError` | 500 | **接线错误**：入口忘了把 `user_id` 写进 state。不是用户的错，必须显式暴露 |
| `GraphTurnFailed` | 503 | 一轮的永久失败（重放预算用尽） |

## 为什么越权是 404 而不是 403

D1 的场景是「用户 A 能不能查到用户 B 的订单」。这里有两个层次的正确性：

1. **功能正确**：必须拦住（403 和 404 都拦住了）；
2. **信息正确**：**不该告诉 A「B 的订单存在」**。

403 的语义是「这东西存在，但你不够格看」—— 它确认了资源存在，攻击者据此可以
枚举「哪些订单号是真的」（用 403/404 的差异做存在性探测是经典的越权信息泄漏）。
404 的语义是「没有」—— 与「这个订单号根本不存在」不可区分，攻击者拿不到任何增量信息。

代价是**排障时更难分辨**（用户说「查不到」，可能是真没有、也可能是越权被拦）。
所以处置是：**对外 404，对内留痕** —— 日志里打 WARNING（带 role 与缺失权限），
响应的 `error.code` 用 `not_found_or_forbidden`（一个刻意「不区分」的码，
但开发期看日志能区分）。等第 11 步的埋点接上，这条 WARNING 会进 Grafana。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.schemas import REQUEST_ID_HEADER, ErrorDetail, ErrorResponse
from app.security.auth import AuthError
from app.security.guardrail import PromptInjectionError
from app.security.rbac import PermissionDenied
from app.security.sql_guard import SQLSecurityError

logger = logging.getLogger(__name__)

#: `Retry-After` 的默认值（秒）：可恢复故障时给调用方一个「等多久」的明确建议。
#:
#: 30 秒是刻意的：与 `app/llm/client.py` 的退避总量同量级（那里是秒级重试 + 上限），
#: 又与限流的窗口（60 秒）错开 —— 两个数字若都取 60，客户端会正好在窗口刚重置时再来一次，
#: 形成「被拒 → 等满窗 → 立刻再打」的脉冲式流量。
DEFAULT_RETRY_AFTER_SECONDS: Final[int] = 30

#: 对外统一话术：**不区分**「不存在」与「无权」。改这句话前先读模块 docstring。
_NOT_FOUND_OR_FORBIDDEN: Final[str] = "未找到该资源，或你没有访问它的权限"


class ApiError(Exception):
    """API 层自己的错误（带稳定 `code`，便于前端分支）。

    为什么需要它：域异常各有各的字段，但**响应形状**必须统一。
    有了这个类型，路由里可以直接
    `raise ApiError(429, "rate_limited", "请求过于频繁")`，
    不必为每种情况写一个 exception handler。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        log_level: int = logging.INFO,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = dict(detail or {})
        self.headers = dict(headers or {})
        self.log_level = log_level


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str = "",
    detail: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """造一个统一形状的错误响应（唯一出口 —— 别在路由里手搓 JSONResponse）。"""
    payload = ErrorResponse(error=ErrorDetail(code=code, message=message, detail=dict(detail or {})), request_id=request_id)
    out_headers = dict(headers or {})
    if request_id:
        out_headers.setdefault(REQUEST_ID_HEADER, request_id)
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"), headers=out_headers)


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def _domain_handlers() -> list[tuple[type[BaseException], Any]]:
    """域异常 → 处置。**顺序即优先级**（下面按「具体 → 笼统」排列）。

    ⚠️ 顺序很重要的原因：`ToolContractError` / `ToolNotAvailableError` 都是
    `PermanentToolError` 的子类，`RetryableToolError` 是 `ToolError` 的子类。
    把 `PermanentToolError` 排在前面的结果是「契约错误被报成 500」，
    而排错的人会去翻数据库日志（实际原因只是调用方少传了一个字段）。
    """
    from app.agents.agent_llm import AgentOutputError
    from app.graph.checkpointer import CheckpointUnavailable
    from app.graph.run import GraphTurnFailed
    from app.agents.state import MissingIdentityError
    from app.tools.errors import (
        PermanentToolError,
        RetryableToolError,
        ToolContractError,
        ToolNotAvailableError,
    )

    def permission(exc: PermissionDenied) -> tuple[int, str, str, dict[str, Any], int]:
        # 只记**事实**（角色、缺哪些权限），不记用户问的原文 —— 日志里不留业务明文。
        logger.warning(
            "越权/权限不足（对外按 404 处理）：role=%s missing=%s reason=%s",
            exc.role or "(未提供)",
            list(exc.missing),
            exc,
        )
        return (
            status.HTTP_404_NOT_FOUND,
            "not_found_or_forbidden",
            _NOT_FOUND_OR_FORBIDDEN,
            {"role": exc.role, "missing": list(exc.missing)},
            logging.WARNING,
        )

    def sql_guard(exc: SQLSecurityError) -> tuple[int, str, str, dict[str, Any], int]:
        return (
            status.HTTP_400_BAD_REQUEST,
            "sql_rejected",
            "这条查询请求不合规，已被拒绝执行",
            {"rule": getattr(exc, "rule", "")},
            logging.WARNING,
        )

    def agent_output(exc: AgentOutputError) -> tuple[int, str, str, dict[str, Any], int]:
        # ⚠️ 不回传 `raw`：它可能包含模型吐出的**业务数据片段**（而这一轮可能已经被拦过）。
        # raw 进日志即可（本地排障够用），出网的东西只留结构化的原因。
        logger.error("模型输出不可解析：agent=%s reason=%s attempts=%s", exc.agent, exc.reason, exc.attempts)
        return (
            status.HTTP_502_BAD_GATEWAY,
            "model_output_unparsable",
            "模型这次没能给出可用的结果，请重试",
            {"agent": exc.agent, "reason": exc.reason},
            logging.ERROR,
        )

    def not_available(exc: ToolNotAvailableError) -> tuple[int, str, str, dict[str, Any], int]:
        return (
            status.HTTP_501_NOT_IMPLEMENTED,
            "capability_not_available",
            str(exc),
            {},
            logging.INFO,
        )

    def contract(exc: ToolContractError) -> tuple[int, str, str, dict[str, Any], int]:
        return (status.HTTP_400_BAD_REQUEST, "tool_contract", str(exc), {}, logging.WARNING)

    def retryable(exc: RetryableToolError) -> tuple[int, str, str, dict[str, Any], int]:
        return (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "temporarily_unavailable",
            "后端暂时不可用，请稍后重试",
            {},
            logging.WARNING,
        )

    def permanent(exc: PermanentToolError) -> tuple[int, str, str, dict[str, Any], int]:
        return (status.HTTP_500_INTERNAL_SERVER_ERROR, "tool_failure", str(exc), {}, logging.ERROR)

    def identity(exc: MissingIdentityError) -> tuple[int, str, str, dict[str, Any], int]:
        # 500 级**接线错误**：不是用户的问题，日志必须是 ERROR 级别（否则它会静默地一直错下去）。
        logger.error("入口没有把 user_id 写进 state：%s", exc)
        return (
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "missing_identity",
            "服务端内部错误（身份未注入 state）",
            {},
            logging.ERROR,
        )

    def checkpoint(exc: CheckpointUnavailable) -> tuple[int, str, str, dict[str, Any], int]:
        return (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "checkpoint_unavailable",
            "会话状态后端暂不可用，请稍后重试",
            {},
            logging.ERROR,
        )

    def turn_failed(exc: GraphTurnFailed) -> tuple[int, str, str, dict[str, Any], int]:
        return (status.HTTP_503_SERVICE_UNAVAILABLE, "turn_failed", str(exc), {}, logging.ERROR)

    def injection(exc: PromptInjectionError) -> tuple[int, str, str, dict[str, Any], int]:
        return (
            status.HTTP_400_BAD_REQUEST,
            "prompt_rejected",
            "这条请求包含不被允许的指令，已被拒绝",
            {"rules": list(getattr(exc, "rules", ()) or ())},
            logging.WARNING,
        )

    def auth(exc: AuthError) -> tuple[int, str, str, dict[str, Any], int]:
        return (
            status.HTTP_401_UNAUTHORIZED,
            "unauthorized",
            str(exc),
            {},
            logging.WARNING,
        )

    return [
        (PermissionDenied, permission),
        (PromptInjectionError, injection),
        (SQLSecurityError, sql_guard),
        (AuthError, auth),
        (AgentOutputError, agent_output),
        (ToolNotAvailableError, not_available),
        (ToolContractError, contract),
        (RetryableToolError, retryable),
        (PermanentToolError, permanent),
        (MissingIdentityError, identity),
        (CheckpointUnavailable, checkpoint),
        (GraphTurnFailed, turn_failed),
    ]


def install_exception_handlers(app: FastAPI) -> None:
    """把域异常与两类框架异常都挂到 app 上（**同一个错误信封**）。"""

    handlers = dict(_domain_handlers())

    for exc_type, translate in handlers.items():

        async def handler(request: Request, exc: BaseException, _t: Any = translate) -> JSONResponse:
            status_code, code, message, detail, level = _t(exc)
            logger.log(level, "API 异常：%s（%s %s）", code, request.method, request.url.path)
            headers: dict[str, str] = {}
            if status_code == status.HTTP_401_UNAUTHORIZED:
                headers["WWW-Authenticate"] = "Bearer"
            if status_code in (status.HTTP_503_SERVICE_UNAVAILABLE, 429):
                headers["Retry-After"] = str(DEFAULT_RETRY_AFTER_SECONDS)
            return error_response(
                status_code=status_code,
                code=code,
                message=message,
                request_id=_request_id(request),
                detail=detail,
                headers=headers,
            )

        app.add_exception_handler(exc_type, handler)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        logger.log(exc.log_level, "API 错误：%s（%s %s）", exc.code, request.method, request.url.path)
        return error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            request_id=_request_id(request),
            detail=exc.detail,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """请求体/查询参数不合规 → 422，字段级原因**保留**（这是调用方唯一能自救的信息）。

        ⚠️ `exc.errors()` 里可能带上**输入原值**。这里只挑 `loc` / `msg` / `type` 三个字段：
        请求体是用户自己发的，回显它本身不算泄漏，但回显整个 body 会把
        「服务端看到了什么」变成一份可被日志/截图带走的副本 —— 少一分没必要的数据流转。

        ⚠️ 这里用字面量 `422` 而不是 `status.HTTP_422_UNPROCESSABLE_ENTITY`：
        Starlette 1.7 起把那个常量标成 deprecated（改名 `HTTP_422_UNPROCESSABLE_CONTENT`），
        而**只要在响应里用到它**就会在每次请求时打一条 `StarletteDeprecationWarning`
        （它是在 `_exception_handler` 内部访问常量时触发的）。422 这个数字十年没变过，
        写死比跟着框架重命名跑要稳 —— 也顺手让演示脚本的输出干净。
        """
        fields = [
            {"loc": [str(x) for x in item.get("loc", ())], "msg": item.get("msg", ""), "type": item.get("type", "")}
            for item in exc.errors()
        ]
        return error_response(
            status_code=422,
            code="validation_error",
            message="请求参数不合规",
            request_id=_request_id(request),
            detail={"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """框架自己抛的 HTTPException（404 路由不存在、405 方法不对…）也走同一信封。

        不接管的话，同一个 API 会同时存在两种错误形状（FastAPI 的 `{"detail": ...}` 与
        我们的 `{"error": {...}}`）—— 前端得写两套解析。

        ⚠️ 只接管 HTTP：WebSocket 路由抛的 `HTTPException`（例如握手前的 403）
        必须继续走 Starlette 默认处置（它会用 `websocket.close()` 关连接）。
        在这里把它翻成 `JSONResponse` 会往一条还没有握手的连接上写 HTTP 响应体，
        结果是握手失败得更难查（客户端只看到「连接被关闭」，服务端日志里也没有原因）。
        """
        if (getattr(request, "scope", {}) or {}).get("type") != "http":
            raise exc
        code = {
            404: "not_found",
            405: "method_not_allowed",
            401: "unauthorized",
            403: "forbidden",
        }.get(exc.status_code, "http_error")
        headers = dict(getattr(exc, "headers", None) or {})
        return error_response(
            status_code=exc.status_code,
            code=code,
            message=str(exc.detail),
            request_id=_request_id(request),
            headers=headers,
        )


__all__ = [
    "DEFAULT_RETRY_AFTER_SECONDS",
    "ApiError",
    "error_response",
    "install_exception_handlers",
]
