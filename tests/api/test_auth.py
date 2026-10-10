"""鉴权用例：`9.api.md` §7 的 `test_requires_auth`，外加 WebSocket 侧的同一条口径。

`9.api.md` §4 第 1 条要的是「对客/对内走同一 API、靠 JWT 区分角色」，
所以本文件同时钉住三件事：

1. **没令牌一律 401**（HTTP 与 WebSocket 两个入口都要，不能只做一半 ——
   WebSocket 的鉴权最容易漏，因为它不走 `Authorization` 头）；
2. **令牌的问题要能分类**（过期 / 篡改 / 类型不对 → 都 401，但话术不同，
   前端据此决定「去登录」还是「重新拿令牌」）；
3. **身份只来自令牌**：请求体里塞 `user_id` / `role` 必须被拒（422），
   否则那就是 D1 越权的入口。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.schemas import WS_CLOSE_UNAUTHORIZED
from app.security import auth
from app.security.policy import CUSTOMER_ROLE
from tests.api.conftest import (
    headers_for,
    make_token,
    read_until_done,
    wait_until_closed,
    ws_subprotocols,
)


# ── HTTP：401 的四条路径 ─────────────────────────────────────────────────────


def test_chat_without_token_is_401(client: TestClient) -> None:
    """`9.api.md` §7 第二条用例：无 token → 401。"""
    response = client.post("/api/chat", json={"message": "我的订单到哪了"})
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert response.headers["WWW-Authenticate"] == "Bearer", "401 必须带 WWW-Authenticate（标准语义）"


@pytest.mark.parametrize(
    "header",
    [
        "token-without-scheme",
        "Basic YWJjOmRlZg==",
        "Bearer ",
        "Bearer not-a-jwt",
    ],
)
def test_malformed_authorization_header_is_401(client: TestClient, header: str) -> None:
    response = client.post("/api/chat", json={"message": "你好"}, headers={"Authorization": header})
    assert response.status_code == 401


def test_expired_token_is_401_with_a_distinguishable_message(client: TestClient) -> None:
    """过期与篡改都返回 401，但话术不同 —— 前端据此决定「重新登录」还是「重试」。"""
    expired = auth.create_access_token(2, ttl_seconds=-10)
    response = client.post("/api/chat", json={"message": "你好"}, headers=headers_for(expired))
    assert response.status_code == 401
    assert "过期" in response.json()["error"]["message"]


def test_tampered_token_is_401(client: TestClient, customer_token: str) -> None:
    tampered = customer_token[:-3] + ("aaa" if not customer_token.endswith("aaa") else "bbb")
    response = client.post("/api/chat", json={"message": "你好"}, headers=headers_for(tampered))
    assert response.status_code == 401
    assert "无效" in response.json()["error"]["message"]


def test_refresh_token_cannot_be_used_as_access_token(client: TestClient) -> None:
    """刷新令牌只能换令牌，不能访问业务接口（`app/security/auth.py` 的第 4 条口径）。"""
    refresh = auth.create_refresh_token(2, role=CUSTOMER_ROLE)
    response = client.post("/api/chat", json={"message": "你好"}, headers=headers_for(refresh))
    assert response.status_code == 401
    assert "令牌类型不对" in response.json()["error"]["message"]


def test_bad_signature_token_is_401(client: TestClient) -> None:
    """用**别的密钥**签的令牌：签名对不上 → 401（这条防的是「换个环境签的令牌能用」）。"""
    import jwt as pyjwt

    forged = pyjwt.encode(
        {"sub": "2", "role": "admin", "typ": "access", "iat": 1, "exp": 9999999999},
        "another-secret-entirely-not-ours",
        algorithm="HS256",
    )
    response = client.post("/api/chat", json={"message": "你好"}, headers=headers_for(forged))
    assert response.status_code == 401


# ── 身份只能来自令牌 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("extra", [{"user_id": 7}, {"role": "admin"}, {"user_id": 7, "role": "admin"}])
def test_identity_fields_in_body_are_rejected(client: TestClient, auth_headers: dict[str, str], extra: dict) -> None:
    """请求体里塞身份字段 → 422（`extra="forbid"`）。

    这是 D1 最重要的一条**形状约束**：身份只能来自已验签的令牌。
    请求体里放 `user_id` 等于「让被攻击的对象自己报身份证」，是最经典的越权入口。
    """
    response = client.post("/api/chat", json={"message": "我的订单到哪了", **extra}, headers=auth_headers)
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert any("user_id" in field["loc"] or "role" in field["loc"] for field in body["error"]["detail"]["fields"])


def test_session_id_cannot_forge_another_user_thread(client: TestClient, auth_headers: dict[str, str], patched_query) -> None:
    """`session_id="1:foo"` 会被拒：`thread_id` 的形状是 `u{user_id}:{session_id}`，
    允许 `:` 就等于允许客户端造出「看起来属于别人」的会话键。

    拦在**两层**（都是 `schemas.SESSION_ID_PATTERN` 的同一份正则）：

    1. pydantic 的字段校验 → 422 `validation_error`（请求体形状不对）；
    2. `deps.resolve_session_id` → 422 `invalid_session_id`（防御性：绕过 pydantic 的调用方，
       例如 WebSocket 的自由 JSON，会走到这里）。

    这条覆盖第 1 层；第 2 层由 `test_chat_stream.py::test_invalid_session_id_returns_error_frame` 覆盖。
    """
    response = client.post(
        "/api/chat", json={"message": "我的订单到哪了", "session_id": "1:foo"}, headers=auth_headers
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert any(field["loc"][-1] == "session_id" for field in body["error"]["detail"]["fields"])


# ── 令牌的 alg 混淆攻击（端到端版）──────────────────────────────────────────


def test_alg_none_token_is_rejected_end_to_end(client: TestClient) -> None:
    """`alg: none` 伪造令牌：在**真实路由**上也要被拒（第 4 步的用例只测了解析函数）。"""
    import base64
    import json

    payload = {"sub": "2", "role": "customer", "typ": "access", "iat": 1, "exp": 9999999999}
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    forged = f"{header.decode()}.{body.decode()}."
    response = client.post("/api/chat", json={"message": "你好"}, headers=headers_for(forged))
    assert response.status_code == 401


# ── WebSocket：同一个入口的鉴权（最容易漏的那一半）──────────────────────────


def test_websocket_without_token_is_closed_with_4401(client: TestClient) -> None:
    """没有令牌的 WS 连接：先 accept 再送 `error` 帧、最后用 4401 关闭。

    为什么不是握手阶段直接拒：浏览器**读不到**握手失败的 HTTP 状态码，
    关闭码是它唯一能编程处理的信息（`WS_CLOSE_UNAUTHORIZED = 4401` 在应用自定义段）。
    """
    with client.websocket_connect("/api/chat/stream", subprotocols=["shopsage.v1"]) as websocket:
        frame = websocket.receive_json()
        assert frame["event"] == "error"
        assert frame["data"]["code"] == "unauthorized"
        assert wait_until_closed(websocket) == WS_CLOSE_UNAUTHORIZED


def test_websocket_token_from_query_string_is_not_supported(client: TestClient, customer_token: str) -> None:
    """令牌**只**认子协议：走查询串会给 4401（并把令牌挡在 access log 之外）。

    这条用例是「有意的缺口」：`?token=...` 会被 access log、浏览器历史、Referer 带走，
    所以本项目刻意不支持它。用例存在的意义是：谁要是打算「顺手加一个查询串支持」，
    它会红，并把人带到 `deps.ws_token` 的 docstring 去读理由。
    """
    with client.websocket_connect(f"/api/chat/stream?token={customer_token}", subprotocols=["shopsage.v1"]) as ws:
        frame = ws.receive_json()
        assert frame["event"] == "error"
        assert frame["data"]["code"] == "unauthorized"


def test_websocket_subprotocol_is_echoed_back(client: TestClient, customer_token: str, patched_query) -> None:
    """握手时服务端选中的子协议必须是客户端提出的之一（浏览器的硬要求）。"""
    with client.websocket_connect(
        "/api/chat/stream", subprotocols=ws_subprotocols(customer_token)
    ) as websocket:
        assert websocket.accepted_subprotocol == "shopsage.v1"
        websocket.send_json({"message": "我的订单到哪了", "session_id": "s-auth"})
        assert websocket.receive_json()["event"] == "skeleton"


def test_websocket_uses_the_token_identity_not_the_body(client: TestClient, patched_query) -> None:
    """WS 消息里塞 `user_id` 也没用：身份只来自令牌（未知字段被忽略，但**绝不采信**）。

    ⚠️ 与 HTTP 的差别是有意的：HTTP 的请求体走 pydantic（`extra="forbid"` → 422），
    而 WS 的每条消息是**自由 JSON**（前端可能带各种字段）。这里断言的是
    「多余的字段不会改变身份」—— 也就是 `state["user_id"]` 仍等于令牌里的 user_id。
    """
    from tests.api.conftest import make_token

    token = make_token(2, "customer")
    with client.websocket_connect("/api/chat/stream", subprotocols=ws_subprotocols(token)) as websocket:
        websocket.send_json({"message": "我的订单到哪了", "session_id": "s-identity", "user_id": 7, "role": "admin"})
        frames = read_until_done(websocket)
        done = frames[-1]
        # thread_id 的形状就能证明用的是谁的身份：u2:... 而不是 u7:...
        assert done["data"]["thread_id"] == "u2:s-identity"
