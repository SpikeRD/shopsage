"""`/api/refunds` 的用例（变更 003 / 需求 2+3）：**客户申请 → 员工审批**的闭环。

四条判据：

| 判据 | 防的是什么 |
|---|---|
| 客户能申请，产出 `requested` | 需求 2 的「可以自己申请」 |
| 客户**批不了**（404） | 需求 2 的「不能自己审批」（权限点分离） |
| 员工能查、能批，重复批 409 | 需求 3 的审批台与状态机 |
| 审批留痕真落库 | 资金动作必须可审计到人 |

⚠️ 这些用例要真库（`@pytest.mark.db`）：幂等靠**数据库唯一约束**、
额度靠行锁，两者都不是内存假实现能证明的。

⚠️ 用例造的退款行统一用 `test-tools-` 前缀的幂等键，前后按前缀清理
（`tests/tools_helpers.cleanup_tool_rows`）—— 演示库不该被测试数据堆乱。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import headers_for, make_token
from tests.tools_helpers import cleanup_tool_rows, find_refundable_order

pytestmark = pytest.mark.db


@pytest.fixture
def clean_writes():
    """本套件会真的插退款行（会占订单额度），所以前后都按幂等键前缀清掉。"""
    cleanup_tool_rows()
    yield
    cleanup_tool_rows()


@pytest.fixture
def refundable_order():
    """挑一笔**属于演示客户 2 或 3** 的可退订单。

    ⚠️ 必须限定主体：`DEMO_ACCOUNTS` 里只有客户 2 与客户 3，
    而 `make_token(4, "customer")` 会**回退**到某个存在的客户账号（实测回退到 3），
    于是「用订单 4 的 owner（user 4）签的令牌」实际上是客户 3 的令牌 ——
    请求被归属校验拦下（404），而失败原因与用例名字完全对不上。
    这类「令牌主体与订单主体不一致」的坑只有真库用例会撞到，所以在这里显式限定。
    """
    for candidate in (2, 3):
        try:
            return find_refundable_order(user_id=candidate)
        except LookupError:
            continue
    pytest.skip("演示库里客户 2/3 都没有「可退且还有额度」的订单：先跑 uv run python -m scripts.seed")


def _request(
    client: TestClient,
    *,
    user_id: int,
    order_id: int,
    amount: str,
    key: str,
    reason: str | None = "我要退款",
) -> Any:
    body: dict[str, Any] = {"order_id": order_id, "amount": amount, "idempotency_key": key}
    if reason is not None:
        body["reason"] = reason
    return client.post("/api/refunds", json=body, headers=headers_for(make_token(user_id, "customer")))


def _decide(client: TestClient, *, role: str, refund_id: int, decision: str, note: str | None = None) -> Any:
    body: dict[str, Any] = {"refund_id": refund_id, "decision": decision}
    if note is not None:
        body["note"] = note
    return client.post("/api/refunds/decision", json=body, headers=headers_for(make_token(2, role)))


# ── 客户申请 ─────────────────────────────────────────────────────────────────


def test_customer_can_request_a_refund(client: TestClient, clean_writes, refundable_order, seeded) -> None:
    """★需求 2 的正常路径：客户提交申请 → 201 + `requested`（等员工审批）。"""
    order_id, user_id, _total, _remaining = refundable_order
    response = _request(
        client, user_id=user_id, order_id=order_id, amount="1.00", key="test-tools-api-req-1"
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "requested", "客户申请只能产出 requested（等员工审批）"
    assert body["order_id"] == order_id
    assert body["amount"] == "1.00"
    assert body["reason"] == "我要退款"
    # 还没人审批，四个留痕字段都该是空的
    assert body["decided_by"] is None
    assert body["decided_at"] is None
    assert body["decision_note"] is None
    assert body["approver_role"] is None


def test_request_is_idempotent(client: TestClient, clean_writes, refundable_order, seeded) -> None:
    """同一个幂等键重复提交 → 同一张单（不产生第二行、不多占额度）。"""
    order_id, user_id, _total, _remaining = refundable_order
    key = "test-tools-api-req-idem"
    first = _request(client, user_id=user_id, order_id=order_id, amount="1.00", key=key)
    second = _request(client, user_id=user_id, order_id=order_id, amount="1.00", key=key)
    assert first.status_code == second.status_code == 201
    assert first.json()["refund_id"] == second.json()["refund_id"], "幂等命中必须返回同一张单"


def test_request_over_quota_is_rejected(client: TestClient, clean_writes, refundable_order, seeded) -> None:
    """★金额超过剩余可退额度 → **400**（不是 500）：这是调用方可以改的参数问题。"""
    order_id, user_id, _total, remaining = refundable_order
    too_much = str((remaining + Decimal("100.00")).quantize(Decimal("0.01")))
    response = _request(
        client, user_id=user_id, order_id=order_id, amount=too_much, key="test-tools-api-req-over"
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "refund_not_allowed"
    assert "剩余可退额度" in response.json()["error"]["message"]


def test_request_on_non_refundable_order_is_rejected(
    client: TestClient, clean_writes, seeded
) -> None:
    """订单状态不可退（例如 `pending`）→ 400 且原因可读。"""
    from sqlalchemy import select

    from app.db.models import Order
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        row = db.execute(
            select(Order.order_id, Order.user_id)
            .where(Order.status.notin_(("shipped", "delivered")))
            # ⚠️ 限定在演示客户 2/3 身上（理由见 `refundable_order` 装置）
            .where(Order.user_id.in_((2, 3)))
            .limit(1)
        ).one_or_none()
    if row is None:
        pytest.skip("演示数据里客户 2/3 没有「不可退状态」的订单（先跑 scripts.seed）")

    response = _request(
        client,
        user_id=int(row.user_id),
        order_id=int(row.order_id),
        amount="1.00",
        key="test-tools-api-req-badstate",
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "refund_not_allowed"


def test_request_requires_request_perm_not_write_perm(
    client: TestClient, clean_writes, refundable_order, seeded
) -> None:
    """★权限点判据：客户持 `request:refund` 就能申请（**不需要** `write:refund`）。

    这条把「申请」与「代客户发起」两个权限点的分离钉在 HTTP 层：
    客户没有 `write:refund`，但他的申请照样成功（201）。
    """
    from app.security.rbac import PERM_REQUEST_REFUND, PERM_WRITE_REFUND, permissions_for

    customer = permissions_for("customer")
    assert PERM_REQUEST_REFUND in customer and PERM_WRITE_REFUND not in customer

    order_id, user_id, _total, _remaining = refundable_order
    response = _request(
        client, user_id=user_id, order_id=order_id, amount="1.00", key="test-tools-api-req-perm"
    )
    assert response.status_code == 201, response.text


def test_request_body_rejects_extra_fields(client: TestClient) -> None:
    """请求体里塞 `user_id` / `role` 之类的自报身份字段 → 422（`extra="forbid"`）。

    这是「身份只能来自令牌」在契约层的落点：多写一个字段就拒，而不是静默忽略 ——
    静默忽略会让调用方以为「我指定了归属」。
    """
    response = client.post(
        "/api/refunds",
        json={"order_id": 1, "amount": "1.00", "idempotency_key": "k", "user_id": 999},
        headers=headers_for(make_token(2, "customer")),
    )
    assert response.status_code == 422, response.text


# ── 员工查询 ─────────────────────────────────────────────────────────────────


def test_staff_can_list_and_filter(client: TestClient, clean_writes, refundable_order, seeded) -> None:
    """★需求 3 的查询：员工能列出退款单，并按单号/订单号/客户/状态筛出子集。"""
    order_id, user_id, _total, _remaining = refundable_order
    created = _request(
        client, user_id=user_id, order_id=order_id, amount="1.00", key="test-tools-api-list-1"
    ).json()
    refund_id = int(created["refund_id"])
    staff = headers_for(make_token(2, "customer_service"))

    listed = client.get("/api/refunds", headers=staff)
    assert listed.status_code == 200, listed.text
    ids = {int(item["refund_id"]) for item in listed.json()["refunds"]}
    assert refund_id in ids, "刚建的退款单应当出现在员工的列表里"

    by_id = client.get(f"/api/refunds?refund_id={refund_id}", headers=staff)
    assert [int(x["refund_id"]) for x in by_id.json()["refunds"]] == [refund_id]

    by_order = client.get(f"/api/refunds?order_id={order_id}", headers=staff)
    assert refund_id in {int(x["refund_id"]) for x in by_order.json()["refunds"]}

    by_user = client.get(f"/api/refunds?user_id={user_id}", headers=staff)
    assert all(int(x["user_id"]) == user_id for x in by_user.json()["refunds"])

    by_status = client.get("/api/refunds?status=requested", headers=staff)
    assert refund_id in {int(x["refund_id"]) for x in by_status.json()["refunds"]}
    other_status = client.get("/api/refunds?status=rejected", headers=staff)
    assert refund_id not in {int(x["refund_id"]) for x in other_status.json()["refunds"]}


def test_invalid_time_filter_is_422_not_silently_ignored(client: TestClient, patched_query) -> None:
    """时间筛选取值非法 → 422 且说清格式。

    **静默忽略是最坏的选择**：用户会以为自己筛过了，看到的其实是全部数据。
    ⚠️ 时间由**路由**解析（`_parse_time`），所以它走的是 422（请求参数不合规）——
       与 `status` 不同（那个在服务层校验，见下一条）。
    """
    response = client.get(
        "/api/refunds?created_from=昨天", headers=headers_for(make_token(2, "customer_service"))
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_time_filter"


def test_invalid_status_filter_is_rejected_not_ignored(client: TestClient, patched_query) -> None:
    """非法 `status` 取值 → **400**（`tool_contract`）且**不静默忽略那次筛选**。

    ⚠️ 与 `created_from` 的 422 不同，这里是 400 —— 差异不是随意的：
      * 时间**在路由层**解析（`_parse_time`），形状不对属于「请求参数不合规」→ 422；
      * `status` 由**服务层**按 `REFUND_STATUSES` 校验（那是业务枚举的真相），
        抛 `ToolContractError`，全局处理器把它映射成 400 `tool_contract`。
    两者都**拒绝**而不是忽略，这才是关键判据；状态码只是各自层级的自然结果。
    """
    response = client.get(
        "/api/refunds?status=nonsense", headers=headers_for(make_token(2, "customer_service"))
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "tool_contract"
    assert "nonsense" in response.json()["error"]["message"], "报错要点名是哪个取值不合法"


# ── 员工审批 ─────────────────────────────────────────────────────────────────


def test_staff_can_approve_and_audit_is_recorded(
    client: TestClient, clean_writes, refundable_order, seeded
) -> None:
    """★需求 3 的正常路径：员工通过 → `approved`，且四样留痕都落库。"""
    order_id, user_id, _total, _remaining = refundable_order
    created = _request(
        client, user_id=user_id, order_id=order_id, amount="1.00", key="test-tools-api-dec-ok"
    ).json()
    refund_id = int(created["refund_id"])

    response = _decide(
        client, role="customer_service", refund_id=refund_id, decision="approve", note="已核实物流签收"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["decided_by"] is not None, "审批人必须落库（decided_by = employees.emp_id）"
    assert body["decided_at"] is not None
    assert body["decision_note"] == "已核实物流签收"
    assert body["approver_role"] == "customer_service", "审批时的角色要留**快照**"
    # 客户填的原始理由不能被审批意见覆盖（两者是不同的字段）
    assert body["reason"] == "我要退款"


def test_staff_can_reject(client: TestClient, clean_writes, refundable_order, seeded) -> None:
    """驳回 → `rejected`，并且这笔钱**不再占额度**（客户可以重新申请）。"""
    order_id, user_id, _total, _remaining = refundable_order
    created = _request(
        client, user_id=user_id, order_id=order_id, amount="1.00", key="test-tools-api-dec-rej"
    ).json()
    response = _decide(
        client, role="customer_service", refund_id=int(created["refund_id"]), decision="reject", note="不符合条件"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"

    from app.tools.refund import remaining_refundable

    # 驳回后额度回到申请前的水平（说明 rejected 不计入占用）
    after = remaining_refundable(user_id, order_id)
    assert after == _remaining, f"驳回后额度应当是 {_remaining}，实际 {after}"


def test_decide_twice_is_409_with_current_status(
    client: TestClient, clean_writes, refundable_order, seeded
) -> None:
    """★重复审批 → **409** 且带上当前状态（前端据此刷新，而不是再点一次）。"""
    order_id, user_id, _total, _remaining = refundable_order
    created = _request(
        client, user_id=user_id, order_id=order_id, amount="1.00", key="test-tools-api-dec-twice"
    ).json()
    refund_id = int(created["refund_id"])

    assert _decide(client, role="customer_service", refund_id=refund_id, decision="approve").status_code == 200
    again = _decide(client, role="customer_service", refund_id=refund_id, decision="reject")

    assert again.status_code == 409, again.text
    body = again.json()
    assert body["error"]["code"] == "refund_not_decidable"
    assert body["error"]["detail"]["current_status"] == "approved", (
        "409 必须带上当前状态：只给一句「操作失败」会让用户再点一次、再撞一次"
    )


def test_decide_missing_refund_is_400(client: TestClient, patched_query) -> None:
    """退款单不存在 → 400（契约问题），而不是 500。"""
    response = _decide(client, role="customer_service", refund_id=999_999, decision="approve")
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "tool_contract"


def test_decide_invalid_decision_is_422(client: TestClient, patched_query) -> None:
    """`decision` 只接受 approve/reject → 其它取值 422（pydantic Literal）。"""
    response = client.post(
        "/api/refunds/decision",
        json={"refund_id": 1, "decision": "maybe"},
        headers=headers_for(make_token(2, "customer_service")),
    )
    assert response.status_code == 422, response.text


def test_decide_body_rejects_self_declared_approver(client: TestClient, patched_query) -> None:
    """★审批人只能来自令牌：请求体里塞 `decided_by` / `approver_role` → 422。

    这条防的是「伪造审批人」：如果请求体能指定 `decided_by`，
    那任何人都能把一笔退款记成「主管批的」。
    """
    response = client.post(
        "/api/refunds/decision",
        json={"refund_id": 1, "decision": "approve", "decided_by": 1, "approver_role": "manager"},
        headers=headers_for(make_token(2, "customer_service")),
    )
    assert response.status_code == 422, response.text


def test_refunds_require_a_token(client: TestClient) -> None:
    """没有令牌 → 401。"""
    assert client.get("/api/refunds").status_code == 401
    assert client.post("/api/refunds", json={}).status_code in (401, 422)
