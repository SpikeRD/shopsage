"""工具层的**契约测试**：把「工具是唯一副作用出口」这条设计决策变成可执行检查。

风格与 `test_db_queries.py` 的「编译期检查」一致：用 `inspect` 遍历真实代码，
而不是再抄一遍清单 —— 抄出来的清单一改就过期，遍历真实对象则「新增工具忘写审计」会立刻红。

这里钉住的四条契约，每一条都对应一个具体的失败场景：

| 契约 | 忘了会怎样 |
|---|---|
| 每个公开工具都要求 `caller` | 审计缺字段，事后回答不了「谁干的」 |
| 写路径零件不对外导出 | `mcp/` 或 agent 直接拿到写会话，绕过预定义函数（D1 失守） |
| 读工具不碰写引擎 | 读路径悄悄用写连接的预算，C1 的连接池被读操作吃光 |
| 审计落地的唯一写法是 `audit_tool_call` | 有人手写 `logger.info` 记「审计」，字段与打码规则从此各写一套 |
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import app.tools as tools_pkg
from app.tools import write_path

TOOLS_DIR = Path(tools_pkg.__file__).parent

# 公开工具函数（`__all__` 里的可调用对象，排除异常类与 dataclass 返回值类型）
_EXCLUDED = {
    "PermanentToolError",
    "RefundNotAllowed",
    "RefundRef",
    "RetryableToolError",
    "RetrievedDoc",
    "TicketRef",
    "ToolContractError",
    "ToolError",
    "ToolNotAvailableError",
    "audit_records",
}


def _public_tools() -> dict[str, object]:
    return {
        name: getattr(tools_pkg, name)
        for name in tools_pkg.__all__
        if name not in _EXCLUDED and callable(getattr(tools_pkg, name))
    }


def test_public_tools_are_documented_and_callable():
    """`6.tools.md` §2 的接口必须都在出口上（改名字先在这里红，而不是等 agent 调用失败）。"""
    expected = {
        "execute_readonly_query",
        "retrieve_docs",
        "build_chart_spec",
        "create_ticket",
        "get_ticket_status",
        # 本步新增的退款写路径（文档 §4/§8 要求「退款」也走预定义函数，见 runbook §10.1 坑 2）
        "create_refund_request",
        "get_refund_status",
    }
    assert expected <= set(_public_tools()), f"出口缺少工具：{sorted(expected - set(_public_tools()))}"


def test_every_public_tool_requires_caller_keyword():
    """★审计不可绕过：每个公开工具都必须有一个**必填的关键字参数** `caller`。"""
    missing: list[str] = []
    for name, func in _public_tools().items():
        signature = inspect.signature(func)  # type: ignore[arg-type]
        parameter = signature.parameters.get("caller")
        if parameter is None or parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            missing.append(f"{name}（缺 keyword-only 的 caller）")
        elif parameter.default is not inspect.Parameter.empty:
            missing.append(f"{name}（caller 有默认值，等于可选）")
    assert not missing, "这些工具没有强制的 caller 审计参数：" + "; ".join(missing)


def test_no_public_tool_can_accept_raw_sql_from_llm_without_guard():
    """执行类工具必须显式声明它接收 SQL，且名字里带 readonly —— 防止有人加一个「万能 exec」。"""
    signature = inspect.signature(tools_pkg.execute_readonly_query)
    assert list(signature.parameters)[0] == "sql"
    assert signature.parameters["params"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    # 绝对上限只有一个真相：`security.policy.MAX_ROWS`
    from app.security import policy
    from app.tools.sql_executor import ABSOLUTE_MAX_ROWS

    assert ABSOLUTE_MAX_ROWS == policy.MAX_ROWS


def test_write_path_parts_are_not_exported():
    """写路径零件（写会话/幂等插入）**不对外导出**：能拿到写会话 == 绕过预定义函数。"""
    for name in ("write_session", "insert_once", "validate_idempotency_key"):
        assert name not in tools_pkg.__all__, f"{name} 不该出现在工具出口上"
    assert not hasattr(tools_pkg, "SessionLocal")


def test_only_write_modules_touch_the_write_engine():
    """只有 `write_path.py` 允许引用写会话/写引擎；其余模块要么读，要么纯计算。

    写法是「读源码」而不是「跑一遍看连了哪个库」：静态检查能在 CI 里拦住
    「顺手 import 了 SessionLocal」这种还没造成事故的改动。
    """
    offenders: list[str] = []
    for path in sorted(TOOLS_DIR.glob("*.py")):
        if path.name in {"write_path.py", "__init__.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        for needle in ("from app.db.session import SessionLocal", "get_write_engine"):
            if needle in source:
                offenders.append(f"{path.name} 出现了 {needle}")
    assert not offenders, "非写路径模块碰了写引擎：" + "; ".join(offenders)


def test_read_tools_never_import_llm_or_db_writes():
    """读工具不许 import LLM 门面（工具层不做推理）—— 防止「工具里顺手调模型」把职责搅混。"""
    for name in ("sql_executor.py", "retriever.py", "chart.py"):
        source = (TOOLS_DIR / name).read_text(encoding="utf-8")
        assert "app.llm" not in source, f"{name} 里出现了 app.llm：工具层只做能力封装，不做推理"


def test_every_tool_module_uses_the_shared_audit_helper():
    """审计只有一种写法：`audit_tool_call`。手写 `logger.info` 记审计会导致字段/打码各写一套。"""
    for path in sorted(TOOLS_DIR.glob("*.py")):
        if path.name in {"audit.py", "__init__.py", "errors.py", "write_path.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        if "def " not in source:
            continue
        assert "audit_tool_call" in source, f"{path.name} 里的工具没有接审计"


def test_audit_records_cover_all_tools_in_one_pass(caplog):
    """一次真实调用 = 一条审计记录（不多不少）—— 覆盖离线可跑的两个工具。"""
    import logging

    from app.tools.audit import audit_records

    def fake_search(*, query: str, top_k: int):
        return [{"text": "t", "source": "s.md"}]

    with caplog.at_level(logging.INFO, logger="app.tools.audit"):
        tools_pkg.build_chart_spec([{"k": "a", "v": 1}], x="k", y="v", caller="test")
        tools_pkg.retrieve_docs("退货", search=fake_search, caller="test")

    recorded = [record["tool"] for record in audit_records(caplog.records)]
    assert recorded == ["build_chart", "retrieve_knowledge"]


def test_tool_names_match_the_mcp_exposure_list():
    """工具名与 `15.mcp.md` 对外暴露的名字对齐（第 15 步做薄壳时不用再起一套命名）。"""
    from app.tools.chart import TOOL_NAME as CHART_TOOL
    from app.tools.refund import CREATE_TOOL as REFUND_TOOL
    from app.tools.retriever import TOOL_NAME as RETRIEVE_TOOL
    from app.tools.sql_executor import TOOL_NAME as SQL_TOOL
    from app.tools.ticket import CREATE_TOOL as TICKET_TOOL

    assert {SQL_TOOL, RETRIEVE_TOOL, CHART_TOOL} == {"sql_query", "retrieve_knowledge", "build_chart"}
    assert TICKET_TOOL == "create_ticket"
    assert REFUND_TOOL == "create_refund"  # 15.mcp.md 只列了 4 个，退款是第 6 步补的写路径


def test_idempotency_scope_is_composite_everywhere():
    """幂等键一律是 `(user_id, idempotency_key)` 复合：单列全局唯一会串用户（越权通道）。"""
    from app.tools.refund import IDEMPOTENCY_SCOPE as REFUND_SCOPE
    from app.tools.ticket import IDEMPOTENCY_SCOPE as TICKET_SCOPE

    assert TICKET_SCOPE == REFUND_SCOPE == ("user_id", "idempotency_key")
    assert write_path.MAX_IDEMPOTENCY_KEY_CHARS == 64  # 与模型列宽一致


@pytest.mark.parametrize("bad_key", ["", " ", "a" * 65, "has space", "tab\tkey"])
def test_idempotency_key_validation_rules(bad_key):
    """幂等键校验的边界（写在契约测试里，避免两张表各写一套规则）。"""
    from app.tools.errors import ToolContractError

    with pytest.raises(ToolContractError):
        write_path.validate_idempotency_key(bad_key)
    assert write_path.validate_idempotency_key(" req-1 ") == "req-1"  # 去空白后合法
