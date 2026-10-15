"""越权与人工审批：`9.api.md` §7 的 `test_customer_cannot_see_other`（D1，端到端）。

`9.api.md` §8 的验收里有一条「越权请求端到端被拦截（D1）」。本文件从**四个方向**验它，
因为越权从来不只有一个入口：

| 方向 | 用例 | 拦在哪 |
|---|---|---|
| 查别人的数据 | `test_customer_cannot_query_another_users_order` | 图里 `data` 节点的行级过滤 + 权限点 |
| 读别人的会话 | `test_thread_id_from_another_user_is_rejected` | `deps.restore_session_id` 的主体校验 |
| 批准别人的操作 | `test_approve_requires_the_same_user` | 同上（**两个方向共用同一条校验**） |
| 用别人的身份 | `test_identity_comes_from_the_token_not_the_body` | pydantic `extra="forbid"` |

后两节覆盖 human-in-the-loop：挂起（202）→ 批准/拒绝（200）→ 重复批准（409）。
本步按 `8.graph-runbook.md` §6.5 的挂点**开启**了对客角色的闸门
（`graph/config.py::interrupt_policy_for`，对客开、内部关）。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.graph.config import REJECTED_REASON
from tests.api.conftest import DATA_REPLIES, headers_for, make_token


def chat(client: TestClient, headers: dict[str, str], message: str = "我的订单到哪了", **extra: Any):
    return client.post("/api/chat", json={"message": message, **extra}, headers=headers)


# ── D1：越权查询 ─────────────────────────────────────────────────────────────


def test_customer_cannot_query_another_users_order(app_factory, patched_query, monkeypatch) -> None:
    """客户请求查**别人**的订单：模型给的 `user_id` 活不到执行层。

    构造方式（这是 D1 的真实攻击形态）：让假模型生成一条「查全站订单」的 SQL 与
    `uid=999`（别人）的绑定参数 —— 第 7 步的机制会在**执行前**把绑定参数覆写成
    令牌里的 user_id。本用例断言那个覆写真的发生了（`params["uid"] == 2`），
    并且这一轮仍然正常收口（不是 500）。

    ⚠️ 判定「覆写生效」必须看**执行器收到的参数**，而不是看响应：
    响应里本来就不带 `rows`（`_shape_answer` 只留结论），所以「响应里没有别人的数据」
    是个更弱的断言 —— 两个都写，才能区分「覆写生效」与「碰巧没查」。
    """
    from app.agents import data as data_mod
    from tests.graph_helpers import AgentFakeLLM

    replies = dict(DATA_REPLIES)
    replies["data"] = {
        "sql": "SELECT order_id, user_id FROM orders WHERE user_id = :uid LIMIT 10",
        "params": {"uid": 999, "n": 10},
        "reason": "查全站订单",
    }
    seen: dict[str, Any] = {}

    def capture(sql, params=None, **kwargs):  # noqa: ANN001, ANN202
        seen["sql"] = sql
        seen["params"] = dict(params or {})
        return [], {"row_count": 0, "truncated": False, "audit_id": "tc-covered", "tables": ["orders"]}

    monkeypatch.setattr(data_mod, "execute_readonly_query", capture)
    app = app_factory(llm=AgentFakeLLM(**replies))
    with TestClient(app) as client:
        response = chat(client, headers_for(make_token(2, "customer")), session_id="d1-1")

    assert response.status_code == 200, "越权请求是**预期内**的拒绝，不该变成 500"
    assert seen.get("params", {}).get("uid") == 2, f"绑定参数必须是令牌里的 user_id：{seen}"
    assert "999" not in str(seen.get("params")), f"模型给的 uid 不能活到执行层：{seen}"
    assert response.json()["flow_state"] in {"delivered", "blocked"}


def test_customer_never_sees_another_users_rows(app_factory, patched_query, monkeypatch) -> None:
    """执行层返回的**别人的行**不会原样出网：`row_count` 之外的内容不进响应。

    这条防的是「行级过滤在 SQL 里生效了，但某个环节把 rows 直接塞进响应」——
    那是本仓最容易漏的一种泄漏（`FRAME_SAFE_KEYS` 只挡了 WebSocket 那一条路径）。
    """
    from app.agents import data as data_mod
    from tests.graph_helpers import fake_query_result

    monkeypatch.setattr(
        data_mod,
        "execute_readonly_query",
        fake_query_result([{"order_id": 9001, "user_id": 999, "phone": "13800000000"}]),
    )
    app = app_factory()
    with TestClient(app) as client:
        raw = chat(client, headers_for(make_token(2, "customer")), session_id="d1-2").text

    assert "9001" not in raw, "别人的订单号不该出现在响应里"
    assert "13800000000" not in raw, "PII 不该出现在响应里"


def test_identity_comes_from_the_token_not_the_body(client: TestClient, patched_query) -> None:
    """用别人的身份：请求体里的 `user_id` 会被 422 拒（不是「忽略」）。

    与 WebSocket 的差别见 `test_auth.py::test_websocket_uses_the_token_identity_not_the_body`：
    HTTP 走 pydantic（严格），WS 走自由 JSON（忽略多余字段但不采信）。
    """
    response = chat(
        client,
        headers_for(make_token(2, "customer")),
        session_id="d1-3",
        user_id=999,
    )
    assert response.status_code == 422


# ── 会话（checkpoint）的越权 ─────────────────────────────────────────────────


def test_thread_id_from_another_user_is_rejected(client: TestClient, patched_query) -> None:
    """拿 `c9:*` 的 thread_id 去批准：422（主体校验）。

    这条比「查别人的订单」更严重：checkpoint 里装的是**完整对话状态**，
    能读别人的 thread 就等于读到别人上一轮的原话。
    """
    response = client.post(
        "/api/chat/approve",
        json={"thread_id": "c9:someone-else", "approved": True},
        headers=headers_for(make_token(2, "customer")),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_thread"


def test_approve_of_a_session_without_pending_operation_is_409(client: TestClient, patched_query) -> None:
    """没有挂起却去批准 → 409（而不是「从头重跑一轮」）。

    这条防的是 LangGraph 的语义陷阱：对**没有挂起**的 thread 发 `Command(resume=...)`
    会让图从头重跑一遍 —— 用户会拿到一句莫名其妙的新回答，而上游已经花掉的模型调用再花一遍。
    """
    response = client.post(
        "/api/chat/approve",
        json={"thread_id": "c2:no-pending", "approved": True},
        headers=headers_for(make_token(2, "customer")),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "nothing_to_approve"


# ── human-in-the-loop：挂起 → 批准 / 拒绝 ────────────────────────────────────


#: `ticket` 分支的假模型（对客询问「投诉物流」会被 router 判成 ticket → 触发闸门）
TICKET_REPLIES: dict[str, Any] = {
    "router": {"route": "ticket", "certainty": 0.9, "reason": "要投诉/建单"},
    "synthesis": {"answer": "（这轮不该用到：挂起时 synthesis 不跑）", "cited": []},
    "guardrail": {"allowed": True, "reason": "ok"},
}


def ticket_app(app_factory):
    """一个「对客问投诉就挂起等批准」的 app。"""
    from tests.graph_helpers import AgentFakeLLM

    return app_factory(llm=AgentFakeLLM(**TICKET_REPLIES))


def test_sensitive_route_returns_202_with_approval_gate(app_factory) -> None:
    """对客的敏感操作 → **202 + `approval_required`**，并给出「谁挂起了、问什么」。

    ⚠️ 202 而不是 200：这一轮**没有完成**（它在等人工点头）。
    用 200 会让前端把「等待中」渲染成「已答完」，用户看到一句空回答。
    """
    app = ticket_app(app_factory)
    with TestClient(app) as client:
        response = chat(
            client, headers_for(make_token(2, "customer")), message="我要投诉物流", session_id="hitl-1"
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["flow_state"] == "approval_required"
    gate = body["approval"]
    assert gate is not None
    assert gate["thread_id"] == "c2:hitl-1"
    assert gate["question"].strip(), "必须给审批人一句能读懂的问题"
    assert gate["resume_with"] == "POST /api/chat/approve"


def test_internal_role_skips_the_gate(app_factory, patched_query) -> None:
    """内部角色**不过**闸门（`GATED_ROLES` 只含对客）：同一个问题直接答完。

    这条把「按角色开关闸门」钉住：如果哪天有人把它改成全局开，
    内部员工的每一步分析都会变成一次审批流程（流程倒退，不是安全加固）。
    """
    from tests.graph_helpers import AgentFakeLLM

    replies = dict(DATA_REPLIES)
    app = app_factory(llm=AgentFakeLLM(**replies))
    with TestClient(app) as client:
        response = chat(
            client,
            headers_for(make_token(7, "internal_employee")),
            session_id="hitl-internal",
        )
    assert response.status_code == 200
    assert response.json()["flow_state"] == "delivered"


def test_approve_resumes_the_suspended_turn(app_factory) -> None:
    """批准之后从**断点**接着跑：这一轮完成，且不再是挂起状态。"""
    app = ticket_app(app_factory)
    headers = headers_for(make_token(2, "customer"))
    with TestClient(app) as client:
        first = chat(client, headers, message="我要投诉物流", session_id="hitl-2")
        assert first.status_code == 202

        approved = client.post(
            "/api/chat/approve",
            json={"thread_id": first.json()["approval"]["thread_id"], "approved": True, "reason": "客服已核实"},
            headers=headers,
        )

    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["flow_state"] != "approval_required"
    assert body["thread_id"] == "c2:hitl-2"


def test_reject_gives_an_honest_sentence(app_factory) -> None:
    """拒绝之后**也给一句话**：`REJECTED_REASON`（不是空白、不是 4xx）。

    「未获批准」是一种正常结果，用户必须知道「这件事没做」，而不是以为做完了。
    """
    app = ticket_app(app_factory)
    headers = headers_for(make_token(2, "customer"))
    with TestClient(app) as client:
        first = chat(client, headers, message="我要投诉物流", session_id="hitl-3")
        rejected = client.post(
            "/api/chat/approve",
            json={"thread_id": first.json()["approval"]["thread_id"], "approved": False},
            headers=headers,
        )

    assert rejected.status_code == 200
    body = rejected.json()
    assert body["answer"].strip(), "拒绝也必须有话术"
    assert body["flow_state"] == "blocked"
    assert REJECTED_REASON in body["answer"] or REJECTED_REASON in " ".join(body["refusals"])


def test_approve_twice_is_conflict_not_a_rerun(app_factory) -> None:
    """重复批准 → 409（`nothing_to_approve`），**不会**把图从头再跑一遍。"""
    app = ticket_app(app_factory)
    headers = headers_for(make_token(2, "customer"))
    with TestClient(app) as client:
        first = chat(client, headers, message="我要投诉物流", session_id="hitl-4")
        thread_id = first.json()["approval"]["thread_id"]
        assert client.post(
            "/api/chat/approve", json={"thread_id": thread_id, "approved": True}, headers=headers
        ).status_code == 200
        again = client.post(
            "/api/chat/approve", json={"thread_id": thread_id, "approved": True}, headers=headers
        )

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "nothing_to_approve"
