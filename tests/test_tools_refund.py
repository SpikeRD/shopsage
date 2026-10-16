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

from app.security.rbac import PERM_APPROVE_REFUND, PERM_REQUEST_REFUND, PermissionDenied
from app.tools.audit import audit_records
from app.tools.errors import PermanentToolError, ToolContractError
from app.tools.refund import (
    REFUNDABLE_ORDER_STATUSES,
    RefundNotAllowed,
    RefundRef,
    create_refund_request,
    decide_refund,
    get_refund_status,
    list_refunds_for_review,
    llm_refund_key,
    remaining_refundable,
    request_refund,
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


# ══════════════════════════════════════════════════════════════════════════════
# 变更 003：客户自助申请（request_refund）与员工审批（decide_refund）
#
# 这一节的用例对应需求 2/3，重点不在「能不能跑」，而在四条边界：
#   ① 客户能申请、但**批不了**（权限点分离：request:refund vs approve:refund）；
#   ② 客户自助申请**永远建不出** approved/rejected —— 状态由核心写死；
#   ③ 审批的状态机只有一条（requested → approved/rejected），其余 409；
#   ④ 审批留痕真落库（谁批的、何时、什么角色、什么意见）。
# ══════════════════════════════════════════════════════════════════════════════


def test_customer_can_request_but_cannot_decide() -> None:
    """★变更 003 的核心分离：客户有 `request:refund`，**没有** `approve:refund`。

    这是「客户不能自己批准自己的退款」在**权限表**层的判据。
    HTTP 层的对应判据在 `tests/api/test_authorization.py::test_customer_cannot_decide_their_own_refund`。
    """
    from app.security.rbac import PERM_APPROVE_REFUND, PERM_REQUEST_REFUND, permissions_for

    customer = permissions_for("customer")
    assert PERM_REQUEST_REFUND in customer
    assert PERM_APPROVE_REFUND not in customer, "客户拿到审批权就等于可以自审批（需求 2 要修的就是它）"
    # 反向：审批权确实给了该给的角色（否则「员工能批」这条不成立）
    for role in ("customer_service", "internal_employee", "manager", "finance_auditor", "admin"):
        assert PERM_APPROVE_REFUND in permissions_for(role), f"{role} 没有审批权"


def test_request_refund_rejects_role_without_permission() -> None:
    """没有 `request:refund` 的角色（例如未登记角色）→ `PermissionDenied`。"""
    with pytest.raises(PermissionDenied) as excinfo:
        request_refund(1, 1, Decimal("1.00"), idempotency_key=tool_key("req-guest"), caller=CALLER, role="guest")
    assert excinfo.value.missing == (PERM_REQUEST_REFUND,)


def test_decide_refund_rejects_customer_role() -> None:
    """★客户角色调审批 → `PermissionDenied`（缺 `approve:refund`）。"""
    with pytest.raises(PermissionDenied) as excinfo:
        decide_refund(1, decision="approve", caller=CALLER, role="customer")
    assert excinfo.value.missing == (PERM_APPROVE_REFUND,)


def test_decide_refund_rejects_bad_decision_value() -> None:
    """`decision` 只接受 approve/reject（形状问题 → `ToolContractError`，不是 500）。"""
    with pytest.raises(ToolContractError):
        decide_refund(1, decision="maybe", caller=CALLER, role="customer_service")


def test_llm_refund_key_is_deterministic() -> None:
    """★图侧幂等键：**确定性**（重放安全）。

    三条断言，每条都对应一类真实故障：
      ① 同样入参 → 同样输出（否则图重放会建出两张退款单）；
      ② 长度与字符集合规（列宽 64 + `validate_idempotency_key` 的字符集）；
      ③ 不同的客户原话 / 不同的订单 → 不同的键
         （客户改了理由再申请是一次**新**意图，不该被静默吞掉）。

    ⚠️ 键里**不含金额**（第一版含了，被重放用例抓出来）：重放时额度已被第一次占满，
    「剩余可退额度」算出来是 0，含金额的键在重放时**算不出同一个值**，
    于是永远命中不了按键回查的那一步。理由写在 `llm_refund_key` 的 docstring。
    """
    first = llm_refund_key(subject="c2", order_id=7, message="我要退款")
    assert first == llm_refund_key(subject="c2", order_id=7, message="我要退款"), "不确定的键 = 重放会多退钱"
    assert first.startswith("llm-")
    assert len(first) <= 64, "超过 idempotency_key 的列宽（VARCHAR(64)）"
    assert first.isascii() and all(ch.isalnum() or ch in "-._:" for ch in first), (
        "键里有非法字符：`validate_idempotency_key` 只允许 [A-Za-z0-9._:-]"
    )
    assert first != llm_refund_key(subject="c2", order_id=7, message="这单退了"), "换了理由应当是新的意图"
    assert first != llm_refund_key(subject="c2", order_id=8, message="我要退款"), "换了订单必须是不同的键"
    assert first != llm_refund_key(subject="c3", order_id=7, message="我要退款"), "换了客户必须是不同的键"


@pytest.mark.db
def test_request_refund_creates_requested_only(clean_writes, refundable_order, seeded):
    """★客户自助申请**只能**建 `requested`：库里的状态就是它。"""
    order_id, user_id, _total, remaining = refundable_order
    ref = request_refund(
        user_id,
        order_id,
        Decimal("1.00"),
        idempotency_key=tool_key("req-status"),
        caller=CALLER,
        role="customer",
    )
    assert ref.status == "requested", f"客户自助申请建出了 {ref.status}，而它只该是 requested"
    assert remaining >= Decimal("1.00")


@pytest.mark.db
def test_request_refund_is_idempotent(clean_writes, refundable_order, seeded):
    """同一个键重复申请只落一行（A2 在客户自助路径上同样成立）。"""
    order_id, user_id, _total, _remaining = refundable_order
    key = tool_key("req-idem")
    first = request_refund(user_id, order_id, Decimal("1.00"), idempotency_key=key, caller=CALLER, role="customer")
    second = request_refund(user_id, order_id, Decimal("1.00"), idempotency_key=key, caller=CALLER, role="customer")
    assert first.refund_id == second.refund_id
    assert count_refunds(key) == 1


@pytest.mark.db
def test_decide_refund_approve_then_redecide_is_conflict(clean_writes, refundable_order, seeded):
    """★审批状态机：`requested → approved` 成功；**再次决策 409**（不是静默覆盖）。

    这条防的是「两个员工同时点通过」与「重复点击」两种真实场景：
    第二次必须被明确拒绝并带上当前状态，让前端刷新 —— 而不是覆盖掉第一次的审批留痕。
    """
    from app.tools.refund import RefundNotDecidable

    order_id, user_id, _total, _remaining = refundable_order
    created = request_refund(
        user_id, order_id, Decimal("1.00"), idempotency_key=tool_key("dec-1"), caller=CALLER, role="customer"
    )

    approved = decide_refund(
        created.refund_id,
        decision="approve",
        note="已核实",
        decider_subject_id=2,
        decider_subject_type="employee",
        caller=CALLER,
        role="customer_service",
    )
    assert approved.status == "approved"
    assert approved.decided_by == 2
    assert approved.approver_role == "customer_service"
    assert approved.decided_at is not None
    assert approved.decision_note == "已核实"

    with pytest.raises(RefundNotDecidable) as excinfo:
        decide_refund(
            created.refund_id,
            decision="reject",
            decider_subject_id=2,
            decider_subject_type="employee",
            caller=CALLER,
            role="customer_service",
        )
    assert excinfo.value.status == "approved", "冲突时必须带上当前状态（前端据此刷新）"
    assert excinfo.value.refund_id == created.refund_id


@pytest.mark.db
def test_decide_refund_reject_frees_the_quota(clean_writes, refundable_order, seeded):
    """被驳回的退款**不再占用额度**：`CONSUMING_REFUND_STATUSES` 不含 rejected。

    这条把「驳回」与「通过」在**账**上的差别钉住：驳回之后客户可以再申请一次
    （剩下多少钱，就是原来的额度）。
    """
    order_id, user_id, _total, remaining = refundable_order
    created = request_refund(
        user_id, order_id, Decimal("1.00"), idempotency_key=tool_key("dec-rej"), caller=CALLER, role="customer"
    )
    assert remaining_refundable(user_id, order_id) == remaining - Decimal("1.00")

    rejected = decide_refund(
        created.refund_id,
        decision="reject",
        decider_subject_id=2,
        decider_subject_type="employee",
        caller=CALLER,
        role="customer_service",
    )
    assert rejected.status == "rejected"
    assert remaining_refundable(user_id, order_id) == remaining, "被驳回的钱必须能再退一次"


@pytest.mark.db
def test_decide_refund_non_employee_subject_leaves_decided_by_null(clean_writes, refundable_order, seeded):
    """★非员工主体审批时 `decided_by` 落 `NULL`（**不是 0**）。

    为什么要专门测：`decided_by` 有指向 `employees.emp_id` 的外键。
    一个客户主体（`accounts.subject_type == "customer"`）理论上可以持有 `approve:refund`
    （将来可能有的「客户代表」角色）。那时把客户 `user_id` 写进去会**违反外键约束**
    —— 500，而不是「留下一条审计记录」。
    落 NULL 的语义是「这张单不是员工批的」，而 `approver_role` 照样记下来。
    """
    order_id, user_id, _total, _remaining = refundable_order
    created = request_refund(
        user_id, order_id, Decimal("1.00"), idempotency_key=tool_key("dec-nonemp"), caller=CALLER, role="customer"
    )
    view = decide_refund(
        created.refund_id,
        decision="approve",
        decider_subject_id=user_id,
        decider_subject_type="customer",
        caller=CALLER,
        role="admin",  # 用一个确实有权限的角色，专门验「主体类别」这一条分支
    )
    assert view.decided_by is None, "非员工主体写进 employees 外键会直接违反约束"
    assert view.approver_role == "admin"


@pytest.mark.db
def test_list_refunds_for_review_filters(clean_writes, refundable_order, seeded):
    """审批台查询：按单号 / 订单号 / 客户 / 状态多选 都能筛出正确子集。"""
    order_id, user_id, _total, _remaining = refundable_order
    created = request_refund(
        user_id, order_id, Decimal("1.00"), idempotency_key=tool_key("list-1"), caller=CALLER, role="customer"
    )
    rows = list_refunds_for_review(
        refund_id=created.refund_id, role="customer_service", caller=CALLER
    )
    assert [row.refund_id for row in rows] == [created.refund_id]

    by_order = list_refunds_for_review(order_id=order_id, role="customer_service", caller=CALLER)
    assert created.refund_id in {row.refund_id for row in by_order}

    by_user = list_refunds_for_review(user_id=user_id, role="customer_service", caller=CALLER)
    assert all(row.user_id == user_id for row in by_user)

    # 状态多选：刚建的单在 requested 里，且必然不在 rejected 里
    only_requested = list_refunds_for_review(
        refund_id=created.refund_id, statuses=["requested"], role="customer_service", caller=CALLER
    )
    assert [row.refund_id for row in only_requested] == [created.refund_id]
    only_rejected = list_refunds_for_review(
        refund_id=created.refund_id, statuses=["rejected"], role="customer_service", caller=CALLER
    )
    assert only_rejected == []


def test_list_refunds_requires_approve_permission() -> None:
    """没有 `approve:refund` 的角色不能查退款列表（客户不能看别人的退款）。"""
    with pytest.raises(PermissionDenied) as excinfo:
        list_refunds_for_review(role="customer", caller=CALLER)
    assert excinfo.value.missing == (PERM_APPROVE_REFUND,)


def test_list_refunds_rejects_unknown_status() -> None:
    """非法的 status 取值 → `ToolContractError`（不是静默忽略那次筛选）。

    静默忽略是最坏的选择：用户会以为自己筛过了，看到的其实是另一批数据。
    """
    with pytest.raises(ToolContractError):
        list_refunds_for_review(statuses=["nonsense"], role="customer_service", caller=CALLER)
