"""`refund` 的测试：**资金规则用真库证明**，越权用异常 + 审计证明。

退款是本项目里唯一会动钱的操作，所以用例的重点不在「能不能跑通」，而在四条护栏：

1. **职责分离（SoD）**：`customer` 角色**没有** `write:refund` —— 对客只能建工单。
2. **归属校验**：拿别人的 `order_id` 来退款 → `PermissionDenied`（不是「订单不存在」）。
3. **额度校验**：金额不得超过「订单金额 − 已申请退款」，且**第二笔**要算上第一笔
   （只看订单总额的实现能过单笔测试，但会退超）。
4. **幂等**：同一个键重复提交只产生一行（A2）。

⚠️ 第 3 条与第 4 条必须同时成立才有意义：幂等键防的是「重复提交」，
额度校验防的是「换一个键再退一次」。只做前者，用户改个键就能退两次。
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from app.security.rbac import PermissionDenied
from app.tools.audit import audit_records
from app.tools.errors import PermanentToolError, ToolContractError
from app.tools.refund import (
    REFUNDABLE_ORDER_STATUSES,
    RefundNotAllowed,
    RefundRef,
    create_refund_request,
    get_refund_status,
)
from tests.tools_helpers import (
    cleanup_tool_rows,
    count_refunds,
    find_refundable_order,
    tool_key,
)

CALLER = "test"


@pytest.fixture
def clean_writes():
    """本套件会真的插退款行（会占订单额度），所以前后都按幂等键前缀清掉。"""
    cleanup_tool_rows()
    yield
    cleanup_tool_rows()


@pytest.fixture
def refundable_order():
    """挑一笔可退订单；演示库没准备好时 skip 并给出修复命令（不伪装成代码失败）。"""
    try:
        return find_refundable_order()
    except LookupError as exc:
        pytest.skip(str(exc))


# ── 离线层：权限、金额、原因文本 ─────────────────────────────────────────────


def test_customer_cannot_create_refund_by_design():
    """★职责分离：对客角色没有 `write:refund` —— 客户要退款应当走「建工单」。"""
    with pytest.raises(PermissionDenied) as excinfo:
        create_refund_request(1, 1, Decimal("10.00"), idempotency_key=tool_key("r"), caller=CALLER, role="customer")
    assert excinfo.value.missing == ("write:refund",)
    assert "职责分离" in str(excinfo.value)


def test_unknown_role_cannot_create_refund():
    """未登记角色 = 无权限（默认拒绝）。"""
    with pytest.raises(PermissionDenied):
        create_refund_request(1, 1, Decimal("10.00"), idempotency_key=tool_key("r"), caller=CALLER, role="guest")


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (10, "10.00"),
        (Decimal("10.5"), "10.50"),
        ("10.555", "10.56"),  # 四舍五入到分（与 NUMERIC(12,2) 对齐）
        (0.1 + 0.2, "0.30"),  # ★ 二进制浮点：float 必须先转 str，否则会带出 0.30000000000000004
    ],
)
def test_amount_is_normalized_to_cents(amount, expected):
    """金额口径：统一按「分」,且 `float` 走 `Decimal(str(x))` 以避免二进制浮点误差。"""
    from app.tools.refund import _to_money

    assert str(_to_money(amount)) == expected


@pytest.mark.parametrize("bad", [0, -1, "abc", None, True, []])
def test_invalid_amount_is_rejected(bad):
    """金额非法 → 契约错误（`True` 也要挡：它是 int 的子类，会变成 1 元）。"""
    with pytest.raises(ToolContractError, match="amount"):
        create_refund_request(1, 1, bad, idempotency_key=tool_key("r"), caller=CALLER)


def test_invalid_order_id_and_reason_are_rejected():
    """`order_id` 必须正整数；`reason` 必须是单行短文本。"""
    with pytest.raises(ToolContractError, match="order_id"):
        create_refund_request(1, 0, Decimal("1.00"), idempotency_key=tool_key("r"), caller=CALLER)
    with pytest.raises(ToolContractError, match="reason"):
        create_refund_request(
            1, 1, Decimal("1.00"), idempotency_key=tool_key("r"), caller=CALLER, reason="x" * 300
        )
    with pytest.raises(ToolContractError, match="reason"):
        create_refund_request(1, 1, Decimal("1.00"), idempotency_key=tool_key("r"), caller=CALLER, reason=123)


def test_refund_not_allowed_is_a_permanent_tool_error():
    """业务拒绝与契约错误**分开**：`RefundNotAllowed` 是 Permanent 的子类（都不重试，但语义不同）。"""
    assert issubclass(RefundNotAllowed, PermanentToolError)
    assert not issubclass(RefundNotAllowed, ToolContractError)


def test_permission_denied_is_audited(caplog):
    """被拒的资金操作要留痕（审计里能看出「谁试图退款但没权限」）。"""
    with caplog.at_level(logging.WARNING, logger="app.tools.audit"):
        with pytest.raises(PermissionDenied):
            create_refund_request(
                1, 1, Decimal("10.00"), idempotency_key=tool_key("r"), caller="agent:data", role="customer"
            )

    record = audit_records(caplog.records)[-1]
    assert record["tool"] == "create_refund"
    assert record["outcome"] == "error"
    assert record["error_type"] == "PermissionDenied"
    assert record["args"]["role"] == "customer"


# ── 真库层：归属、额度、幂等 ─────────────────────────────────────────────────


@pytest.mark.db
def test_create_refund_then_read_status(clean_writes, refundable_order, seeded):
    """happy path：可退订单的一笔小额退款 → 建单成功，状态可查（对客路径只查自己的）。"""
    order_id, user_id, _total, remaining = refundable_order
    amount = min(Decimal("10.00"), remaining)

    created = create_refund_request(
        user_id, order_id, amount, idempotency_key=tool_key("happy"), caller=CALLER, reason="七天无理由"
    )
    assert isinstance(created, RefundRef)
    assert created.status == "requested"
    assert created.amount == amount
    assert get_refund_status(created.refund_id, caller=CALLER, requester_user_id=user_id) == created


@pytest.mark.db
def test_create_refund_is_idempotent(clean_writes, refundable_order, seeded):
    """幂等：同一个键提交两次 → 返回值相等，且库里只有一行（A2：不重复退款）。"""
    order_id, user_id, _total, _remaining = refundable_order
    key = tool_key("refund-idem")
    first = create_refund_request(user_id, order_id, Decimal("5.00"), idempotency_key=key, caller=CALLER)
    second = create_refund_request(user_id, order_id, Decimal("5.00"), idempotency_key=key, caller=CALLER)

    assert first == second
    assert count_refunds(key) == 1


@pytest.mark.db
def test_refund_amount_cannot_exceed_remaining_quota(clean_writes, refundable_order, seeded):
    """额度上限：请求金额超过**剩余可退额度** → `RefundNotAllowed`（报错里带两个数字，便于对账）。

    ⚠️ 判据是「剩余额度」而不是「订单总额」：订单可能已经被退过一部分
    （演示数据里就有一笔 141.00 的订单已占用 112.80）。按总额判的实现会退超。
    """
    order_id, user_id, _total, remaining = refundable_order
    with pytest.raises(RefundNotAllowed) as excinfo:
        create_refund_request(
            user_id, order_id, remaining + Decimal("0.01"), idempotency_key=tool_key("over"), caller=CALLER
        )
    assert str(remaining) in str(excinfo.value)
    assert count_refunds(tool_key("over")) == 0  # 被拒的请求**不留行**


@pytest.mark.db
def test_second_refund_counts_previous_refunds(clean_writes, refundable_order, seeded):
    """★额度是**累计**校验：第一笔占掉额度后，第二笔（换一个幂等键）不能把余下的退超。

    这条与幂等是两件事：幂等键防「同一请求重放」，额度校验防「换个键再退一次」。
    只做幂等的话，改一个键就能把钱退两次。
    """
    order_id, user_id, _total, available = refundable_order
    if available < Decimal("2.00"):
        pytest.skip(f"这笔订单只剩 {available} 可退，无法构造两笔退款")

    first_amount = (available / 2).quantize(Decimal("0.01"))
    create_refund_request(user_id, order_id, first_amount, idempotency_key=tool_key("acc-1"), caller=CALLER)

    leftover = available - first_amount
    with pytest.raises(RefundNotAllowed):
        create_refund_request(
            user_id, order_id, leftover + Decimal("0.01"), idempotency_key=tool_key("acc-2"), caller=CALLER
        )

    # 额度之内的那一笔仍然可以成功（校验是算余额，而不是「一个订单只能退一次」）
    ok = create_refund_request(user_id, order_id, leftover, idempotency_key=tool_key("acc-3"), caller=CALLER)
    assert ok.amount == leftover


@pytest.mark.db
def test_refund_on_someone_elses_order_is_denied(clean_writes, refundable_order, seeded, caplog):
    """越权：拿别人的订单号来退款 → `PermissionDenied` + 审计留痕（这是资金侧的 D1）。"""
    order_id, user_id, _total, _remaining = refundable_order
    other_user = 1 if user_id != 1 else 2

    with caplog.at_level(logging.WARNING, logger="app.tools.audit"):
        with pytest.raises(PermissionDenied, match="越权拦截"):
            create_refund_request(
                other_user, order_id, Decimal("1.00"), idempotency_key=tool_key("cross"), caller=CALLER
            )

    record = audit_records(caplog.records)[-1]
    assert record["outcome"] == "error" and record["error_type"] == "PermissionDenied"
    assert record["args"]["user_id"] == other_user


@pytest.mark.db
@pytest.mark.db
def test_refund_on_missing_order_is_contract_error(clean_writes):
    """订单不存在 ≠ 越权：前者是调用方写错了（契约错误），后者是权限问题。"""
    with pytest.raises(ToolContractError, match="订单不存在"):
        create_refund_request(1, 999_999, Decimal("1.00"), idempotency_key=tool_key("noorder"), caller=CALLER)


@pytest.mark.db
def test_non_refundable_order_status_is_business_rejection(clean_writes, seeded):
    """状态不可退（如 `pending`/`cancelled`）→ 业务拒绝，报错里带上可退状态清单。"""
    from sqlalchemy import select

    from app.db.models import Order
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        row = db.execute(
            select(Order.order_id, Order.user_id, Order.status)
            .where(Order.status.notin_(REFUNDABLE_ORDER_STATUSES))
            .order_by(Order.order_id)
            .limit(1)
        ).one_or_none()
    if row is None:
        pytest.skip("演示库里没有不可退状态的订单：重新灌种子数据 `uv run python -m scripts.seed`")

    with pytest.raises(RefundNotAllowed) as excinfo:
        create_refund_request(
            int(row.user_id), int(row.order_id), Decimal("1.00"), idempotency_key=tool_key("status"), caller=CALLER
        )
    assert str(row.status) in str(excinfo.value)
    assert REFUNDABLE_ORDER_STATUSES[0] in str(excinfo.value)


@pytest.mark.db
def test_refund_takes_a_row_lock_on_the_order(clean_writes, refundable_order, seeded):
    """★`SELECT ... FOR UPDATE` 真的发给了数据库（`_remaining_refundable` 的并发前提）。

    为什么值得单独一条：行锁是「并发退款不超发」的唯一保障，而**单线程用例证明不了锁的效果**
    （没有第二个事务来撞）。能证明的是「这句 SQL 确实带锁」——
    于是删掉 `.with_for_update()` 时，这条用例立刻红，而不是等到线上退超一笔钱才发现。
    真正的并发验证属第 19 步压测（两个事务同时退同一订单）。
    """
    from sqlalchemy import event

    from app.db.session import get_write_engine

    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    engine = get_write_engine()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        order_id, user_id, _total, _remaining = refundable_order
        create_refund_request(
            user_id, order_id, Decimal("1.00"), idempotency_key=tool_key("lock"), caller=CALLER
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert any("FOR UPDATE" in statement.upper() for statement in statements), (
        "退款路径没有对订单行加锁（缺 with_for_update()）：并发退款会各自读到同样的余额"
    )
    # 订单查询（带锁）与退款插入的顺序：先锁后写，锁必须在 INSERT 之前发出去
    lock_index = next(i for i, s in enumerate(statements) if "FOR UPDATE" in s.upper())
    insert_index = next(i for i, s in enumerate(statements) if s.strip().upper().startswith("INSERT"))
    assert lock_index < insert_index


@pytest.mark.db
def test_refund_status_blocks_cross_user_read(clean_writes, refundable_order, seeded, caplog):
    """对客查退款状态：查别人的退款单 → `PermissionDenied` + 留痕。"""
    order_id, user_id, _total, _remaining = refundable_order
    created = create_refund_request(
        user_id, order_id, Decimal("1.00"), idempotency_key=tool_key("rstatus"), caller=CALLER
    )
    other_user = 1 if user_id != 1 else 2

    with caplog.at_level(logging.WARNING, logger="app.tools.audit"):
        with pytest.raises(PermissionDenied, match="越权拦截"):
            get_refund_status(created.refund_id, caller=CALLER, requester_user_id=other_user)

    assert audit_records(caplog.records)[-1]["details"]["owned_by_requester"] is False


@pytest.mark.db
def test_refund_status_missing_returns_none():
    """退款单不存在 → `None`。"""
    assert get_refund_status(999_999, caller=CALLER, requester_user_id=1) is None


@pytest.mark.db
def test_refund_audit_records_amount_and_flags(clean_writes, refundable_order, seeded, caplog):
    """审计字段：金额、订单状态、是否幂等命中都要能查到（对账与告警的依据）。"""
    order_id, user_id, _total, _remaining = refundable_order
    key = tool_key("audit")
    with caplog.at_level(logging.INFO, logger="app.tools.audit"):
        create_refund_request(user_id, order_id, Decimal("2.00"), idempotency_key=key, caller="api:chat")
        create_refund_request(user_id, order_id, Decimal("2.00"), idempotency_key=key, caller="api:chat")

    records = [r for r in audit_records(caplog.records) if r["tool"] == "create_refund"]
    assert [r["details"]["created"] for r in records] == [True, False]
    assert records[0]["args"]["amount"] == "Decimal('2.00')"  # 审计里金额以字符串形式可读
    assert records[0]["details"]["order_status"] in REFUNDABLE_ORDER_STATUSES
    assert records[0]["caller"] == "api:chat"
