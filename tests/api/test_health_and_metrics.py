"""`GET /health` 与 `GET /metrics` 的用例（`9.api.md` §2 的两个基础设施接口）。

覆盖三件事，每件都对应一个「看起来很合理但会出事」的改动：

| 用例 | 它钉住的改动 |
|---|---|
| `test_health_never_fails_even_when_dependencies_are_down` | 「依赖挂了就返回 503」—— 会让存活探针重启一个健康的进程 |
| `test_health_reports_component_status_without_lying` | 「探活写死 ok」—— 会让排障从一个错误的起点开始 |
| `test_health_is_not_rate_limited` | 「所有接口都限流」—— 会让探针在依赖抖动时被 429 |

`/metrics` 侧钉住三件事：文本格式能被解析、**对客不泄漏**（标签里没有 user_id）、
以及「指标里能查到 B2 的首帧延迟」（第 11 步接 Grafana 时的数据源）。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import frames_by_event, read_until_done, ws_subprotocols


def test_health_returns_200_and_component_list(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded", "down"}
    assert body["service"] == "shopsage-api"
    assert body["request_id"], "响应里必须带请求 id（排障时用它对齐日志）"
    names = {component["name"] for component in body["components"]}
    assert "rate_limit" in names, "限流后端的健康状况必须可见（内存降级是有代价的）"


def test_health_never_fails_even_when_dependencies_are_down(app_factory) -> None:
    """依赖不可达时**仍然 200**：状态写在 body 里（`health.py` 模块 docstring 的取舍）。

    做法：把 `DATABASE_URL` 指向一个**必定不可达**的地址（127.0.0.1:1 —— 保留端口，
    不会有服务监听），并把 Redis 也指过去。
    """
    app = app_factory()
    with TestClient(app) as client:
        settings = app.state.settings.model_copy(
            update={"database_url": "postgresql://u:p@127.0.0.1:1/none", "redis_url": "redis://127.0.0.1:1/0"}
        )
        app.state.settings = settings
        response = client.get("/health")

    assert response.status_code == 200, "存活探针不能因为依赖故障而失败（否则会重启健康进程）"
    body = response.json()
    assert body["status"] == "down", "Postgres 不可达 = 真的 down（业务数据一条都读不到）"
    postgres = next(c for c in body["components"] if c["name"] == "postgres")
    assert postgres["status"] == "down"
    assert "VMware-Linux" in postgres["detail"], "失败要带一句能照做的排查提示（不是光说 unreachable）"
    assert postgres["latency_ms"] is not None, "失败也要有耗时（那是分诊的关键信息之一）"


def test_health_is_not_rate_limited(client: TestClient) -> None:
    """探针不该吃配额：`/health` 连打 10 次（默认 chat 额度只有 3）依然全部 200。"""
    codes = [client.get("/health").status_code for _ in range(10)]
    assert set(codes) == {200}, f"探针被限流了：{codes}"


def test_metrics_exposition_format_is_parseable(client: TestClient) -> None:
    """指标输出必须是 Prometheus 能解析的文本格式（第 11 步接抓取的唯一要求）。"""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in response.headers["content-type"]
    assert response.text.endswith("\n"), "文本格式要求每条指标一行（末尾要有换行）"
    for line in response.text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        assert re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? -?\d", line), f"这行不是合法指标：{line}"


def test_metrics_record_http_requests_with_low_cardinality_labels(client: TestClient) -> None:
    """状态码标签是 `2xx` 这种低基数形式（不是原始码、更不是 user_id）。"""
    client.get("/health")
    text = client.get("/metrics").text
    assert 'shopsage_api_requests_total{endpoint="GET /health",status_class="2xx"}' in text
    assert "user_id" not in text, "指标标签里绝不能出现 user_id（基数爆炸 + 隐私）"


def test_stream_first_frame_metric_is_exposed(client: TestClient, customer_token: str, patched_query) -> None:
    """B2 的量化指标（首帧延迟）必须能在 `/metrics` 里查到 —— 这是第 11 步画图的输入。"""
    with client.websocket_connect(
        "/api/chat/stream", subprotocols=ws_subprotocols(customer_token)
    ) as websocket:
        websocket.send_json({"message": "我的订单到哪了", "session_id": "s-metrics"})
        frames = read_until_done(websocket)

    assert frames_by_event(frames, "done"), "一轮流式应当以 done 帧收尾（否则下面的指标断言没有意义）"
    text = client.get("/metrics").text
    assert "shopsage_api_stream_first_frame_seconds_count" in text
    assert "shopsage_api_stream_answer_seconds_count" in text


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_infrastructure_paths_are_not_under_the_business_prefix(client: TestClient, path: str) -> None:
    """文档/探活挂在根上，不跟 `API_PREFIX` 走（理由见 `api/config.py`）。"""
    assert client.get(path).status_code == 200
    assert client.get(f"/api{path}").status_code == 404
