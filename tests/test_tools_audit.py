"""`audit` 的测试：审计**字段齐、打码对、失败也留痕**（`6.tools.md` §4 第 2 条）。

审计这种东西最容易「看起来做了、关键时刻用不上」：
字段缺一个（没有 caller）、参数没打码（手机号进日志）、失败不记录（越权尝试无痕）——
三种毛病都不会让功能挂掉，但会让事故复盘变成猜谜。所以这三条各自有用例钉住。
"""

from __future__ import annotations

import json
import logging

import pytest

from app.security.masking import REDACTED
from app.tools.audit import (
    AUDIT_REDACTION_ROLE,
    AuditHandle,
    audit_records,
    audit_tool_call,
    new_audit_id,
    redact_args,
)
from app.tools.errors import ToolContractError


def test_audit_id_is_prefixed_and_unique():
    """审计 id 有 `tc-` 前缀（便于 grep）且不重复。"""
    ids = {new_audit_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("tc-") and len(i) == 15 for i in ids)


def test_success_records_all_required_fields(caplog):
    """一条成功记录必须带齐：调用者 / 时间 / 参数 / 结果行数 / 耗时 / 结果状态。"""
    with caplog.at_level(logging.INFO, logger="app.tools.audit"):
        with audit_tool_call("demo_tool", caller="agent:data", sql="SELECT 1", limit=5) as handle:
            handle.rows = 3
            handle.details["truncated"] = False

    record = audit_records(caplog.records)[-1]
    assert record["tool"] == "demo_tool"
    assert record["caller"] == "agent:data"
    assert record["outcome"] == "ok"
    assert record["rows"] == 3
    assert record["error_type"] is None
    assert record["args"] == {"sql": "SELECT 1", "limit": 5}
    assert record["details"] == {"truncated": False}
    assert record["started_at"].endswith("+00:00") and "T" in record["started_at"]
    assert record["duration_ms"] >= 0
    assert handle.audit_id == record["audit_id"]


def test_failure_is_also_audited_at_warning(caplog):
    """失败路径同样落一条记录（含异常类型与截断后的报文）—— 越权尝试就是靠它留痕的。"""
    with caplog.at_level(logging.WARNING, logger="app.tools.audit"):
        with pytest.raises(ValueError):
            with audit_tool_call("demo_tool", caller="mcp:sql_query"):
                raise ValueError("boom")

    record = audit_records(caplog.records)[-1]
    assert record["outcome"] == "error"
    assert record["error_type"] == "ValueError"
    assert record["details"]["error"] == "boom"
    assert caplog.records[-1].levelno == logging.WARNING


def test_cancellation_is_audited_and_propagated(caplog):
    """`CancelledError` 也要记（第 12 步的队列取消任务时，日志里必须能看出「是谁取消的」），且必须继续冒泡。"""
    import asyncio

    with caplog.at_level(logging.WARNING, logger="app.tools.audit"):
        with pytest.raises(asyncio.CancelledError):
            with audit_tool_call("demo_tool", caller="api:chat"):
                raise asyncio.CancelledError()

    record = audit_records(caplog.records)[-1]
    assert record["error_type"] == "CancelledError"


def test_caller_is_mandatory_and_cannot_be_blank():
    """空 caller / 全空白 caller 一律拒绝：审计字段不可选。"""
    with pytest.raises(ToolContractError, match="caller"):
        with audit_tool_call("demo_tool", caller=""):
            pass  # pragma: no cover - 构造阶段就该抛错
    with pytest.raises(ToolContractError, match="caller"):
        AuditHandle("demo_tool", "   ", {})


def test_audit_json_is_one_line_and_parseable(caplog):
    """落盘的是**一行** JSON：人 `Select-String` 看，机器 `json.loads` 直接解析。"""
    with caplog.at_level(logging.INFO, logger="app.tools.audit"):
        with audit_tool_call("demo_tool", caller="test", note="含中文备注") as handle:
            handle.rows = 1

    message = caplog.records[-1].getMessage()
    assert message.startswith("tool_call {")
    payload = json.loads(message[len("tool_call ") :])
    assert payload["args"]["note"] == "含中文备注"  # ensure_ascii=False：日志里中文保持可读


