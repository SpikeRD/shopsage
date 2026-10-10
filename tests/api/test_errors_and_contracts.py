"""错误映射用例：**域异常 → HTTP**（`9.api.md` §6 第 5 步、`8.graph-runbook.md` §9 的接线点）。

第 7、8 步刻意让异常带着字段穿透（`PermissionDenied.missing` / `SQLSecurityError.rule` /
`AgentOutputError.raw`），为的就是在这里一次翻成正确的状态码。本文件逐条验那个映射表：

| 异常 | 期望码 | 为什么是这个码（详见 `errors.py` 的表） |
|---|---|---|
| `PermissionDenied` | **404** | 不能告诉攻击者「资源存在但你不够格」（403 会做这件事） |
| `SQLSecurityError` | 400 | 模型生成的 SQL 不合规 → 这次请求做不成 |
| `AgentOutputError` | 502 | 上游（模型）坏了，不是调用方的错 |
| `ToolNotAvailableError` | 501 | 「这个能力还没做」（rag 属第 10 步） |
| `ToolContractError` | 400 | 契约不满足 = 调用方写错了（**不能被报成 500**） |
| `RetryableToolError` | 503 + `Retry-After` | 临时故障，等一会儿再来 |
| `PermanentToolError` | 500 | 库/工具永久失败（不是重试能解决的） |
| `CheckpointUnavailable` | 503 | 状态后端不可用 |
| `MissingIdentityError` | 500 | **接线错误**（入口忘了写 user_id），必须显式暴露 |

另外验四条「形状」契约：所有错误同一个信封、未知路径/方法不对也走信封、
验证错误保留字段级原因、请求 id 可被网关透传。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.agent_llm import AgentOutputError
from app.agents.state import MissingIdentityError
from app.api.errors import DEFAULT_RETRY_AFTER_SECONDS, ApiError, error_response
from app.graph.checkpointer import CheckpointUnavailable
from app.security.guardrail import PromptInjectionError
from app.security.rbac import PermissionDenied
from app.security.sql_guard import SQLSecurityError
from app.tools.errors import (
    PermanentToolError,
    RetryableToolError,
    ToolContractError,
    ToolNotAvailableError,
)
from tests.api.conftest import headers_for, make_token


def _app_with_failing_graph(app_factory, exc: Exception):
    """造一个「跑图必炸」的 app：把 graph_factory 换成直接抛异常的假工厂。

    为什么在**工厂**这一层注入（而不是 monkeypatch 某个节点）：
    工厂是 `create_app` 的公开注入点，用它等于同时验了「API 层确实把异常交给 handler 处理」——
    如果哪天有人在路由里 `try/except Exception` 自己吞掉，这条用例会红。
    """

    def boom(runtime, actor):  # noqa: ANN001, ANN202
        raise exc

    return app_factory(graph_factory=boom)


MAPPING_CASES = [
    pytest.param(
        PermissionDenied("越权拦截", role="customer", missing=["read:all_orders"]),
        404,
        "not_found_or_forbidden",
        id="permission-denied-404",
    ),
    pytest.param(PromptInjectionError("疑似提示词注入", rules=["ignore_instructions"]), 400, "prompt_rejected", id="injection-400"),
    pytest.param(SQLSecurityError("不该出现的写操作", rule="not_read_only"), 400, "sql_rejected", id="sql-guard-400"),
    pytest.param(
        AgentOutputError("模型没给出 JSON", agent="data", raw="一段散文", reason="no_json", attempts=2),
        502,
        "model_output_unparsable",
        id="agent-output-502",
    ),
    pytest.param(ToolNotAvailableError("知识库检索还没落地（第 10 步）"), 501, "capability_not_available", id="not-implemented-501"),
    pytest.param(ToolContractError("参数不对"), 400, "tool_contract", id="contract-400"),
    pytest.param(RetryableToolError("库连接被断"), 503, "temporarily_unavailable", id="retryable-503"),
    pytest.param(PermanentToolError("表不存在"), 500, "tool_failure", id="permanent-500"),
    pytest.param(CheckpointUnavailable("Postgres 打不开"), 503, "checkpoint_unavailable", id="checkpoint-503"),
    pytest.param(MissingIdentityError("state 缺少合法的 user_id"), 500, "missing_identity", id="missing-identity-500"),
]


@pytest.mark.parametrize(("exc", "expected_status", "expected_code"), MAPPING_CASES)
def test_domain_exception_mapping(
    app_factory, patched_query, exc: Exception, expected_status: int, expected_code: str
) -> None:
    """逐条验那张映射表（含「子类优先」的顺序）。"""
    app = _app_with_failing_graph(app_factory, exc)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/chat",
            json={"message": "我的订单到哪了", "session_id": "err-map"},
            headers=headers_for(make_token(2, "customer")),
        )

    assert response.status_code == expected_status, response.text
    body = response.json()
    assert body["error"]["code"] == expected_code
    assert body["request_id"]


def test_contract_error_is_not_reported_as_500(app_factory, patched_query) -> None:
    """**子类顺序**专项：`ToolContractError` 是 `PermanentToolError` 的子类。

    如果 handler 注册顺序反了（先注册父类），契约错误会被报成 500 ——
    排障的人会去翻数据库日志，而真实原因只是调用方少传了一个字段。
    """
    app = _app_with_failing_graph(app_factory, ToolContractError("缺少必填字段 order_id"))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/chat",
            json={"message": "退款", "session_id": "err-contract"},
            headers=headers_for(make_token(2, "customer")),
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "tool_contract"


def test_permission_denied_is_404_and_leaks_no_internal_wording(app_factory, patched_query) -> None:
    """越权对外是 404，且**内部话术不原样出网**（角色/缺失权限只在日志与结构化 detail 里）。

    404 的选择理由见 `errors.py`：403 会确认「资源存在」，攻击者据此枚举订单号是否存在。
    """
    app = _app_with_failing_graph(
        app_factory,
        PermissionDenied("越权拦截：身份 2 请求查 9 的数据", role="customer", missing=["read:all_orders"]),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/chat",
            json={"message": "查别人的订单", "session_id": "err-404"},
            headers=headers_for(make_token(2, "customer")),
        )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["message"] == "未找到该资源，或你没有访问它的权限"
    assert "越权拦截" not in response.text, "内部措辞不该出现在响应体里"
    assert body["error"]["detail"]["role"] == "customer", "结构化 detail 里要留排障信息"


def test_retryable_failure_carries_retry_after(app_factory, patched_query) -> None:
    """503 必须带 `Retry-After`：告诉调用方「等多久」，而不是让它自己猜着重试。"""
    app = _app_with_failing_graph(app_factory, RetryableToolError("库连接被断"))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/chat",
            json={"message": "我的订单到哪了", "session_id": "err-503"},
            headers=headers_for(make_token(2, "customer")),
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(DEFAULT_RETRY_AFTER_SECONDS)


def test_model_raw_output_never_goes_out(client: TestClient, auth_headers: dict[str, str], monkeypatch) -> None:
    """`AgentOutputError.raw`（模型原文）只进日志、**不进响应**。

    理由：模型原文可能包含**它刚刚读到的业务数据片段**（例如它复述了一行别人的订单）。
    排障要 raw 时去日志里拿，出网的东西只留结构化的原因。
    """
    raw_text = "这是模型吐出的原文，里面可能带 order_id=9001 这样的业务数据"

    def boom(runtime, actor):  # noqa: ANN001, ANN202
        raise AgentOutputError("解析失败", agent="data", raw=raw_text, reason="no_json", attempts=1)

    app = client.app  # type: ignore[attr-defined]
    original = app.state.graph_factory
    app.state.graph_factory = boom
    try:
        response = client.post(
            "/api/chat", json={"message": "你好", "session_id": "err-raw"}, headers=auth_headers
        )
    finally:
        app.state.graph_factory = original
    assert response.status_code == 502
    assert "9001" not in response.text
    assert raw_text not in response.text


def test_validation_error_keeps_field_level_reasons(client: TestClient, auth_headers: dict[str, str]) -> None:
    """422 保留字段级原因（那是调用方唯一能自救的信息）。"""
    response = client.post("/api/chat", json={"message": ""}, headers=auth_headers)
    assert response.status_code == 422
    detail = response.json()["error"]["detail"]["fields"]
    assert any(field["loc"][-1] == "message" for field in detail), detail


def test_message_length_limit_is_enforced(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post("/api/chat", json={"message": "查" * 2001}, headers=auth_headers)
    assert response.status_code == 422


def test_unknown_path_uses_the_same_envelope(client: TestClient) -> None:
    """未知路径也走同一个错误信封（否则前端要写两套解析）。"""
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error", "request_id"}
    assert body["error"]["code"] == "not_found"


def test_method_not_allowed_uses_the_same_envelope(client: TestClient) -> None:
    response = client.get("/api/chat")  # 只支持 POST
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_request_id_header_is_echoed_and_client_supplied_one_is_honoured(client: TestClient) -> None:
    """客户端带 `X-Request-Id` 就用它（链路追踪），不带就服务端生成。"""
    generated = client.get("/health")
    assert generated.headers["X-Request-Id"]

    supplied = client.get("/health", headers={"X-Request-Id": "trace-from-gateway"})
    assert supplied.headers["X-Request-Id"] == "trace-from-gateway"
    assert supplied.json()["request_id"] == "trace-from-gateway"


def test_absurdly_long_request_id_is_ignored(client: TestClient) -> None:
    """超长请求 id 一律忽略（否则它进了日志就是一个日志注入面）。"""
    response = client.get("/health", headers={"X-Request-Id": "x" * 500})
    assert response.headers["X-Request-Id"] != "x" * 500
    assert len(response.headers["X-Request-Id"]) <= 64


def test_non_printable_request_id_is_ignored(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-Id": "bad\tid"})
    assert response.headers["X-Request-Id"] != "bad\tid"


def test_api_error_helper_builds_the_envelope() -> None:
    """`ApiError` 是路由里唯一该用的「主动报错」方式（带稳定的 code）。"""
    exc = ApiError(429, "rate_limited", "慢点", detail={"limit": 3}, headers={"Retry-After": "5"})
    assert exc.status_code == 429
    assert exc.code == "rate_limited"
    assert exc.headers["Retry-After"] == "5"

    response = error_response(status_code=429, code="rate_limited", message="慢点", detail={"limit": 3})
    assert response.status_code == 429
    assert b"rate_limited" in response.body


def test_unknown_exception_is_not_swallowed(app_factory, patched_query) -> None:
    """未预期的异常**不**被我们的 handler 吞掉（仍然是框架的 500 语义）。

    这条是**刻意的边界**：只映射已知的域异常。把 `Exception` 也接管会得到一个
    「所有 500 都长得像业务错误」的 API —— 那会让真正的 bug 消失在统一话术后面。
    两条断言一起看才算证明：

    1. 关掉 `raise_server_exceptions` 时得到 500，且响应体**不是**我们的错误信封
       （说明没人把它翻成业务错误）；
    2. 打开它时 `TestClient` 会把原始异常**抛给用例**（说明异常确实穿透到了框架层）。
    """
    error = RuntimeError("意料之外的 bug")

    def boom(runtime, actor):  # noqa: ANN001, ANN202
        raise error

    app = app_factory(graph_factory=boom)
    payload = {"message": "你好", "session_id": "err-unknown"}
    headers = headers_for(make_token(2, "customer"))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/chat", json=payload, headers=headers)
    assert response.status_code == 500
    # ⚠️ 这里**不能** `response.json()`：未预期的异常由 Starlette 的 `ServerErrorMiddleware`
    # 处理，返回的是 `text/plain` 的 `Internal Server Error`（不是 JSON）。
    # 这本身就是断言的一部分 —— 我们的错误信封没有被套上去。
    assert response.headers["content-type"].startswith("text/plain")
    assert b"rate_limited" not in response.content and b'"error"' not in response.content

    with TestClient(app, raise_server_exceptions=True) as client, pytest.raises(RuntimeError) as excinfo:
        client.post("/api/chat", json=payload, headers=headers)
    assert excinfo.value is error, "抛出来的必须是我们注入的那个异常（没被换成语义化的话术）"
