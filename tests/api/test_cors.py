"""CORS：**浏览器**能跨源读这个 API 的那一层（第 16 步新增，实测撞到才有它）。

## 为什么这组用例必须存在（而不是「配一下就行」）

第 16 步的 Web 前端在 `http://127.0.0.1:3000`，API 在 `http://127.0.0.1:8000`
—— **端口不同就是不同源**。第 9 步的 API 从来没有跨源调用方，所以一个 CORS 中间件都没注册，
后果是前端一探活就得到：

```text
连不上后端（http://127.0.0.1:8000）：Failed to fetch
```

**没有任何 HTTP 状态码**，最容易被误判成「后端没起」或「令牌无效」——
而实际上服务完全健康、令牌也完全有效，只是浏览器拒绝把响应交给页面。

⚠️ **同一个坑还有第二层（本组用例的存在理由）**：本步的真链路用例（`web/tests/live`）
是 **Node 直接 `fetch`**，它**不受 CORS 约束** —— 也就是说
「9 条真链路全绿」完全不能证明浏览器能打开这个页面。所以必须有**服务端这一侧**的断言：
拿 `Origin` 头打进来，看响应里有没有 `Access-Control-Allow-Origin`。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.config import DEV_CORS_ORIGINS, cors_origins
from app.api.main import create_app

WEB_ORIGIN = "http://127.0.0.1:3000"


@pytest.fixture
def client() -> TestClient:
    """本组用例只关心中间件层，所以用默认 app（内存限流 + 真依赖照常探活）。"""
    return TestClient(create_app())


# ── 配置面 ──────────────────────────────────────────────────────────────────


def test_dev_default_allows_the_next_dev_server_origins(monkeypatch) -> None:
    """开发期默认放通前端来源 —— 且 `localhost` 与 `127.0.0.1` **都要有**。

    两者在浏览器眼里是**不同的来源**，只配一个的症状是
    「用 localhost 打开能用、换 127.0.0.1 就 Failed to fetch」（反之亦然）——
    这种「换个地址就坏」的现象最容易被归因成别的东西。
    """
    monkeypatch.delenv("ENV", raising=False)
    origins = cors_origins(None)
    assert WEB_ORIGIN in origins
    assert "http://localhost:3000" in origins
    assert set(DEV_CORS_ORIGINS) <= set(origins)


def test_configured_origins_win_and_are_normalised(monkeypatch) -> None:
    """显式配置优先，且去掉末尾斜杠（浏览器发的 `Origin` **没有**末尾斜杠，
    配成 `http://host:3000/` 会永远匹配不上 —— 这是 CORS 最常见的配置错）。"""
    monkeypatch.setenv("API_CORS_ORIGINS", " http://example.test:8080/ , http://other.test ")
    assert cors_origins(None) == ["http://example.test:8080", "http://other.test"]


def test_wildcard_is_rejected_loudly(monkeypatch) -> None:
    """`*` 必须报错而不是放行：白名单是显式的（与 `4.security.md` 的「表白名单」同一纪律）。"""
    monkeypatch.setenv("API_CORS_ORIGINS", "*")
    with pytest.raises(ValueError, match="不允许写成"):
        cors_origins(None)


def test_malformed_origin_is_rejected(monkeypatch) -> None:
    """漏了协议的来源要**启动即拒**（配置错必须 fail-fast，与限流表达式同一口径）。"""
    monkeypatch.setenv("API_CORS_ORIGINS", "localhost:3000")
    with pytest.raises(ValueError, match="必须带协议"):
        cors_origins(None)


def test_prod_defaults_to_no_cross_origin(monkeypatch) -> None:
    """`ENV=prod` 且没配 → **空列表**（= 不允许任何跨源读）。

    生产应当同源部署（反代把 `/api` 与页面放同一个域），或显式列出域名 ——
    默认放开跨源等于把「哪些站点能从浏览器里读这个 API」交给任何网站。
    """
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.delenv("API_CORS_ORIGINS", raising=False)
    assert cors_origins(None) == []


# ── 中间件行为（真正被前端依赖的部分）───────────────────────────────────────


def test_simple_request_from_the_web_origin_gets_the_cors_header(client: TestClient) -> None:
    """★核心用例：带 `Origin` 的 GET 必须回 `Access-Control-Allow-Origin`。

    没有这个头，浏览器会把响应当作**不可读**（页面侧看到的是一句 `Failed to fetch`）——
    与「服务没起」在页面上长得一模一样。这一条用例就是那件事的服务端判据。
    """
    response = client.get("/health", headers={"Origin": WEB_ORIGIN})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == WEB_ORIGIN


def test_unknown_origin_gets_no_cors_header(client: TestClient) -> None:
    """不在白名单里的来源**不许**拿到 CORS 头（它是白名单，不是「谁问都给」）。"""
    response = client.get("/health", headers={"Origin": "http://evil.test"})
    assert response.status_code == 200, "服务照常处理（CORS 只约束浏览器，不拦服务端调用）"
    assert "access-control-allow-origin" not in response.headers


def test_preflight_is_answered_without_authentication(client: TestClient) -> None:
    """★预检必须在**鉴权/限流之前**被答掉：预检带不了 `Authorization`。

    如果 CORS 不是最外层，预检会先撞上鉴权 → 浏览器看到的是
    「CORS 失败」，而服务端日志里只有 401（两侧对不上，最难查）。
    """
    response = client.options(
        "/api/chat",
        headers={
            "Origin": WEB_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200, f"预检不该被鉴权拦下：{response.status_code} {response.text[:200]}"
    assert response.headers.get("access-control-allow-origin") == WEB_ORIGIN
    assert "POST" in response.headers.get("access-control-allow-methods", "")
    # ⚠️ 别用子串断言（`"a,b" in "a, b"` 会因为**空格**而失败 —— 本用例第一版就是这么挂的）：
    # 头里是一个**列表**，要拆开比集合
    allowed = {
        item.strip().lower()
        for item in response.headers.get("access-control-allow-headers", "").split(",")
        if item.strip()
    }
    assert {"authorization", "content-type"} <= allowed, f"allow-headers 缺项：{sorted(allowed)}"


def test_preflight_does_not_consume_rate_limit_quota(client: TestClient) -> None:
    """预检**不吃配额**：否则「同一个页面点几次」会因为预检把额度刷光而 429。

    判据：连打 40 次预检（默认 `chat` 额度是 30/60s），最后一次仍然是 200 且带 CORS 头。
    """
    for _ in range(40):
        response = client.options(
            "/api/chat",
            headers={"Origin": WEB_ORIGIN, "Access-Control-Request-Method": "POST"},
        )
        assert response.status_code == 200, "预检被限流了：浏览器会以为是 CORS 失败"
    # 紧接着的业务请求仍然有额度（说明预检没有扣桶）
    ok = client.post(
        "/api/chat",
        headers={"Origin": WEB_ORIGIN, "Content-Type": "application/json"},
        json={"message": "我的订单到哪了"},
    )
    assert ok.status_code == 401, "没有令牌就该是 401（这里要的是「不是 429」）"
    assert ok.headers.get("access-control-allow-origin") == WEB_ORIGIN


def test_error_responses_also_carry_cors_headers(client: TestClient, customer_token: str) -> None:
    """★错误响应也要带 CORS 头 —— 否则前端连「为什么失败」都读不到。

    没有这个头，401/429/422 在页面上**全都是**一句 `Failed to fetch`：
    用户看到「连不上后端」，而真相可能是「令牌过期了」或「点太快被限流了」。
    """
    unauthorized = client.post(
        "/api/chat",
        headers={"Origin": WEB_ORIGIN, "Content-Type": "application/json"},
        json={"message": "我的订单到哪了"},
    )
    assert unauthorized.status_code == 401
    assert unauthorized.headers.get("access-control-allow-origin") == WEB_ORIGIN
    # 错误信封本身也照常（CORS 是叠在上面的一层，不改业务响应）
    assert unauthorized.json()["error"]["code"] == "unauthorized"

    # ⚠️ 校验类错误（422）**必须带令牌**才看得到：`require_principal` 是路由依赖，
    #    鉴权在请求体校验之前 —— 不带令牌只会得到 401（本用例第一版把这两件事混了）
    validation = client.post(
        "/api/chat",
        headers={
            "Origin": WEB_ORIGIN,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {customer_token}",
        },
        json={"message": "我的订单到哪了", "user_id": 999},
    )
    assert validation.status_code == 422
    assert validation.headers.get("access-control-allow-origin") == WEB_ORIGIN


def test_request_id_header_is_exposed_to_the_browser(client: TestClient) -> None:
    """`X-Request-Id` 要在 `Access-Control-Expose-Headers` 里。

    否则前端（第 16 步的排障提示语）读不到它 —— 而它是把「用户反馈的一句话」
    对到服务端日志的唯一凭据。
    """
    response = client.get("/health", headers={"Origin": WEB_ORIGIN})
    exposed = response.headers.get("access-control-expose-headers", "").lower()
    assert "x-request-id" in exposed


# ── 反向断言：路由用到的方法**都**要在 allow_methods 里（issue 005）────────────

def _cors_allow_methods() -> set[str]:
    """从中间件配置里取 CORS 放通的方法集合（`create_app()` 的装配结果）。"""
    from starlette.middleware.cors import CORSMiddleware

    for middleware in create_app().user_middleware:
        if middleware.cls is CORSMiddleware:
            declared = middleware.kwargs.get("allow_methods") or []
            return {str(item).upper() for item in declared}
    raise AssertionError("没找到 CORSMiddleware：跨源调用会全线失败（配置被删了？）")


def _route_methods() -> set[str]:
    """路由**实际**用到的 HTTP 方法（从 OpenAPI schema 取，那是权威）。

    ⚠️ 为什么不用遍历 `app.routes`：Starlette 1.7 把 `include_router` 的结果包成
    `_IncludedRouter`，直接遍历只能看到 `/docs` 那几条**内置**路由 ——
    本用例第一版就是这么写的，于是「API 用到的 PATCH」根本没被枚举到，
    一条本该红的断言反而绿了（实测）。OpenAPI schema 是 FastAPI 生成的权威视图，
    它一定包含全部已注册的业务路由。
    """
    schema = create_app().openapi()
    out: set[str] = set()
    for operations in schema.get("paths", {}).values():
        out.update(str(method).upper() for method in operations)
    return out


def test_every_route_method_is_allowed_by_cors() -> None:
    """★路由用到的每个方法都必须在 `allow_methods` 里（issue 005 的防回归判据）。

    ## 这条用例要挡的是什么

    管理台的「停用 / 改角色」走 `PATCH /api/admin/accounts/{id}`，而 CORS 的
    `allow_methods` 一度只有 `["GET", "POST", "OPTIONS"]`。`PATCH` 是**非简单方法**，
    浏览器一定先发预检 —— 预检被 `400 Disallowed CORS method` 拒掉之后，
    **那条真正的请求根本不会发出**：

    ```text
    页面：  请求没有到达后端（没有状态码）… TypeError: Failed to fetch
    控制台：Response to preflight request doesn't pass access control check
    后端：  一条请求都没有（预检在中间件层就被答掉了）
    ```

    这个症状极易被误判成「后端没起」或「令牌失效」，而真相是白名单漏了一个方法。

    ## 为什么既有用例没拦住

    `test_preflight_is_answered_without_authentication` 只验了 `POST`（简单方法），
    它证明「allow_methods 里的方法能过预检」，**证不了反方向**
    （有 scope、有额度、却没登记 —— 与 `issues/003` 的 P0-1 是同一类漏项）。
    所以这里做的是**反向**断言：以路由为起点，逐个要求白名单包含它。
    """
    allowed = _cors_allow_methods()
    used = _route_methods()

    assert used, "OpenAPI schema 里没有任何路由方法（收集逻辑坏了？）"

    # 预检自身要用 OPTIONS；漏了它所有跨源写请求都会被拒
    assert "OPTIONS" in allowed, "allow_methods 里没有 OPTIONS：浏览器发不了预检"

    missing = sorted(used - allowed)
    assert not missing, (
        f"这些方法被路由用到、但 CORS 没放通：{missing}\n"
        "  —— 浏览器会先发预检，预检被 400 拒 → 那条请求根本不会发出\n"
        "  （症状：页面上 `Failed to fetch` 且**没有状态码**，后端日志里一条请求都没有）"
    )

