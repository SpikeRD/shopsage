"""`GET /api/orders` 的用例（变更 003 / 需求 2）：**行级过滤 + 脱敏 + 剩余可退额度**。

三条判据，每条对应一类真实故障：

| 判据 | 防的是什么 |
|---|---|
| **只返回自己的订单** | 行级过滤失效（D1 最经典的形态） |
| **「不是我的单」与「不存在」不可区分** | 用状态码差异枚举订单号是否存在 |
| **脱敏字段按角色裁剪** | 出口漏了一处脱敏（`payment_method` 是财务级） |

⚠️ 这些用例要真库（`@pytest.mark.db`）：行级过滤的正确性**只能在 SQL 层证明** ——
用假执行器的话，「有没有带 WHERE user_id」这件事根本不会被验证。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import headers_for, make_token

pytestmark = pytest.mark.db


def orders_of(client: TestClient, user_id: int) -> list[dict[str, Any]]:
    response = client.get("/api/orders", headers=headers_for(make_token(user_id, "customer")))
    assert response.status_code == 200, response.text
    return list(response.json()["orders"])


def test_orders_are_row_filtered(client: TestClient, patched_query, seeded) -> None:
    """★两个客户的订单集合**没有交集** —— 行级过滤生效的最直接证据。"""
    mine = orders_of(client, 2)
    theirs = orders_of(client, 3)
    assert mine, "演示数据里客户 2 应该有订单（先跑 uv run python -m scripts.seed）"
    assert theirs, "演示数据里客户 3 应该有订单"

    mine_ids = {int(item["order_id"]) for item in mine}
    theirs_ids = {int(item["order_id"]) for item in theirs}
    assert not (mine_ids & theirs_ids), f"越权：两个客户看到了同一批订单 {mine_ids & theirs_ids}"


def test_order_detail_of_another_user_is_404(client: TestClient, patched_query, seeded) -> None:
    """★别人的订单号 → 404（**与「不存在」同码同话术**）。

    这条同时验两件事：
      ① 拦住了（功能正确）；
      ② 用的是 404 而不是 403（**信息正确**）——403 会确认「这个订单号存在」，
         攻击者据此可以枚举真实订单号（与 `app/api/errors.py` 的口径一致）。
    """
    theirs = orders_of(client, 3)
    victim = int(theirs[0]["order_id"])
    headers = headers_for(make_token(2, "customer"))

    forbidden = client.get(f"/api/orders/{victim}", headers=headers)
    missing = client.get("/api/orders/99999999", headers=headers)

    assert forbidden.status_code == 404, forbidden.text
    assert missing.status_code == 404, missing.text
    assert forbidden.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found"
    assert forbidden.json()["error"]["message"] == missing.json()["error"]["message"], (
        "两种原因的文案必须一致：文案不同等于告诉攻击者「这个订单号是真的」"
    )


def test_order_view_carries_remaining_refundable(client: TestClient, patched_query, seeded) -> None:
    """订单页必须给出「还剩多少可退」—— 那是客户申请退款时的默认金额。

    口径：`remaining = total - 已占用（不含 rejected）`。这里断言的是
    **算术自洽**（三者能对上），而不是某个具体数字（种子数据会变）。
    """
    from decimal import Decimal

    mine = orders_of(client, 2)
    assert mine
    for item in mine:
        total = Decimal(item["total_amount"])
        consumed = Decimal(item["consumed_amount"])
        remaining = Decimal(item["remaining_refundable"])
        assert remaining == total - consumed, f"订单 {item['order_id']} 的额度算术不自洽"
        assert remaining >= Decimal("0.00")
        # `refundable` 是派生布尔：额度为 0 时必须是 False（否则界面给一个必然失败的按钮）
        if remaining == Decimal("0.00"):
            assert item["refundable"] is False


def test_payment_method_is_masked_for_customer(client: TestClient, patched_query, seeded) -> None:
    """★`payment_method` 是**财务级**字段：客户看到的是 `***` 而不是明文。

    这是变更 003 决策 D1 的另一半：金额（`total_amount`）**不在** `FIELD_RULES` 里，
    所以它明文可见（那是客户自己的订单，且订单页的核心用途就是「还剩多少可退」）；
    而 `payment_method` 登记为财务级，客户看不到。

    ⚠️ 断言「是 `***` 而不是 None」很关键：`None` 的语义是「这笔订单没有支付方式」，
       `***` 的语义才是「有，但你看不到」。把两者混起来，界面就没法如实展示。
    """
    mine = orders_of(client, 2)
    assert mine
    masked = [item for item in mine if item["payment_method"] is not None]
    assert masked, "演示数据里订单应该有支付方式（否则这条用例没验到东西）"
    for item in masked:
        assert item["payment_method"] == "***", (
            f"客户看到了支付方式 {item['payment_method']!r}：它是财务级字段（FIELD_RULES）"
        )


def test_masking_levels_actually_vary_by_role(patched_query, seeded) -> None:
    """反向对照：脱敏**按角色等级裁剪**，而不是一刀切抹掉。

    只测「客户看不到某个字段」是不够的 —— 把脱敏实现成「永远抹掉所有 PII」
    也能让上一条通过。这里用 `name`（等级 2「内部可见」）做对照：
    客户（上限 1）看不到，`manager`（上限 2）能看到打码后的值。

    ⚠️ 为什么不用 `payment_method` 做对照（我一开始就是这么写的，实测发现是错的）：
    `FIELD_RULES["payment_method"]` 登记的脱敏函数是 `mask_bank_card`，而真实值是
    `alipay` / `wechat` / `card` / `cod` —— 里面**一个数字都没有**，于是
    `mask_bank_card` 走「位数不足 → 整体打码」那条分支，对**所有角色**都返回 `***`。
    实测确认（六个角色全是 `***`），这是既有脱敏表的一处缺陷，已记入 `issues/`，
    **不在本次范围内顺手改**：本次只保证「不改脱敏规则表」（变更 003 的决策 D1）。
    所以对照必须选一个等级差异**真的看得见**的字段。
    """
    from app.security.masking import REDACTED, mask_pii

    row = {"name": "张三", "phone": "13812341234"}

    # ⚠️ `mask_pii` 返回 `MaskResult`（不是 list）：要取 `.rows[0]`。
    as_customer = mask_pii([dict(row)], role="customer").rows[0]  # 上限 1：name 是等级 2 → 抹掉
    as_manager = mask_pii([dict(row)], role="manager").rows[0]  # 上限 2：name 等级 2 → 打码

    assert as_customer["name"] == REDACTED, "客户（上限 1）不该看到等级 2 的姓名字段"
    assert as_manager["name"] not in (REDACTED, "张三"), (
        "manager（上限 2）应当看到**打码后**的姓名（张*），而不是明文也不是整值抹掉"
    )
    assert as_manager["name"].endswith("*"), f"打码形态不对：{as_manager['name']!r}"
    # 等级 1（脱敏级）对两者都是「按脱敏函数处理」，所以都能看到打码手机号
    assert as_customer["phone"] == "138****1234"
    assert as_manager["phone"] == "138****1234"


def test_employee_gets_a_clear_error_not_an_empty_list(client: TestClient, patched_query) -> None:
    """★员工主体（`user_id=0`）调对客订单口 → **400 且说清原因**，不是空列表。

    空列表看起来像「你没有订单」，真因却是「这个身份根本不该用这个口」。
    把接线/路径错误伪装成「没有数据」是最难查的一类问题。
    """
    response = client.get("/api/orders", headers=headers_for(make_token(7, "internal_employee")))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "no_customer_identity"


def test_orders_require_a_token(client: TestClient) -> None:
    """没有令牌 → 401（鉴权是准入条件，不是「返回空列表」）。"""
    assert client.get("/api/orders").status_code == 401
