"""`refund` 图节点的用例（变更 003 / 需求 2）：**一句话 → 一条 `requested` 退款单**。

这个节点是本仓第一个真正的**写节点**，所以用例的重点是四类边界：

| 判据 | 防的是什么 |
|---|---|
| 唯一可退订单 → 落库 | 需求 2 的正常路径（LLM 能真的提交退款） |
| 多笔可退 → **不落库**、追问 | 猜错订单（客户想退 A，系统退了 B） |
| **重放两次只落一单** | 幂等键不确定 → 客户被多退一笔 |
| 无可退 / 不是他的单 / 状态不可退 | 如实说明而不是 500，也不是含糊的「办不了」 |

⚠️ 全部走真库（`@pytest.mark.db`）：幂等靠数据库唯一约束，
用假实现根本测不出「重放是否真的只落一单」。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from app.agents.refund import (
    MSG_NOT_CUSTOMER,
    MSG_NOT_MINE,
    MSG_NOT_REFUNDABLE,
    MSG_NO_PERMISSION,
    MSG_NO_REFUNDABLE,
    refund_node,
)
from app.agents.state import inject_identity
from app.db.models import Order, Refund
from app.db.session import SessionLocal
from app.tools.orders import refundable_orders
from app.tools.refund import CONSUMING_REFUND_STATUSES, REFUNDABLE_ORDER_STATUSES
from tests.tools_helpers import TEST_KEY_PREFIX, cleanup_tool_rows

pytestmark = pytest.mark.db


@pytest.fixture
def clean_writes():
    """本套件会真的插退款行（占额度），前后都按幂等键前缀清掉。"""
    cleanup_tool_rows()
    yield
    cleanup_tool_rows()


def state_for(text: str, *, user_id: int = 2, role: str = "customer") -> dict[str, Any]:
    """造节点入口 state（与 API 的 `entry_state` 同形）。"""
    return {"messages": [{"role": "user", "content": text}], **inject_identity(user_id, role)}


def refund_count_for(*, user_id: int, order_id: int) -> int:
    with SessionLocal() as db:
        return int(
            db.execute(
                select(func.count())
                .select_from(Refund)
                .where(Refund.user_id == user_id, Refund.order_id == order_id)
            ).scalar_one()
        )


def pick_customer_with_orders(*, min_candidates: int = 1) -> tuple[int, int]:
    """挑一个「至少有 `min_candidates` 笔可退订单」的演示客户，返回 `(user_id, 第一个订单号)`。

    ⚠️ 必须限定 user_id 在 {2, 3}（演示账号只有这两个客户）：
    别的 user 也在 `users` 表里，但**没有账号**（登录不了），
    用例拿他们的 user_id 造 state 会得到「令牌主体对不上」的假失败。

    ⚠️ `min_candidates` 是必需的参数而不是「随便挑一个」：演示数据里
    客户 2 只有 1 笔可退订单、客户 3 有 4 笔。要验「多笔就追问」的用例
    如果挑到客户 2，会因为**数据分布**而失败（而不是因为代码不对）。
    让用例显式声明它需要的形状，比让它在数据变化时莫名其妙地红要好。
    """
    for user_id in (2, 3):
        candidates = refundable_orders(user_id)
        if len(candidates) >= min_candidates:
            return user_id, int(candidates[0]["order_id"])
    pytest.skip(
        f"演示库里客户 2/3 都没有 {min_candidates} 笔可退订单："
        "先跑 uv run python -m scripts.seed（注意：图用例会占用额度，跑完应清理）"
    )


# ── 正常路径 ─────────────────────────────────────────────────────────────────


async def test_unique_refundable_order_is_submitted(clean_writes, seeded) -> None:
    """★唯一可退订单 → **落库**为 `requested`，并回传单号/金额/剩余额度。

    这是需求 2「客户可以通过 LLM 申请退款」的核心判据。
    """
    user_id, order_id = pick_customer_with_orders()
    # 只留一笔可退订单（把其余的可退额度用真实退款占掉，而不是改数据）
    others = [item for item in refundable_orders(user_id) if int(item["order_id"]) != order_id]
    for item in others:
        from app.tools.refund import request_refund
        from tests.tools_helpers import tool_key

        request_refund(
            user_id,
            int(item["order_id"]),
            item["remaining"],
            idempotency_key=tool_key(f"graph-occupy-{item['order_id']}"),
            caller="test",
            role="customer",
        )

    before = refund_count_for(user_id=user_id, order_id=order_id)
    out = await refund_node(state_for(f"订单 {order_id} 帮我退款", user_id=user_id))

    result = out["refund"]
    assert result["status"] == "submitted", f"应当落单，实际：{out}"
    assert result["order_id"] == order_id
    assert refund_count_for(user_id=user_id, order_id=order_id) == before + 1
    # 单号/金额/状态都是**已发生的事实**，必须能核对
    assert int(result["refund_id"]) > 0
    assert result["amount"]
    assert result["state"] == "requested", "客户申请只能产出 requested"


async def test_multiple_refundable_orders_asks_instead_of_guessing(clean_writes, seeded) -> None:
    """★多笔可退且客户**没点名**订单号 → **不落库**，把候选列出来追问。

    「退哪一单」是本节点**无法从事实推断**的选择。猜错的代价是
    「客户想退 A，系统退了 B」—— 那比多问一句严重得多。
    """
    user_id, _order_id = pick_customer_with_orders(min_candidates=2)
    candidates = refundable_orders(user_id)
    assert len(candidates) >= 2

    ids = [int(item["order_id"]) for item in candidates]
    before = {oid: refund_count_for(user_id=user_id, order_id=oid) for oid in ids}

    out = await refund_node(state_for("我要退款", user_id=user_id))

    assert out["refund"]["status"] == "needs_order_id"
    assert set(out["refund"]["candidates"]) == set(ids)
    for oid in ids:
        assert refund_count_for(user_id=user_id, order_id=oid) == before[oid], (
            f"多笔可退时不该落库，但订单 {oid} 的退款行数变了"
        )


async def test_named_order_wins_over_asking(clean_writes, seeded) -> None:
    """客户**在话里点名**了某一笔 → 直接用它，不再追问。

    这是对「多笔就问」的必要补充：客户已经把答案说了，再问一遍是坏的体验，
    而且会让他以为系统没听懂。
    """
    user_id, _order_id = pick_customer_with_orders(min_candidates=2)
    candidates = refundable_orders(user_id)
    target = int(candidates[-1]["order_id"])

    out = await refund_node(state_for(f"订单 {target} 退了", user_id=user_id))
    assert out["refund"]["status"] == "submitted"
    assert out["refund"]["order_id"] == target


# ── 幂等与重放（本节点最贵的一条）────────────────────────────────────────────


async def test_replay_creates_only_one_refund(clean_writes, seeded) -> None:
    """★**同一轮重放两次只落一单** —— 幂等键必须确定性。

    图在节点失败时会从断点重放（`app/graph/run.py`），所以同一段 state
    可能被这个节点执行**两次**。如果幂等键带随机源，第二次会建出**第二张单**：
    两张 `requested` 各占一份额度，客户会发现可退额度莫名少了一半。

    这条用例直接把节点跑两次来模拟重放（同样的 state、同样的原话）。
    """
    user_id, order_id = pick_customer_with_orders()
    # 先占掉其余可退订单，保证「唯一可退」这条分支被走到
    for item in refundable_orders(user_id):
        if int(item["order_id"]) != order_id:
            from app.tools.refund import request_refund
            from tests.tools_helpers import tool_key

            request_refund(
                user_id,
                int(item["order_id"]),
                item["remaining"],
                idempotency_key=tool_key(f"graph-occupy2-{item['order_id']}"),
                caller="test",
                role="customer",
            )

    before = refund_count_for(user_id=user_id, order_id=order_id)
    state = state_for(f"订单 {order_id} 帮我退款", user_id=user_id)
    first = await refund_node(state)
    second = await refund_node(state)  # ← 同一段 state，模拟重放

    assert first["refund"]["status"] == "submitted", f"第一次执行就没落单：{first}"
    assert first["refund"]["refund_id"] == second["refund"]["refund_id"], (
        "重放得到了不同的退款单 —— 幂等键带了随机源，客户会被多退一笔"
    )
    assert refund_count_for(user_id=user_id, order_id=order_id) == before + 1, (
        "重放建出了第二张单（幂等失效）"
    )


# ── 拒绝路径：如实说明，且**不抛异常** ───────────────────────────────────────


async def test_no_refundable_order_is_explained(clean_writes, seeded) -> None:
    """没有可退订单 → 一条**如实**的 refusal（不是 500，也不是「系统故障」）。"""
    user_id, _order_id = pick_customer_with_orders()
    # 把所有可退订单的额度占满
    from app.tools.refund import request_refund
    from tests.tools_helpers import tool_key

    for item in refundable_orders(user_id):
        request_refund(
            user_id,
            int(item["order_id"]),
            item["remaining"],
            idempotency_key=tool_key(f"graph-drain-{item['order_id']}"),
            caller="test",
            role="customer",
        )
    assert refundable_orders(user_id) == []

    out = await refund_node(state_for("我要退款", user_id=user_id))
    assert out["refund"]["status"] == "refused"
    assert out["refusals"] == [MSG_NO_REFUNDABLE]


async def test_order_of_another_user_is_not_confirmed(clean_writes, seeded) -> None:
    """★客户提到**别人的**订单号 → 只说「没查到」，**不确认它存在**。

    这条与 `app/api/errors.py` 的越权口径同源：确认「这个订单号是真的」
    本身就是一次信息泄漏（攻击者据此枚举订单号）。
    """
    user_id, _order_id = pick_customer_with_orders()
    with SessionLocal() as db:
        victim = db.execute(
            select(Order.order_id)
            .where(Order.user_id != user_id)
            .where(Order.status.in_(REFUNDABLE_ORDER_STATUSES))
            .limit(1)
        ).scalar_one_or_none()
    if victim is None:
        pytest.skip("演示数据里没有别人的可退订单")

    out = await refund_node(state_for(f"订单 {int(victim)} 帮我退了", user_id=user_id))
    assert out["refund"]["status"] == "refused"
    assert out["refusals"] == [MSG_NOT_MINE]
    assert refund_count_for(user_id=user_id, order_id=int(victim)) == 0


async def test_own_order_in_a_non_refundable_state_says_so(clean_writes, seeded) -> None:
    """自己的单但状态不可退 → **说明原因**（与「不是你的单」区分开）。

    两者对用户的含义完全不同：一个是「等发货就行」，另一个是「你输错了订单号」。
    含糊成同一句话会让客户去反复核对一个本来就对的订单号。
    """
    user_id, _order_id = pick_customer_with_orders()
    with SessionLocal() as db:
        row = db.execute(
            select(Order.order_id)
            .where(Order.user_id == user_id)
            .where(Order.status.notin_(REFUNDABLE_ORDER_STATUSES))
            .limit(1)
        ).scalar_one_or_none()
    if row is None:
        pytest.skip("演示数据里该客户没有「不可退状态」的订单")

    out = await refund_node(state_for(f"订单 {int(row)} 帮我退了", user_id=user_id))
    assert out["refund"]["status"] == "refused"
    assert out["refusals"] == [MSG_NOT_REFUNDABLE]


async def test_employee_identity_is_told_to_use_the_review_console(seeded) -> None:
    """员工主体（`user_id=0`）走到这个节点 → 如实说明「请走审批界面」。

    ⚠️ 不是异常：员工主体**有**合法的 `user_id = 0`，那不是接线错误而是路径不对
    （`MissingIdentityError` 是留给「入口压根没写 user_id」的）。
    """
    out = await refund_node(
        {"messages": [{"role": "user", "content": "帮我退款"}], **inject_identity(0, "internal_employee")}
    )
    # 员工没有 request:refund → 先撞权限那条（也如实说明）
    assert out["refund"]["status"] == "refused"
    assert out["refusals"][0] in (MSG_NOT_CUSTOMER, MSG_NO_PERMISSION)


async def test_role_without_request_permission_is_refused(seeded) -> None:
    """没有 `request:refund` 的角色 → refusal（不是异常）。"""
    out = await refund_node(state_for("帮我退款", user_id=2, role="customer_service"))
    assert out["refund"]["status"] == "refused"
    assert out["refusals"] == [MSG_NO_PERMISSION]


# ── 与状态口径的一致性 ───────────────────────────────────────────────────────


async def test_submitted_refund_counts_towards_the_quota(clean_writes, seeded) -> None:
    """★落单之后额度**真的减少**（`requested` 计入占用）。

    这条把节点与账目口径连起来：如果节点落了单但额度没变，
    客户就能反复用同一笔订单拿到多笔退款 —— 那是资金超发。
    """
    user_id, order_id = pick_customer_with_orders()
    for item in refundable_orders(user_id):
        if int(item["order_id"]) != order_id:
            from app.tools.refund import request_refund
            from tests.tools_helpers import tool_key

            request_refund(
                user_id,
                int(item["order_id"]),
                item["remaining"],
                idempotency_key=tool_key(f"graph-quota-{item['order_id']}"),
                caller="test",
                role="customer",
            )

    from app.tools.refund import remaining_refundable

    before = remaining_refundable(user_id, order_id)
    out = await refund_node(state_for(f"订单 {order_id} 帮我退款", user_id=user_id))
    assert out["refund"]["status"] == "submitted"

    amount = Decimal(str(out["refund"]["amount"]))
    after = remaining_refundable(user_id, order_id)
    assert after == before - amount, f"落单后额度没有变化：{before} → {after}（金额 {amount}）"
    assert after == Decimal("0.00")


def test_graph_refund_rows_are_cleaned_up() -> None:
    """自检：图侧造的行（`llm-` 前缀）**能被清理函数命中**。

    这条不是「测测试」，而是把「测试不该永久吃掉演示库的可退额度」这条纪律钉在代码里。
    实测踩到过：图用例让节点真的落了单，而清理函数只认 `test-tools-` 前缀，
    于是跑完一遍图用例之后「客户 2/3 没有可退订单」，后续用例成片 skip ——
    现象看起来像「种子数据坏了」，真因是测试数据没清。
    """
    from app.tools.refund import llm_refund_key
    from tests.tools_helpers import LLM_KEY_PREFIX, TEST_KEY_PREFIX, cleanup_tool_rows

    key = llm_refund_key(subject="c2", order_id=1, message="x")
    assert key.startswith(LLM_KEY_PREFIX), "图侧键的前缀变了：清理函数就命中不了它了"
    assert not key.startswith(TEST_KEY_PREFIX), (
        "图侧键不该带上 `test-tools-` 前缀：那个前缀的含义是「本套件自己造的」"
    )
    # 真的清一次（幂等，重复跑没有副作用）
    assert set(cleanup_tool_rows()) == {"tickets", "refunds"}
