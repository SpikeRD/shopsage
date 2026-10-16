"""限流用例：`9.api.md` §7 的 `test_rate_limit`（超过阈值 → 429，D1 纵深）。

本文件覆盖「滑动窗口」的四个可断言性质 + 两个容易做错的地方：

| 用例 | 性质 |
|---|---|
| 超限 → 429 + `Retry-After` | 拦截生效，且给了「等多久」 |
| 未超限 → `X-RateLimit-Remaining` 递减 | 前端能提前减速（不是只能撞 429） |
| 桶按「用户」分开 | 一个人刷不爆另一个人的额度 |
| 桶按「接口」分开 | 刷 chat 不该把 approve 的额度吃掉 |
| **窗口滑动** | 时间过了额度会回来（不是永久封禁） |
| **匿名按 IP 分桶** | 未鉴权的刷接口也被压住，且不互相牵连 |

⚠️ 这些用例都用注入的小额度（`tiny_limiter()`，默认 3/60s），
而不是去改 `.env` —— 改环境变量的用例会互相污染（而且跑不了并发）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.ratelimit import (
    MemoryRateLimitStore,
    RateLimitRule,
    RateLimiter,
    caller_bucket,
)
from tests.api.conftest import headers_for, make_token, tiny_limiter


def chat(client: TestClient, headers: dict[str, str], session_id: str = "rl-1"):
    return client.post(
        "/api/chat", json={"message": "我的订单到哪了", "session_id": session_id}, headers=headers
    )


# ── 基本行为 ─────────────────────────────────────────────────────────────────


def test_exceeding_quota_returns_429_with_retry_after(
    client: TestClient, customer_token: str, patched_query
) -> None:
    """`9.api.md` §7 第五条用例：超过阈值 → 429（D1）。"""
    headers = headers_for(customer_token)
    codes = [chat(client, headers, f"rl-{i}").status_code for i in range(4)]

    assert codes[:3] == [200, 200, 200], f"额度是 3，前三次应当放行：{codes}"
    assert codes[3] == 429, f"第四次应当被拦：{codes}"


def test_login_is_rate_limited(app_factory) -> None:
    """★登录接口**必须真的被限流**（issue 003 的 P0-1 行为回归判据）。

    修复前：`scope.SCOPE_BY_ROUTE` 有 `auth.login`、`ratelimit.DEFAULT_RULES` 也配了
    10/300s，但 `middleware.LIMITED_SCOPES` 里没有它 —— 中间件根本不去问限流器，
    于是**连续 40 次错误登录全是 401、一次 429 都没有**（实测）。
    登录是唯一能用「猜」换到令牌的入口，这条失效等于给暴力破解无限次尝试。

    这条用例走**真实 HTTP 路径**（而不是只断言常量），所以它能在「有人把
    `LIMITED_SCOPES` 里的登录项删掉」时立刻红。
    """
    limiter = tiny_limiter()
    # `tiny_limiter` 只给 chat 相关的三个 scope 配额度，这里显式补一条很小的登录规则
    limiter.rules["auth.login"] = RateLimitRule(limit=2, window_seconds=60.0)

    with TestClient(app_factory(rate_limiter=limiter)) as client:
        codes = [
            client.post(
                "/api/auth/login", json={"identifier": "nobody", "password": "wrong"}
            ).status_code
            for _ in range(4)
        ]

    # 前两次放行（凭据错 → 401），第三次起必须被限流拦下（429）
    assert codes[:2] == [401, 401], f"额度是 2，前两次应当放行到鉴权层：{codes}"
    assert codes[2:] == [429, 429], f"超限后必须是 429（而不是继续 401）：{codes}"


def test_429_body_has_the_error_envelope_and_headers(
    client: TestClient, customer_token: str, patched_query
) -> None:
    headers = headers_for(customer_token)
    for i in range(3):
        chat(client, headers, f"rl-env-{i}")
    response = chat(client, headers, "rl-env-over")

    assert response.status_code == 429
    body = response.json()
    assert body["error"]["code"] == "rate_limited"
    assert body["error"]["detail"]["limit"] == 3
    assert body["request_id"]
    assert response.headers["Retry-After"].isdigit()
    assert int(response.headers["Retry-After"]) >= 1
    assert response.headers["X-RateLimit-Remaining"] == "0"


def test_remaining_header_counts_down(client: TestClient, customer_token: str, patched_query) -> None:
    headers = headers_for(customer_token)
    remaining = [chat(client, headers, f"rl-rem-{i}").headers["X-RateLimit-Remaining"] for i in range(3)]
    assert remaining == ["2", "1", "0"]


def test_rate_limit_headers_are_absent_on_infrastructure_paths(client: TestClient) -> None:
    """`/health` 不吃配额，所以也不带 `X-RateLimit-*`（带上了会让人以为它受保护）。"""
    response = client.get("/health")
    assert "X-RateLimit-Limit" not in response.headers


# ── 分桶的维度（用户 / 接口 / 匿名）──────────────────────────────────────────


def test_quota_is_per_user(client: TestClient, patched_query) -> None:
    """一个人刷爆自己的额度，不影响别人（`caller_bucket` 里带 user_id）。"""
    heavy = headers_for(make_token(2, "customer"))
    light = headers_for(make_token(3, "customer"))

    for i in range(3):
        assert chat(client, heavy, f"rl-heavy-{i}").status_code == 200
    assert chat(client, heavy, "rl-heavy-over").status_code == 429
    assert chat(client, light, "rl-light-1").status_code == 200, "另一个用户的额度不该被吃掉"


def test_quota_is_per_endpoint(client: TestClient, customer_token: str, patched_query) -> None:
    """刷 chat 不吃 approve 的额度（`scope.py` 的映射表是分接口的）。"""
    headers = headers_for(customer_token)
    for i in range(3):
        chat(client, headers, f"rl-scope-{i}")
    assert chat(client, headers, "rl-scope-over").status_code == 429

    # approve 走另一个 scope：即使 chat 已经超限，它仍然按自己的额度判定
    response = client.post(
        "/api/chat/approve", json={"thread_id": "c2:none", "approved": True}, headers=headers
    )
    assert response.status_code != 429, "approve 有自己的额度桶，不该被 chat 的 429 牵连"


def test_anonymous_requests_are_bucketed_by_ip(client: TestClient) -> None:
    """未鉴权的请求按来源 IP 分桶（否则一个匿名脚本会把所有匿名用户一起限死）。

    ⚠️ 这条用**限流器本身**验（而不是打 HTTP）：`TestClient` 的 `client` 属性可以被用例改，
    但真正要断言的是「桶键的构成规则」——那是 `caller_bucket` 的契约，
    直接测它比绕过一层 HTTP 更清楚（HTTP 层的 429 已经在上面验过了）。
    """
    assert caller_bucket(user_id=None, client_ip="10.0.0.1") == "ip:10.0.0.1"
    assert caller_bucket(user_id=None, client_ip="10.0.0.2") == "ip:10.0.0.2"
    assert caller_bucket(user_id=None, client_ip="") == "anon"
    assert caller_bucket(user_id=2, role="customer", client_ip="10.0.0.1") == "u:2:customer"


# ── 窗口语义 ─────────────────────────────────────────────────────────────────


def test_window_slides_so_quota_comes_back() -> None:
    """滑动窗口：额度会回来（不是永久封禁）。

    用一个**可控时钟**把时间推过窗口，而不是 `time.sleep(1)`：
    真实 sleep 会让用例变慢，而且窗口一短就会变 flaky（本仓最讨厌的一类红）。
    """
    now = [1000.0]
    limiter = RateLimiter(
        store=MemoryRateLimitStore(clock=lambda: now[0]),
        rules={"chat": RateLimitRule(limit=2, window_seconds=10.0)},
    )
    assert limiter.check("u:2:customer", "chat").allowed
    assert limiter.check("u:2:customer", "chat").allowed
    blocked = limiter.check("u:2:customer", "chat")
    assert blocked.allowed is False
    assert blocked.retry_after >= 1

    now[0] += 10.1  # 窗口滑过去了
    assert limiter.check("u:2:customer", "chat").allowed, "窗口滑过之后应当恢复"


def test_sliding_window_does_not_allow_double_at_the_boundary() -> None:
    """**滑动窗口 vs 固定窗口**的关键区别：窗口边界不放行双倍流量。

    固定窗口（`INCR` + `EXPIRE`）在 59.9s 打满、60.1s 再打满 —— 一个窗口周期内放行 2× limit。
    这条用例把那个缺口钉住：连续 4 次请求（limit=2、窗口 10s）在**任何**时间点都不该
    出现「某 10 秒内超过 2 次被放行」。
    """
    now = [0.0]
    limiter = RateLimiter(
        store=MemoryRateLimitStore(clock=lambda: now[0]),
        rules={"chat": RateLimitRule(limit=2, window_seconds=10.0)},
    )
    allowed_times: list[float] = []
    for step in range(20):
        if limiter.check("u:2:customer", "chat").allowed:
            allowed_times.append(now[0])
        now[0] += 1.0  # 每秒一次

    for start in allowed_times:
        in_window = [t for t in allowed_times if start <= t < start + 10.0]
        assert len(in_window) <= 2, f"窗口 [{start}, {start + 10}) 内放行了 {len(in_window)} 次（> limit）"


# ── WebSocket 侧 ─────────────────────────────────────────────────────────────


def test_websocket_stream_is_rate_limited(app_factory) -> None:
    """WS 连接建立时**就**占一个额度；额度用完时后续连接收到 `error` 帧 + 4429 关闭。

    这里用 `stream=1` 的额度（而不是默认的 3）：只开两条连接就够了 ——
    用例更快，也不依赖「前三条都成功」这种与实现细节耦合的顺序。

    ⚠️ **服务端不会在握手后就主动推帧**：骨架帧是「收到一轮提问之后」才发的
    （见 `chat_stream` 的时序：accept → 限流 → 等消息 → 骨架 → 跑图）。
    所以第一条连接必须**先发消息再读帧** —— 只 `accept` 不发言的话，
    两边会一起等对方（实测：用例直接挂死到超时，见 runbook §10.1 坑 8）。
    """
    from fastapi.testclient import TestClient

    from app.api.schemas import WS_CLOSE_RATE_LIMITED
    from tests.api.conftest import wait_until_closed, ws_subprotocols

    app = app_factory(rate_limiter=tiny_limiter(stream=1))
    token = make_token(2, "customer")

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/chat/stream", subprotocols=ws_subprotocols(token)
        ) as websocket:
            websocket.send_json({"message": "我的订单到哪了", "session_id": "rl-ws-1"})
            assert websocket.receive_json()["event"] == "skeleton", "第一条连接应当放行"

        with client.websocket_connect(
            "/api/chat/stream", subprotocols=ws_subprotocols(token)
        ) as websocket:
            frame = websocket.receive_json()
            assert frame["event"] == "error", f"额度用完后应当收到错误帧：{frame}"
            assert frame["data"]["code"] == "rate_limited"
            assert frame["data"]["retry_after"] >= 1
            assert wait_until_closed(websocket) == WS_CLOSE_RATE_LIMITED


def test_rate_limit_rule_rejects_invalid_configuration() -> None:
    """非法额度**启动即抛**（不是静默退回默认值）。

    静默退回会让「我明明调小了额度，怎么没生效」变成一次线上排障 —— 这类配置错误必须 fail-fast，
    与 `Settings._validate_prod_requirements` 是同一条纪律。
    """
    from app.api.config import parse_rate_limit

    assert parse_rate_limit("30/60") == (30, 60.0)
    assert parse_rate_limit("30") == (30, 60.0)
    with pytest.raises(ValueError):
        parse_rate_limit("abc")
    with pytest.raises(ValueError):
        parse_rate_limit("0/60")
    with pytest.raises(ValueError):
        parse_rate_limit("30/-1")
    with pytest.raises(ValueError):
        parse_rate_limit("")