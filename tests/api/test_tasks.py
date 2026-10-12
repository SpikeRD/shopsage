"""第 12 步的 HTTP 契约：`POST /api/tasks`、`GET /api/tasks/queue`、`GET /api/tasks/{job_id}`。

用 `TestClient` 跑真 app（真路由、真鉴权、真限流），只把**队列**换成内存实现
（`tests/api/conftest.py` 的 `queue` 装置）—— 于是这些用例验的是**接口契约**，
不是队列实现（队列语义在 `tests/queue/` 里用生产的 `bridge_job` + `execute_job` 验）。

⚠️ 这里有两条用例专门钉住**容易写反**的东西：

1. `/tasks/queue` 必须比 `/tasks/{job_id}` **先声明**（否则 `queue` 会被当成一个 job_id，
   症状是「查队列现状返回 404 未找到该任务」—— 路由其实是对的，只是匹配顺序反了）；
2. 别人查你的任务必须 **404**（而不是 403）：`job_id` 是可枚举的，
   403 会确认「这个任务确实存在」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.queue.jobs import FUNC_REPORT
from tests.api.conftest import headers_for, make_token, tiny_limiter
from tests.queue.fake_queue import MemoryJobQueue

TASK_URL = "/api/tasks"


def customer_headers() -> dict[str, str]:
    return headers_for(make_token(2, "customer"))


def internal_headers() -> dict[str, str]:
    return headers_for(make_token(7, "internal_employee"))


# ── 入队（202 + job_id）────────────────────────────────────────────────────


def test_submit_returns_202_and_a_job_id(client: TestClient) -> None:
    """**长任务秒级返回 job_id**：POST 返回 202（已受理），不是 200（拿到了结果）。"""
    response = client.post(
        TASK_URL,
        json={"kind": "report", "params": {"title": "本月订单"}},
        headers=customer_headers(),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job_id"].startswith("q_")
    assert body["kind"] == "report"
    assert body["status"] == "queued"
    assert body["created"] is True
    assert body["status_url"] == f"{TASK_URL}/{body['job_id']}"
    assert body["poll_after_seconds"] > 0


def test_submit_requires_auth(client: TestClient) -> None:
    """没有令牌 → 401（入队会花别人的钱，不能匿名）。"""
    assert client.post(TASK_URL, json={"kind": "report"}).status_code == 401


def test_submit_rejects_unknown_kind(client: TestClient) -> None:
    """未知 kind → 422（`extra="forbid"` + 取值域一起挡住「拼错的任务种类」）。"""
    response = client.post(TASK_URL, json={"kind": "nope"}, headers=customer_headers())
    assert response.status_code == 422


def test_submit_rejects_extra_fields(client: TestClient) -> None:
    """请求体里出现 `user_id` → 422：身份只能来自令牌（D1 的经典入口）。"""
    response = client.post(
        TASK_URL,
        json={"kind": "report", "user_id": 999},
        headers=customer_headers(),
    )
    assert response.status_code == 422


def test_agent_task_requires_a_message(client: TestClient) -> None:
    """`agent` 任务缺 `message` → 422（**在 API 层拦住**，不让它跑到 worker 里再失败）。"""
    response = client.post(TASK_URL, json={"kind": "agent", "params": {}}, headers=customer_headers())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_task"


def test_kb_rebuild_is_refused_for_customers(client: TestClient) -> None:
    """知识库重建是**内部操作**：普通客户提交会被 403 挡回。

    ⚠️ 403（而不是 422）：请求形状完全合法，被拒的原因是**权限**。
    第一版把这条判断写在 `schemas.TaskRequest.enqueue_kwargs` 里并抛 `PermissionError`，
    结果是 500 —— 一个权限问题被报成「服务端未知错误」（见 runbook §10.1）。
    """
    response = client.post(TASK_URL, json={"kind": "kb_rebuild"}, headers=customer_headers())
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "forbidden"
    assert "kb_rebuild" in response.json()["error"]["message"]


def test_kb_rebuild_is_allowed_for_internal(client: TestClient) -> None:
    """内部员工可以提交（并拿到 202 + job_id）。"""
    response = client.post(TASK_URL, json={"kind": "kb_rebuild"}, headers=internal_headers())
    assert response.status_code == 202, response.text
    assert response.json()["created"] is True


def test_idempotent_submit_returns_created_false(client: TestClient) -> None:
    """同一幂等键提交两次 → 第二次 `created=False`（客户端据此知道不必重新等）。"""
    first = client.post(TASK_URL, json={"kind": "report", "params": {"title": "x"}}, headers=customer_headers())
    second = client.post(TASK_URL, json={"kind": "report", "params": {"title": "x"}}, headers=customer_headers())
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert first.json()["created"] is True
    assert second.json()["created"] is False


def test_explicit_idempotency_key_wins_over_params(client: TestClient) -> None:
    """显式幂等键：参数不同也算同一个任务（业务幂等键的用途）。"""
    first = client.post(
        TASK_URL,
        json={"kind": "report", "params": {"title": "甲"}, "idempotency_key": "biz:1"},
        headers=customer_headers(),
    )
    second = client.post(
        TASK_URL,
        json={"kind": "report", "params": {"title": "乙"}, "idempotency_key": "biz:1"},
        headers=customer_headers(),
    )
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.json()["created"] is False


def test_illegal_idempotency_key_is_422(client: TestClient) -> None:
    """非法幂等键（含空格）→ 422，**不做静默清洗**（清洗会让两个请求撞成一个）。"""
    response = client.post(
        TASK_URL,
        json={"kind": "report", "idempotency_key": "a b"},
        headers=customer_headers(),
    )
    assert response.status_code == 422
    assert "幂等键" in response.json()["error"]["message"]


def test_deferred_submit_is_reported_as_deferred(client: TestClient) -> None:
    """延迟任务：响应里 `status="deferred"` + `deferred_seconds`（不是「已排队」）。"""
    response = client.post(
        TASK_URL,
        json={"kind": "report", "defer_by": 60},
        headers=customer_headers(),
    )
    assert response.status_code == 202
    assert response.json()["status"] == "deferred"
    assert response.json()["deferred_seconds"] == 60.0


def test_defer_beyond_the_limit_is_422(client: TestClient) -> None:
    """延迟超过 30 天 → 422（更远的定时任务该用 cron，不该躺在 Redis 里）。"""
    response = client.post(
        TASK_URL,
        json={"kind": "report", "defer_by": 40 * 24 * 3600},
        headers=customer_headers(),
    )
    assert response.status_code == 422


def test_the_injected_queue_receives_the_job(app_factory, queue: MemoryJobQueue) -> None:
    """入队真的落在**注入的队列**上（而不是悄悄连了 Redis）。"""
    with TestClient(app_factory()) as client:
        response = client.post(TASK_URL, json={"kind": "report"}, headers=customer_headers())
    job_id = response.json()["job_id"]
    assert job_id in queue.jobs
    assert queue.jobs[job_id].function == FUNC_REPORT


# ── 查状态 ───────────────────────────────────────────────────────────────────


def test_status_of_a_queued_job(client: TestClient) -> None:
    """刚提交的任务查状态 → `queued`（还没跑）。"""
    submitted = client.post(
        TASK_URL, json={"kind": "report", "params": {"title": "查状态"}}, headers=customer_headers()
    )
    job_id = submitted.json()["job_id"]

    response = client.get(f"{TASK_URL}/{job_id}", headers=customer_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert "worker" in body["description"]
    assert body["result"] is None
    assert body["dead_letter"] is None


def test_status_of_an_unknown_job_is_not_found(client: TestClient) -> None:
    response = client.get(f"{TASK_URL}/q_does_not_exist", headers=customer_headers())
    assert response.status_code == 200
    assert response.json()["status"] == "not_found"


def test_status_of_a_completed_job_returns_the_result(app_factory, queue: MemoryJobQueue) -> None:
    """跑完之后查状态 → `complete` + 结果（前端据此展示长任务的产物）。

    ⚠️ 任务结果里必须带 `user_id`（`run_report` 的真实结果里有它）：
    `GET /api/tasks/{job_id}` 的越权判定就是拿它与令牌里的 user_id 比。
    这里刻意让假 runner 也返回它 —— 少了它，越权检查会**静默失效**
    （拿不到 owner 就不判），而那正是最不该失效的一处。
    """
    queue.runners[FUNC_REPORT] = queue.make_runner(
        FUNC_REPORT, lambda payload: {"user_id": payload.get("user_id"), "summary": "本月 2 笔订单"}
    )

    with TestClient(app_factory()) as client:
        job_id = client.post(
            TASK_URL, json={"kind": "report", "params": {"title": "完成"}}, headers=customer_headers()
        ).json()["job_id"]
        # 在同一个事件循环之外跑内存队列：这里用 `asyncio.run`（内存队列不依赖循环状态）
        import asyncio

        asyncio.run(queue.run_until_idle())
        body = client.get(f"{TASK_URL}/{job_id}", headers=customer_headers()).json()

    assert body["status"] == "complete"
    assert body["result"]["summary"] == "本月 2 笔订单"


def test_another_user_cannot_read_the_task(client: TestClient) -> None:
    """别人查你的任务 → **404**（不是 403：403 会确认「这个任务存在」）。"""
    job_id = client.post(
        TASK_URL, json={"kind": "report", "params": {"title": "私有"}}, headers=customer_headers()
    ).json()["job_id"]

    intruder = headers_for(make_token(7, "customer"))  # 另一个 user_id、同样是 customer 角色
    response = client.get(f"{TASK_URL}/{job_id}", headers=intruder)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found_or_forbidden"


def test_status_requires_auth(client: TestClient) -> None:
    assert client.get(f"{TASK_URL}/q_x").status_code == 401


# ── 队列现状（**声明顺序**的用例）────────────────────────────────────────────


def test_queue_status_endpoint_is_reachable(client: TestClient) -> None:
    """`GET /api/tasks/queue` 必须命中**队列现状**这个路由。

    ⚠️ 这条用例的价值全在「顺序」上：`/tasks/{job_id}` 若先声明，`queue` 会被当成一个
    job_id → 返回 200 + `status="not_found"`（状态码还是对的！），
    症状极具误导性。所以这里断言的是**响应形状**（有 depth/scale_hint），
    而不是「HTTP 200」。
    """
    response = client.get(f"{TASK_URL}/queue", headers=customer_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert "depth" in body and "scale_hint" in body
    assert "status" not in body  # ← 命中的不是「查某个任务」
    assert body["kinds"] == ["report", "agent", "kb_rebuild"]


def test_queue_status_reflects_the_injected_queue(app_factory, queue: MemoryJobQueue) -> None:
    """队列现状读的是注入队列的真实积压（并把它写进指标）。"""
    from app.api.metrics import MetricsRegistry

    metrics = MetricsRegistry()
    with TestClient(app_factory(queue=queue, metrics=metrics)) as client:
        for index in range(3):
            client.post(TASK_URL, json={"kind": "report", "params": {"n": index}}, headers=customer_headers())
        body = client.get(f"{TASK_URL}/queue", headers=customer_headers()).json()

    assert body["depth"] == 3
    assert body["backlog"] == 3
    assert body["scale_hint"] == "积压正常"
    assert metrics.snapshot()["gauges"]["shopsage_queue_depth"] == 3.0


def test_queue_status_requires_auth(client: TestClient) -> None:
    assert client.get(f"{TASK_URL}/queue").status_code == 401


# ── 限流 ─────────────────────────────────────────────────────────────────────


def test_submitting_tasks_is_rate_limited(app_factory) -> None:
    """提交长任务**必须限流**（而且比 chat 更严：被滥用的代价落在队列上）。

    ⚠️ 这条用例同时钉住一个**三道登记**的坑：`POST /api/tasks` 要出现在
    ① `middleware.LIMITED_SCOPES`（中间件要不要限）、② `scope.SCOPE_BY_ROUTE`
    （用哪个 scope）、③ `ratelimit.DEFAULT_RULES`（默认额度）三处，缺任何一处
    都**不会报错** —— 缺 ① 就是「额度配了但完全不生效」（本仓实测踩到，见 runbook §10.1）。
    """
    limiter = tiny_limiter(chat=2, stream=2, approve=2)
    # `tiny_limiter` 只给三个 chat 相关的 scope 配额度，所以这里显式补一条小的 tasks 规则
    from app.api.ratelimit import RateLimitRule

    limiter.rules["tasks"] = RateLimitRule(limit=2, window_seconds=60.0)

    with TestClient(app_factory(rate_limiter=limiter)) as client:
        codes = [
            client.post(
                TASK_URL, json={"kind": "report", "params": {"n": index}}, headers=customer_headers()
            ).status_code
            for index in range(4)
        ]
    assert codes[:2] == [202, 202]
    assert codes[2:] == [429, 429]


def test_the_task_endpoint_is_registered_for_rate_limiting() -> None:
    """三道登记缺一不可（断言比上面那条更强：它检查**常量本身**）。"""
    from app.api.middleware import LIMITED_SCOPES
    from app.api.ratelimit import DEFAULT_RULES
    from app.api.scope import SCOPE_BY_ROUTE

    assert ("POST", "/api/tasks") in LIMITED_SCOPES
    assert SCOPE_BY_ROUTE[("POST", "/api/tasks")] == "tasks"
    assert "tasks" in DEFAULT_RULES


def test_default_task_quota_is_stricter_than_chat() -> None:
    """默认额度表里 `tasks` 比 `chat` 严（改额度时不要把这条口径改反）。"""
    from app.api.ratelimit import DEFAULT_RULES

    assert "tasks" in DEFAULT_RULES
    assert DEFAULT_RULES["tasks"].limit < DEFAULT_RULES["chat"].limit


@pytest.mark.parametrize("kind", ["report", "agent", "kb_rebuild"])
def test_every_kind_is_accepted_by_the_schema(kind: str) -> None:
    """三种任务都在 `TaskRequest.kind` 的取值域里（契约与实现不能漂移）。"""
    from app.api.schemas import TaskRequest
    from app.queue.jobs import JOB_KINDS

    assert kind in JOB_KINDS
    model = TaskRequest(kind=kind)  # type: ignore[arg-type]
    assert model.kind == kind
