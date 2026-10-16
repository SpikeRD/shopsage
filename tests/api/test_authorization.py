"""越权与「谁有权审批」：`9.api.md` §7 的 `test_customer_cannot_see_other`（D1，端到端）。

`9.api.md` §8 的验收里有一条「越权请求端到端被拦截（D1）」。本文件从**四个方向**验它，
因为越权从来不只有一个入口：

| 方向 | 用例 | 拦在哪 |
|---|---|---|
| 查别人的数据 | `test_customer_cannot_query_another_users_order` | 图里 `data` 节点的行级过滤 + 权限点 |
| 用别人的身份 | `test_identity_comes_from_the_token_not_the_body` | pydantic `extra="forbid"` |
| **批准自己的退款** | `test_customer_cannot_decide_their_own_refund`（变更 003） | `approve:refund` 不在客户权限集里 |
| **提交别人的退款** | `test_customer_cannot_request_refund_for_another_user`（变更 003） | 服务层的订单归属校验 |

## ⚠️ 变更 003 删掉了本节原来的两节用例（共 6 条）

原来这里覆盖的是**对话轮级**人机审批：挂起（202）→ 批准/拒绝（200）→ 重复批准（409），
以及 `test_thread_id_from_another_user_is_rejected` / `test_approve_of_a_session_..._is_409`。

它们被删除的原因是**接口没了**：`POST /api/chat/approve` 随变更 003 一起移除，
因为它的批准人就是发起人（自审批）—— 那正是需求 2 要修的「对客入口可以自己审批退款申请」。

替代它的判据在**本文件的第三节**（单据级审批）与 `tests/api/test_refunds_api.py`：
审批对象从「一轮对话」变成「一笔有 id、有金额、有状态机的退款单」，
批准人必须是持有 `approve:refund` 的员工。

⚠️ 「图能不能真的停下来」这条**框架能力**没有被丢弃：它仍在
`tests/graph/test_graph_interrupt.py` 里用显式构造的 `InterruptPolicy` 验证
（变更 003 决策 D2：保留原语、只关死生产默认）。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

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


# ── 变更 003：客户不能批准自己的退款（需求 2 的核心判据）────────────────────


def test_customer_cannot_decide_their_own_refund(client: TestClient, patched_query) -> None:
    """★客户**不能**审批任何退款单 —— 包括他自己刚提交的那一张。

    这是需求 2 的原始描述「对客入口现在可以自己审批退款申请，明显有问题」的回归判据。

    变更 003 之前，「自审批」是通过**对话轮闸门**实现的：客户发起敏感操作 → 图挂起 →
    客户在同一个页面上点「批准并继续」→ `POST /api/chat/approve` 放行。
    那条路的批准人就是发起人，而系统把「批过了」如实渲染给用户，制造「已经办好了」的错觉。

    现在：审批是一个**单据级**动作，要 `approve:refund`，而客户只有 `request:refund`。
    本用例从**两个方向**钉住它：
      ① 老接口已经不存在（404）—— 没有回头路；
      ② 客户拿自己的令牌去调新的审批口 → 404（`app/api/errors.py` 的越权口径：
         不确认资源是否存在，所以「无权」与「不存在」返回同一个码）。
    """
    headers = headers_for(make_token(2, "customer"))

    # ① 旧的对话轮审批接口必须已经消失
    legacy = client.post(
        "/api/chat/approve",
        json={"thread_id": "c2:whatever", "approved": True},
        headers=headers,
    )
    assert legacy.status_code == 404, (
        "变更 003 已删除 /api/chat/approve（发起人可以批准自己）；"
        "它返回的不是 404 说明它又被加回来了"
    )

    # ② 新的单据级审批口对客户关闭
    attempted = client.post(
        "/api/refunds/decision",
        json={"refund_id": 1, "decision": "approve"},
        headers=headers,
    )
    assert attempted.status_code == 404, attempted.text
    assert attempted.json()["error"]["code"] == "not_found_or_forbidden"


def test_customer_cannot_list_all_refunds(client: TestClient, patched_query) -> None:
    """★客户**不能**查询全站退款列表（那是审批台的读口，要 `approve:refund`）。

    这条与上一条是一对：如果客户能列出全站退款，他就能看到**别人的**退款单
    （金额、订单号、理由）—— 那比「查别人的订单」更敏感，因为退款理由里
    常有客户写的个人信息。
    """
    response = client.get(
        "/api/refunds",
        headers=headers_for(make_token(2, "customer")),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found_or_forbidden"


def test_customer_cannot_request_refund_for_another_user(app_factory, patched_query) -> None:
    """★退款申请的归属只能是**令牌里的那个客户**：换个订单号也拿不到别人的单。

    客户 2 拿着客户 3 的订单号来申请退款 → 服务层的归属校验拦下
    （`_insert_refund_once` 里那条「越权拦截」），路由按 404 处置。

    ⚠️ 请求体里**没有** `user_id` 字段（`extra="forbid"`），所以这条用例唯一能试的
    越权方式就是「换一个订单号」—— 那正是真实攻击者会做的。
    """
    app = app_factory()
    with TestClient(app) as local:
        mine = local.get("/api/orders", headers=headers_for(make_token(2, "customer")))
        assert mine.status_code == 200, mine.text
        my_orders = {int(item["order_id"]) for item in mine.json()["orders"]}
        assert my_orders, "演示数据里客户 2 应该至少有一笔订单（先跑 scripts.seed）"

        other = local.get("/api/orders", headers=headers_for(make_token(3, "customer")))
        assert other.status_code == 200, other.text
        other_only = {int(item["order_id"]) for item in other.json()["orders"]} - my_orders
        assert other_only, "演示数据里客户 3 应该有客户 2 没有的订单"
        victim = sorted(other_only)[0]

        response = local.post(
            "/api/refunds",
            json={
                "order_id": victim,
                "amount": "1.00",
                "idempotency_key": "d1-other-user-order",
            },
            headers=headers_for(make_token(2, "customer")),
        )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found_or_forbidden"


def test_customer_route_is_not_gated_anymore(app_factory, patched_query) -> None:
    """★变更 003 起**对客路径也不再挂起等批准**（自审批的那条路已经拆除）。

    变更 003 之前这条用例检查的是「对客问投诉 → 202 + approval_required」，
    因为闸门对客户开着。现在 `GATED_ROLES` 是空集：任何角色问同一句话都直接答完。

    ⚠️ 这条断言的价值在于把「生产默认已经关死」钉在 **HTTP 层**：
    图层的对应断言在 `tests/graph/test_graph_interrupt.py`。
    两处都要有 —— 只测图层的话，「有人把 `GATED_ROLES` 加回去」在 API 层不会被发现。

    ⚠️ 同时断言内部角色也直接答完：闸门**不是**被搬到了内部角色身上。
    """
    from tests.graph_helpers import AgentFakeLLM

    replies = {
        "router": {"route": "ticket", "certainty": 0.9, "reason": "要投诉/建单"},
        "synthesis": {"answer": "已收到你的反馈，我们会尽快处理。", "cited": []},
        "guardrail": {"allowed": True, "reason": "ok"},
    }
    app = app_factory(llm=AgentFakeLLM(**replies))
    with TestClient(app) as client:
        customer_turn = chat(
            client,
            headers_for(make_token(2, "customer")),
            message="我要投诉物流",
            session_id="no-gate-1",
        )
        internal_turn = chat(
            client,
            headers_for(make_token(7, "internal_employee")),
            message="我要投诉物流",
            session_id="no-gate-2",
        )

    assert customer_turn.status_code == 200, (
        "变更 003 起对客路径不该再挂起等批准；返回 202 说明对话轮闸门又被打开了"
    )
    assert customer_turn.json()["flow_state"] == "delivered"
    assert customer_turn.json()["approval"] is None
    assert internal_turn.status_code == 200
    assert internal_turn.json()["flow_state"] == "delivered"