def test_sensitive_arguments_are_redacted(caplog):
    """参数打码：手机号/姓名/密钥类进日志前一律变模糊（复用 `security.masking`）。"""
    with caplog.at_level(logging.INFO, logger="app.tools.audit"):
        with audit_tool_call(
            "demo_tool",
            caller="test",
            phone="13812341234",
            name="张三",
            email="zhangsan@example.com",
            api_key="sk-secret-value",
            idempotency_key="req-123",  # 不是秘密：它是业务幂等键，打码会让排障变难
        ) as handle:
            handle.rows = 1

    record = audit_records(caplog.records)[-1]
    assert "13812" not in str(record["args"]["phone"])  # 打了码，且看不出完整号码
    assert record["args"]["name"] == REDACTED
    assert record["args"]["email"] != "zhangsan@example.com"
    assert record["args"]["api_key"] == REDACTED
    assert record["args"]["idempotency_key"] == "req-123"
    assert set(record["redacted_args"]) >= {"phone", "name", "api_key"}
    assert AUDIT_REDACTION_ROLE == "customer_service"  # 用最严角色打码，规则变更要一起改


def test_value_form_fallback_masks_unregistered_pii():
    """字段名没登记，但值长得像手机号的照样打码（漏登记是必然的，所以兜底不是可选项）。"""
    args, redacted = redact_args({"memo": "13812341234", "note": "客户说手机号打不通"})
    assert args["memo"] != "13812341234"  # 整个值是手机号 → 被规则认出来
    assert args["note"] == "客户说手机号打不通"  # 夹在长文本里的不在兜底范围（4.security-runbook §4.3）
    assert redacted == ("memo",)


def test_nested_arguments_are_redacted_too():
    """★嵌套参数同样要打码（**实测漏过**）：SQL 的绑定参数都在 `params` 这个子字典里，
    只打顶层字段名会让 `{"params": {"phone": "13812341234"}}` 原样进日志。

    这条用例是「先跑出真实日志、再回头补测试」的产物 ——
    只测顶层参数的版本是绿的，但日志里躺着一个完整手机号。
    """
    args, redacted = redact_args(
        {
            "sql": "SELECT order_id FROM orders WHERE user_id = :uid",
            "params": {"uid": 1, "phone": "13812341234", "nested": {"email": "a@b.com"}},
            "batch": [{"phone": "13900001111"}, {"uid": 2}],
        }
    )
    assert args["params"]["uid"] == 1  # 非敏感参数原样保留（排障要看得出查的是谁）
    assert "13812" not in str(args["params"]["phone"])
    assert args["params"]["nested"]["email"] != "a@b.com"
    assert "13900" not in str(args["batch"][0]["phone"])
    assert args["batch"][1] == {"uid": 2}
    assert set(redacted) >= {"params.phone", "params.nested.email", "batch.[0].phone"}


def test_long_values_are_truncated_not_dropped():
    """超长参数截断保留（不是丢掉）：日志不能被长 SQL/长文本放大，但排障要能看到前缀。"""
    long_sql = "SELECT " + ", ".join(f"c{i}" for i in range(500))
    args, _ = redact_args({"sql": long_sql})
    assert args["sql"].endswith("…(截断)")
    assert len(args["sql"]) < len(long_sql)
    assert args["sql"].startswith("SELECT c0, c1")


def test_non_json_types_are_downgraded_not_crashing():
    """`Decimal` / 对象 / 字节流不能把整条日志带崩（`json.dumps` 会直接抛 TypeError）。"""
    from decimal import Decimal

    args, _ = redact_args(
        {"amount": Decimal("12.34"), "blob": b"\x00\x01", "nested": {"inner": [Decimal("1.5")]}}
    )
    json.dumps(args)  # 不抛错即通过
    assert args["amount"] == "Decimal('12.34')"
    assert args["blob"] == "<2 bytes>"
    assert args["nested"]["inner"] == ["Decimal('1.5')"]


def test_audit_records_helper_ignores_non_audit_logs(caplog):
    """`audit_records()` 只挑审计行：别的模块的日志不会污染断言。"""
    with caplog.at_level(logging.INFO):
        logging.getLogger("app.db.queries").info("普通日志")
        with audit_tool_call("demo_tool", caller="test"):
            pass

    records = audit_records(caplog.records)
    assert [r["tool"] for r in records] == ["demo_tool"]
