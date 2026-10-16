"""入口权限贯通（变更 003 / 需求 1+3）：**同一套令牌能驱动它该能进的每个入口**。

## 这一组用例在验什么（以及为什么不能只在前端验）

需求 1 的现场是「管理员能看到对客/对内入口，但没有权限操作」。
根因不是后端缺权限（`admin` 本来就有全部权限点），而是**登录只把令牌写进一个入口钥匙**，
而前端每个页面只读自己那把钥匙（`web/src/lib/session.ts`）。

所以真正的判据分两半，本文件验**后端那一半**：

| 判据 | 在哪验 | 验的是什么 |
|---|---|---|
| 同一套令牌能驱动三个入口的接口 | **本文件** | 后端的**授权**对三个入口都成立 |
| 登录把令牌写进多把钥匙 | `web/tests/session.test.ts` | 前端的**存储**行为 |

两半缺一不可：只验前端会漏掉「后端其实不放行」；只验后端会漏掉
「页面根本没认出这个身份」（那正是需求 1 的现场）。

## ⚠️ 一个必须写清的边界：客服「能进对内」≠「能查全站」

变更 003 给 `customer_service` 加了 `approve:refund`，并把「可进对内入口」的判据
放宽为 `read:all_orders` **或** `approve:refund`。所以客服会看到对内入口 ——
但他查全站订单依然会被拒（他没有 `read:all_orders`）。
`test_customer_service_enters_internal_but_cannot_query_all` 把这两件事**分别**钉住。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.security.accounts import ENTRY_ADMIN, ENTRY_CUSTOMER, ENTRY_INTERNAL, entry_for_role
from app.security.rbac import (
    PERM_APPROVE_REFUND,
    PERM_READ_ALL_ORDERS,
    PERM_REQUEST_REFUND,
    can_enter_internal,
    permissions_for,
)
from tests.api.conftest import headers_for, make_token


def test_admin_holds_every_permission() -> None:
    """★需求 1 的**根因证据**：管理员在后端**从来不缺权限**。

    变更 003 之前现场报的是「管理员没有权限操作」，容易让人去改 RBAC ——
    而 `admin` 早就持有全部权限点（含 `read:all_orders`、`read:finance`）。
    真正的问题在**前端登录态写入**（只写了一把钥匙）。

    这条用例把「后端不欠权限」钉死，避免下一个人再把改动落在错误的位置。
    """
    from app.security.rbac import PERM_ADMIN

    admin = permissions_for("admin")
    for perm in (
        "read:own",
        PERM_READ_ALL_ORDERS,
        "read:finance",
        "write:refund",
        "write:ticket",
        PERM_REQUEST_REFUND,
        PERM_APPROVE_REFUND,
        PERM_ADMIN,
    ):
        assert perm in admin, f"admin 缺 {perm}：需求 1 就真的变成权限问题了"
    assert entry_for_role("admin") == ENTRY_ADMIN


def test_admin_token_drives_all_three_entries(client: TestClient, patched_query, seeded) -> None:
    """★需求 1 的验收：**同一枚管理员令牌**能驱动三个入口的业务接口。

    三条断言分别对应「对客入口」「对内入口」「管理台」各自的核心接口：
      * `/api/auth/me`（三个页面打开时都会调它认身份）；
      * `GET /api/orders`（对客订单页 —— 管理员也会遇到 `user_id=0` 那条分支）；
      * `GET /api/refunds`（对内退款审批台）；
      * `GET /api/admin/accounts`（管理台）。

    ⚠️ 这里断言的是「**授权**成立」，而不是「每个都返回 200」：
    管理员是对客入口时 `user_id=0`，订单口会返回 400（`no_customer_identity`）——
    那**也是**正确行为（员工没有自己的订单）。关键是它**不是** 401/404：
    401 意味着令牌不被这个入口接受（那才是需求 1 的故障），404 意味着权限不够。
    """
    headers = headers_for(make_token(1, "admin"))

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["role"] == "admin"
    assert me.json()["entry"] == ENTRY_ADMIN

    # 管理台：必须 200（它是管理员的默认入口）
    accounts = client.get("/api/admin/accounts", headers=headers)
    assert accounts.status_code == 200, accounts.text

    # 对内退款审批台：必须 200（管理员持有 approve:refund）
    refunds = client.get("/api/refunds", headers=headers)
    assert refunds.status_code == 200, refunds.text

    # 对客订单口：授权成立，但管理员没有「自己的订单」→ 400 且原因明确
    orders = client.get("/api/orders", headers=headers)
    assert orders.status_code == 400, orders.text
    assert orders.json()["error"]["code"] == "no_customer_identity"
    assert orders.status_code not in (401, 404), "401/404 才说明这个入口不接受管理员的令牌"


def test_customer_service_enters_internal_but_cannot_query_all(
    client: TestClient, patched_query, seeded
) -> None:
    """★变更 003 决策 15 的两面：客服**能进对内入口**，但**查不到全站订单**。

    第一面（能进）：`entry_for_role("customer_service") == internal`
    —— 否则他登录后被送进对客入口，看不到自己的审批台（需求 3 对客服不可用）。

    第二面（查不到）：他没有 `read:all_orders`，所以对内页的查询框必须对他关闭。
    ⚠️ 这两件事必须**分别**成立。把它们合成一个判据（例如让前端用
    `can_enter_internal` 去决定查询框能不能用）会造出一个更难查的问题：
    客服看到一个**可用的**查询框，而每次提交都被后端拒。
    """
    # 第一面：入口
    assert entry_for_role("customer_service") == ENTRY_INTERNAL
    assert can_enter_internal("customer_service") is True

    # 第二面：查全站的权限**没有**给
    assert PERM_READ_ALL_ORDERS not in permissions_for("customer_service"), (
        "客服拿到了 read:all_orders —— 那他就真能查全站订单了，"
        "而设计上他只该看退款单据"
    )

    # 第三面：行为上确实如此。`/api/auth/me` 会回服务端算好的 entry 与权限点
    me = client.get("/api/auth/me", headers=headers_for(make_token(2, "customer_service")))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["entry"] == ENTRY_INTERNAL, "客服的入口必须是对内（否则看不到审批台）"
    assert PERM_APPROVE_REFUND in body["permissions"]
    assert PERM_READ_ALL_ORDERS not in body["permissions"]

    # 第四面：他能用审批台（这是他进对内的**唯一**理由）
    assert client.get("/api/refunds", headers=headers_for(make_token(2, "customer_service"))).status_code == 200


def test_customer_entry_is_unchanged(client: TestClient, patched_query, seeded) -> None:
    """对客角色的入口与权限**没有被变更 003 顺手放宽**。

    变更 003 的三条需求都不该影响普通客户：他仍然只进对客入口、
    仍然没有审批权、仍然读不到全站订单。
    """
    assert entry_for_role("customer") == ENTRY_CUSTOMER
    assert can_enter_internal("customer") is False

    me = client.get("/api/auth/me", headers=headers_for(make_token(2, "customer")))
    assert me.json()["entry"] == ENTRY_CUSTOMER
    assert PERM_APPROVE_REFUND not in me.json()["permissions"]
    assert PERM_REQUEST_REFUND in me.json()["permissions"], "需求 2 要客户能申请退款"


def test_internal_employee_entry_is_unchanged(client: TestClient, patched_query, seeded) -> None:
    """普通员工的入口不变（对内），且现在多了审批权。"""
    assert entry_for_role("internal_employee") == ENTRY_INTERNAL
    me = client.get("/api/auth/me", headers=headers_for(make_token(3, "internal_employee")))
    assert me.status_code == 200, me.text
    assert me.json()["entry"] == ENTRY_INTERNAL
    assert PERM_APPROVE_REFUND in me.json()["permissions"]


def test_unknown_role_still_gets_the_customer_entry() -> None:
    """未登记角色 = 零权限 → 只能进对客入口（**默认拒绝**在变更 003 之后仍然成立）。"""
    assert entry_for_role("nobody") == ENTRY_CUSTOMER
    assert can_enter_internal("nobody") is False
