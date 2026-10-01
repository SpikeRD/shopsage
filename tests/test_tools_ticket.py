"""`ticket` 的测试：**幂等要看库里的行数**，越权要看「被拒 + 留痕」。

三条纪律：

1. **幂等的判据是数据库里的行数**，不是「两次返回值相等」——
   后者在一个「先 SELECT 再 INSERT」的实现里同样成立（单线程没有竞态），
   而行数断言 + 唯一约束才是真正的证据（见 `write_path.insert_once` 的说明）。
2. **越权用例断言的是异常类型 + 审计记录**：只断言抛错，等于只测了一半 ——
   另一半是「这次尝试在日志里看得见」（D1 的证据链）。
3. 真库用例都带 `@pytest.mark.db`，并在用例末尾按幂等键前缀清理自己造的行。
"""

from __future__ import annotations

import logging

import pytest

from app.security.rbac import PermissionDenied
from app.tools.audit import audit_records
from app.tools.errors import ToolContractError
from app.tools.ticket import TicketRef, create_ticket, get_ticket_status
from app.tools.write_path import MAX_IDEMPOTENCY_KEY_CHARS
from tests.tools_helpers import cleanup_tool_rows, count_tickets, tool_key

CALLER = "test"


@pytest.fixture
def clean_writes():
    """用例前后各清一次本套件造的行（幂等键前缀隔离，不动种子数据）。"""
    cleanup_tool_rows()
    yield
    cleanup_tool_rows()


# ── 离线层：契约与权限 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_type", ["退款", "REFUND", "", None, "order_"])
def test_invalid_ticket_type_is_rejected_before_touching_db(bad_type):
    """类型枚举错 → `ToolContractError`（在**进数据库之前**拦，报文里带全部合法取值）。"""
    with pytest.raises(ToolContractError, match="工单类型"):
        create_ticket(1, bad_type, idempotency_key=tool_key("t"), caller=CALLER)


@pytest.mark.parametrize(
    "bad_key",
    ["", "   ", "x" * (MAX_IDEMPOTENCY_KEY_CHARS + 1), "has space", "换行\n", "带#号"],
)
def test_invalid_idempotency_keys_are_rejected(bad_key):
    """幂等键形状：非空、长度 ≤64、字符集受限（否则「同一个键」会有多种写法，幂等失效）。"""
    with pytest.raises(ToolContractError, match="idempotency_key"):
        create_ticket(1, "logistics", idempotency_key=bad_key, caller=CALLER)


def test_unknown_role_has_no_write_permission():
    """未登记角色 = 无权限（默认拒绝），而不是「没登记就放行」。"""
    with pytest.raises(PermissionDenied) as excinfo:
        create_ticket(1, "logistics", idempotency_key=tool_key("role"), caller=CALLER, role="guest")
    assert "write:ticket" in str(excinfo.value)
    assert excinfo.value.missing == ("write:ticket",)


def test_permission_denied_is_audited(caplog):
    """越权/缺权限的尝试必须留痕（审计里能看到「谁被拒了、缺哪个权限」）。"""
    with caplog.at_level(logging.WARNING, logger="app.tools.audit"):
        with pytest.raises(PermissionDenied):
            create_ticket(1, "logistics", idempotency_key=tool_key("audit"), caller="agent:data", role="guest")

    record = audit_records(caplog.records)[-1]
    assert record["tool"] == "create_ticket"
    assert record["outcome"] == "error"
    assert record["error_type"] == "PermissionDenied"
    assert record["args"]["role"] == "guest"


def test_caller_is_required():
    """审计字段不可选：没有 caller 连参数校验都过不去。"""
    with pytest.raises(ToolContractError, match="caller"):
        create_ticket(1, "logistics", idempotency_key=tool_key("nocaller"), caller="")


def test_invalid_user_id_is_rejected():
    """`user_id` 必须是正整数（bool 也挡掉：`True` 会静默变成 user_id=1）。"""
    for bad in (0, -3, "1", True, None):
        with pytest.raises(ToolContractError, match="user_id"):
            create_ticket(bad, "logistics", idempotency_key=tool_key("uid"), caller=CALLER)


# ── 真库层：幂等、归属、状态 ─────────────────────────────────────────────────


@pytest.mark.db
def test_create_ticket_idempotent(clean_writes, seeded):
    """`6.tools.md` §7 第 3 条：同一个幂等键重复调用 → 返回值相等，且**库里只有一行**。"""
    key = tool_key("idem")
    first = create_ticket(1, "logistics", idempotency_key=key, caller=CALLER)
    second = create_ticket(1, "logistics", idempotency_key=key, caller=CALLER)

    assert isinstance(first, TicketRef)
    assert first == second  # 幂等：两次调用返回同一个业务对象
    assert count_tickets(key) == 1  # ★ 真正的判据：数据库里只有一行


