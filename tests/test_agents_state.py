"""共享状态与 LLM 零件的用例（`7.agents_module.md` §2 的数据契约 + §4 第 1 条）。

这里测的是**六个节点共同的底座**：state 的取值口径、结构化输出的解析与修复。
它们坏掉时六个 Agent 会一起坏，所以值得单独一组用例。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.agents.agent_llm import (
    AgentOutputError,
    build_system_prompt,
    customer_row_filter_error,
    extract_json_object,
    format_rows,
    generate_json,
    plan_json_call,
    repair_prompt,
)
from app.agents.state import (
    CUSTOMER,
    ROUTES,
    GraphState,
    MissingIdentityError,
    inject_identity,
    is_customer_path,
    last_user_message,
    messages_of,
    permissions_of,
    require_user_id,
    role_of,
)
from app.security.rbac import ROLE_PERMISSIONS
from tests.agents_helpers import FakeLLM

# ── state 的取值口径 ─────────────────────────────────────────────────────────


def test_routes_cover_the_documented_branches() -> None:
    """取值域必须与文档里的三类检索 + 写操作 + 直达出口对齐。"""
    assert set(ROUTES) == {"data", "rag", "analysis", "ticket", "final"}


def test_last_user_message_prefers_the_newest_user_turn() -> None:
    state: GraphState = {
        "messages": [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
        ]
    }
    assert last_user_message(state) == "第二问"


def test_last_user_message_ignores_assistant_only_history() -> None:
    state: GraphState = {"messages": [{"role": "assistant", "content": "在的"}]}
    assert last_user_message(state) == ""


def test_messages_of_tolerates_garbage() -> None:
    """state 是跨节点传递的**弱类型载体**：取值处必须容忍缺键与类型不符。"""
    assert messages_of({}) == []
    assert messages_of({"messages": "不是列表"}) == []  # type: ignore[typeddict-item]
    assert messages_of({"messages": [{"role": "user", "content": "x"}]}) == [
        {"role": "user", "content": "x"}
    ]


def test_inject_identity_comes_from_the_rbac_table() -> None:
    """权限**只从 `rbac.ROLE_PERMISSIONS` 来**：这条是 D1 在 agent 层的边界。"""
    injected = inject_identity(7, "customer_service")
    assert injected["user_id"] == 7
    assert injected["role"] == "customer_service"
    assert injected["permissions"] == ROLE_PERMISSIONS["customer_service"]
    assert injected["is_customer"] is False


def test_inject_identity_unknown_role_is_zero_permission(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="app.agents.state"):
        injected = inject_identity(1, "intern")
    assert injected["permissions"] == frozenset()
    assert any("未在 rbac.ROLE_PERMISSIONS 里登记" in r.getMessage() for r in caplog.records)


def test_permissions_of_falls_back_to_role_lookup() -> None:
    """state 里没写 `permissions` 时按角色现查（而不是当成空集）。

    当成空集会让「只传 role 的调用方」拿到零权限，症状是「明明是对客查询却报权限不足」——
    让人去怀疑 RBAC 表，而真正的原因是状态没填。
    """
    assert permissions_of({"role": "customer"}) == ROLE_PERMISSIONS["customer"]
    assert permissions_of({"permissions": frozenset({"x"})}) == frozenset({"x"})


def test_role_of_defaults_to_customer() -> None:
    """缺 role 时按**对客**处理：最保守的假设（看不到全站数据）。"""
    assert role_of({}) == CUSTOMER
    assert is_customer_path({}) is True
    assert is_customer_path({"is_customer": False}) is False
    assert is_customer_path({"role": "manager"}) is False


def test_require_user_id_rejects_missing_and_boolean() -> None:
    assert require_user_id({"user_id": 5}) == 5
    for bad in ({}, {"user_id": None}, {"user_id": True}, {"user_id": "1"}):
        with pytest.raises(MissingIdentityError):
            require_user_id(bad)  # type: ignore[arg-type]


# ── 结构化输出的解析与修复 ───────────────────────────────────────────────────


def test_extract_json_object_plain_and_fenced() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('好的，结果是：\n{"a": 1}\n以上。') == {"a": 1}


def test_extract_json_object_errors_are_self_explanatory() -> None:
    """报错要带**原因**与**原文**：只抛「解析失败」，排障就得去翻 provider 原始响应。"""
    with pytest.raises(AgentOutputError) as empty:
        extract_json_object("   ")
    assert "空内容" in str(empty.value) and empty.value.reason == "empty"

    with pytest.raises(AgentOutputError) as not_json:
        extract_json_object("我认为应该查订单表")
    assert not_json.value.reason == "not_json"
    assert not_json.value.raw == "我认为应该查订单表"

    with pytest.raises(AgentOutputError) as wrong_root:
        extract_json_object("[1, 2, 3]")
    assert "顶层不是对象" in str(wrong_root.value)


def test_repaired_prompt_carries_the_real_problem() -> None:
    """§5 第三条：回喂的必须是**具体报错**，不是「再试一次」。"""
    base = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    first, is_repair = plan_json_call(base)
    assert first == base and is_repair is False

    repaired, is_repair = plan_json_call(base, raw="我答不上来", problem=repair_prompt("not_json"))
    assert is_repair is True
    assert repaired[:2] == base, "修复轮不能丢掉原始 prompt（few-shot 会被挤没）"
    assert repaired[-2] == {"role": "assistant", "content": "我答不上来"}
    assert "not_json" in repaired[-1]["content"]
    assert "只**重新输出一个 JSON 对象" in repaired[-1]["content"]


async def test_generate_json_accepts_sync_and_async_fakes() -> None:
    """假 LLM 同步写也行、真 LLM 异步也行（否则每个测试都要包一层 async）。"""

    def sync_fake(messages, **kwargs):  # noqa: ANN001, ANN003, ANN202
        return {"route": "rag"}

    async def async_fake(messages, **kwargs):  # noqa: ANN001, ANN003, ANN202
        return json.dumps({"route": "rag"})

    assert await generate_json([], llm=sync_fake, agent="t") == {"route": "rag"}
    assert await generate_json([], llm=async_fake, agent="t") == {"route": "rag"}


async def test_generate_json_repairs_once_then_raises_with_evidence() -> None:
    """修复一次仍失败 → 抛 `AgentOutputError`，并带上原文与尝试次数。"""
    llm = FakeLLM("不是 JSON", "还是不是 JSON")
    with pytest.raises(AgentOutputError) as exc:
        await generate_json([], llm=llm, agent="router")
    assert exc.value.agent == "router"
    assert exc.value.attempts == 2
    assert exc.value.raw == "还是不是 JSON"


async def test_generate_json_repairs_zero_times_when_asked() -> None:
    llm = FakeLLM("不是 JSON")
    with pytest.raises(AgentOutputError):
        await generate_json([], llm=llm, agent="router", repairs=0)
    assert llm.call_count == 1


def test_build_system_prompt_keeps_contract_separate() -> None:
    """输出契约进 system（不变量），与 user 消息（变量）分开 —— 修复轮追加时不会挤掉它。"""
    prompt = build_system_prompt("你是分流器", output_schema='{"route": "..."}')
    assert prompt.startswith("你是分流器")
    assert "输出格式（必须严格遵守）" in prompt
    assert '{"route": "..."}' in prompt


def test_format_rows_handles_decimal_and_datetime() -> None:
    """`format_rows` 走 `str()`：`json.dumps` 会在 `Decimal`/`datetime` 上直接 TypeError。"""
    import datetime as dt

    rows = [{"order_id": 1, "total": Decimal("2745.98"), "created": dt.datetime(2026, 9, 29)}]
    text = format_rows(rows)
    assert "order_id | total | created" in text
    assert "2745.98" in text and "2026-09-29" in text

    with pytest.raises(TypeError):
        json.dumps(rows[0])


def test_format_rows_states_truncation_and_empty() -> None:
    rows = [{"n": i} for i in range(5)]
    text = format_rows(rows, limit=2)
    assert "共 5 行" in text and "只展示前 2 行" in text
    assert format_rows([]) == "（查询结果是 0 行）"


# ── NL→SQL 的行级过滤自检 ────────────────────────────────────────────────────


def test_customer_row_filter_accepts_bound_placeholder() -> None:
    assert customer_row_filter_error("SELECT 1 FROM orders WHERE user_id = :uid LIMIT 5", user_id=1) is None
    assert (
        customer_row_filter_error("SELECT 1 FROM orders WHERE :uid = user_id LIMIT 5", user_id=1) is None
    ), "谁在等号左边不该被假设"


def test_customer_row_filter_rejects_literal_and_missing() -> None:
    literal = customer_row_filter_error("SELECT 1 FROM orders WHERE user_id = 1 LIMIT 5", user_id=2)
    assert literal and "字面量" in literal and "'uid': 2" in literal

    missing = customer_row_filter_error("SELECT 1 FROM orders WHERE status = 'paid' LIMIT 5", user_id=2)
    assert missing and "没有 `user_id = 绑定参数`" in missing

    no_where = customer_row_filter_error("SELECT count(*) FROM orders LIMIT 5", user_id=2)
    assert no_where and "没有 WHERE 子句" in no_where


def test_customer_row_filter_requires_outermost_where() -> None:
    """子查询里的 `user_id = :uid` 不算：外层仍可能全表扫。"""
    sql = (
        "SELECT o.order_id FROM orders o "
        "JOIN (SELECT order_id, user_id FROM orders WHERE user_id = :uid LIMIT 10) x "
        "ON x.order_id = o.order_id WHERE o.status = 'paid' LIMIT 10"
    )
    assert customer_row_filter_error(sql, user_id=1) is not None


def test_customer_row_filter_rejects_unparsable_sql() -> None:
    error = customer_row_filter_error("SELECT FROM WHERE", user_id=1)
    assert error and "无法解析" in error


def _agents_sources() -> list[tuple[str, str]]:
    """(文件名, 源码) 列表。"""
    from pathlib import Path

    return [(path.name, path.read_text(encoding="utf-8")) for path in sorted(Path("app/agents").glob("*.py"))]


def _imported_modules(source: str) -> set[str]:
    """源码里**真正被 import** 的模块名（AST 解析，不看注释与文档字符串）。

    ⚠️ 必须用 AST 而不是 `"litellm" in source`：本仓的文档字符串里到处在**解释**
    「不要绕过 litellm 门面」，纯文本匹配会把说明一起当成违规
    （实测第一次就撞上了：三个文件的说明文字被当成绕过证据）。
    """
    import ast

    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module.split(".")[0])
    return modules


def test_agents_layer_does_not_talk_to_litellm_directly() -> None:
    """`app/llm` 是模型调用的**唯一**出口（`5.llm.md` 第 1 节的硬约束）。

    绕过去的调用不会经过门面，于是重试/退避/熔断/计费全部失守。
    """
    offenders = [
        name
        for name, source in _agents_sources()
        if {"litellm", "openai"} & _imported_modules(source)
    ]
    assert offenders == [], f"这些 agent 文件绕过了 app.llm 门面：{offenders}"


def test_agents_layer_never_opens_a_write_session() -> None:
    """Agent 不许自己开写会话（写路径只有一个出口：`tools/` 的预定义函数）。"""
    offenders: list[str] = []
    for name, source in _agents_sources():
        imports = _imported_modules(source)
        if {"write_path"} & imports or "SessionLocal(" in source:
            offenders.append(name)
    assert offenders == [], f"这些 agent 文件直接碰了写路径：{offenders}"


def test_agents_package_does_not_require_langgraph() -> None:
    """第 7 步必须能在没有 langgraph 的环境里 import（依赖属第 8 步）。"""
    offenders = [
        name
        for name, source in _agents_sources()
        if {"langgraph", "langchain", "langchain_core"} & _imported_modules(source)
    ]
    assert offenders == [], f"这些 agent 文件引入了第 8 步的依赖：{offenders}"


def test_agents_layer_only_reaches_out_through_app_modules() -> None:
    """Agent 的第三方依赖面必须很窄：只允许 stdlib + sqlglot（NL→SQL 自检用）。

    这条不是洁癖：agent 层每多一个重依赖，「六个 Agent 独立可测」的成本就高一截。
    """
    allowed = {
        "__future__",
        "collections",
        "dataclasses",
        "decimal",
        "inspect",
        "json",
        "logging",
        "re",
        "sqlglot",
        "typing",
    }
    seen: set[str] = set()
    for _name, source in _agents_sources():
        for module in _imported_modules(source):
            if module in allowed or module in {"app", "tests"}:
                continue
            seen.add(module)
    assert seen == set(), f"agent 层出现了计划外依赖：{sorted(seen)}"


def test_importing_agents_does_not_require_the_database() -> None:
    """六个 Agent 的 import 不许依赖数据库连接（否则「独立可测」不成立）。

    用**子进程**跑一次全新解释器：同进程里 `app.db.session` 早就建好连接池了，
    在同一进程内怎么断言都证明不了「新环境能不能 import」。
    """
    import subprocess
    import sys

    code = "import app.agents; print('ok')"
    result = subprocess.run(  # noqa: S603 - 固定参数，无外部输入
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