@pytest.mark.db
def test_idempotent_hit_reports_existing_row_not_request(clean_writes, seeded):
    """幂等命中时返回**库里那行**的字段：第二次请求带了不同的 type，不能把请求参数回显成结果。"""
    key = tool_key("idem-type")
    first = create_ticket(1, "logistics", idempotency_key=key, caller=CALLER)
    second = create_ticket(1, "refund", idempotency_key=key, caller=CALLER)

    assert second == first  # 仍然是第一次那行（type=logistics）
    assert second.type == "logistics"
    assert count_tickets(key) == 1


@pytest.mark.db
def test_different_keys_create_different_tickets(clean_writes, seeded):
    """不同幂等键 = 两次真实请求 → 两张工单（幂等不能退化成「同类型只建一张」）。"""
    a = create_ticket(1, "logistics", idempotency_key=tool_key("k1"), caller=CALLER)
    b = create_ticket(1, "logistics", idempotency_key=tool_key("k2"), caller=CALLER)
    assert a.ticket_id != b.ticket_id
    assert a != b


@pytest.mark.db
def test_same_key_different_users_are_isolated(clean_writes, seeded):
    """幂等键按 `user_id` 命名空间隔离：单列全局唯一时，第二个用户会**拿到第一个用户的工单**。"""
    shared_key = tool_key("shared")
    mine = create_ticket(1, "logistics", idempotency_key=shared_key, caller=CALLER)
    theirs = create_ticket(2, "logistics", idempotency_key=shared_key, caller=CALLER)

    assert mine.ticket_id != theirs.ticket_id
    assert theirs.user_id == 2
    assert count_tickets(shared_key) == 2


@pytest.mark.db
def test_unknown_user_is_rejected_with_readable_message(clean_writes):
    """用户不存在：给一条能读懂的报错，而不是让外键约束抛 23503。"""
    with pytest.raises(ToolContractError, match="用户不存在"):
        create_ticket(999_999, "logistics", idempotency_key=tool_key("nouser"), caller=CALLER)


@pytest.mark.db
def test_create_ticket_audit_records_created_flag(clean_writes, seeded, caplog):
    """「本次是否新建」在审计里（不在返回值里）—— 第一次 True、第二次 False。"""
    key = tool_key("flag")
    with caplog.at_level(logging.INFO, logger="app.tools.audit"):
        create_ticket(1, "logistics", idempotency_key=key, caller=CALLER)
        create_ticket(1, "logistics", idempotency_key=key, caller=CALLER)

    records = [r for r in audit_records(caplog.records) if r["tool"] == "create_ticket"]
    assert [r["details"]["created"] for r in records] == [True, False]
    assert records[0]["rows"] == 1 and records[1]["details"]["idempotent_hit"] is True


@pytest.mark.db
def test_get_ticket_status_own_ticket(clean_writes, seeded):
    """对客查自己的工单：正常返回（并带行级过滤的语义 —— 只查自己的）。"""
    key = tool_key("status")
    created = create_ticket(1, "order", idempotency_key=key, caller=CALLER)
    found = get_ticket_status(created.ticket_id, caller=CALLER, requester_user_id=1)
    assert found == created
    assert found is not None and found.status == "open"


@pytest.mark.db
def test_get_ticket_status_blocks_cross_user_read(clean_writes, seeded, caplog):
    """越权读别人的工单：`PermissionDenied`（403 语义）+ 审计留痕，而不是假装「不存在」。"""
    key = tool_key("cross")
    created = create_ticket(1, "order", idempotency_key=key, caller=CALLER)

    with caplog.at_level(logging.WARNING, logger="app.tools.audit"):
        with pytest.raises(PermissionDenied, match="越权拦截"):
            get_ticket_status(created.ticket_id, caller="agent:data", requester_user_id=2)

    record = audit_records(caplog.records)[-1]
    assert record["tool"] == "get_ticket_status"
    assert record["outcome"] == "error"
    assert record["details"]["owned_by_requester"] is False


@pytest.mark.db
def test_get_ticket_status_missing_returns_none():
    """工单不存在 → `None`（不是异常）：调用方据此回答「没有这个工单」。"""
    assert get_ticket_status(999_999, caller=CALLER, requester_user_id=1) is None


@pytest.mark.db
def test_internal_read_without_requester_can_see_any_ticket(clean_writes, seeded):
    """内部路径（不给 requester_user_id）可以看到任意工单 —— 角色判定由第 9 步 API 层负责。"""
    created = create_ticket(1, "product", idempotency_key=tool_key("internal"), caller="agent:analysis")
    assert get_ticket_status(created.ticket_id, caller="agent:analysis") == created
