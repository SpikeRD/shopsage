"""Data Agent 的用例：NL→SQL 的校验、重试回喂、降级，以及真库端到端。

对应文档用例：
    test_data_retries_then_gives_up()  # 非法 SQL 重试 N 次后返回降级答案
    test_data_sql_passes_guard()       # 生成的 SQL 一定先过 validate_sql
"""

from __future__ import annotations

import logging

import pytest

from app.agents import data as data_mod
from app.agents.data import (
    MAX_SQL_ATTEMPTS,
    allowed_tables,
    build_data_messages,
    data_node,
    plan_sql,
    schema_text,
)
from app.security.sql_guard import SQLSecurityError
from tests.agents_helpers import FakeLLM, internal_state, schema_reply, state_of

VALID_SQL = (
    "SELECT order_id, status, total_amount FROM orders "
    "WHERE user_id = :uid ORDER BY order_id LIMIT :n"
)

# ── schema 与 prompt ─────────────────────────────────────────────────────────


def test_schema_text_lists_all_allowed_tables() -> None:
    """schema 必须覆盖 `policy.ALLOWED_TABLES` 里**每一张**表。

    漏一张的症状是「模型永远说没有这张表」，而代码里明明有 ——
    生成式 prompt 的这类腐烂只能靠断言挡住。
    """
    text = schema_text()
    for table in allowed_tables():
        assert f"{table}(" in text, f"schema 里缺表 {table}"
    assert len(text.splitlines()) == len(allowed_tables())


def test_schema_text_hides_protected_and_internal_columns() -> None:
    """受保护列与内部列不进 prompt（减少无效尝试，也不白送探测信息）。

    ⚠️ 不列出**不等于**不拦：真正的拒绝在 `validate_sql` 里。
    这条用例只断言「没写进 prompt」，别误读成安全边界。
    """
    text = schema_text()
    for column in ("phone", "email", "address", "id_card", "operator_id", "idempotency_key"):
        assert column not in text, f"{column} 不该出现在 prompt 的 schema 里"
    assert "orders(" in text and "total_amount" in text


def test_prompt_carries_few_shot_and_hard_constraints() -> None:
    """few-shot（3~5 条）与六条硬约束必须真的进 prompt。"""
    messages = build_data_messages("我的订单", is_customer=True, user_id=7)
    system = messages[0]["content"]

    assert len(data_mod.SQL_EXAMPLES) >= 3
    for question, sql in data_mod.SQL_EXAMPLES:
        assert question in system and sql in system
    for keyword in ("单条 SELECT", "WHERE", "LIMIT", "绑定参数", "user_id = :uid"):
        assert keyword in system
    assert "user_id=7" in system  # 对客模板要带上当前身份


def test_prompt_repair_round_echoes_previous_output() -> None:
    """修复轮 = 追加 assistant 回显 + 一条指明问题的 user 消息。"""
    messages = build_data_messages(
        "我的订单", is_customer=True, user_id=1, problem="缺少 WHERE", previous='{"sql": "SELECT 1"}'
    )
    assert messages[-2] == {"role": "assistant", "content": '{"sql": "SELECT 1"}'}
    assert messages[-1]["role"] == "user" and "缺少 WHERE" in messages[-1]["content"]


# ── 校验与重试（不碰库、不碰真模型）──────────────────────────────────────────


async def test_data_sql_passes_guard() -> None:
    """文档用例：能执行的那条 SQL **一定**先过了 `validate_sql`。

    做法：注入一个假执行器替身，把 `data_node` 真正会执行的那条 SQL 记下来，
    再单独用它跑一次守卫 —— 若实现里漏了校验，这条断言会红。
    """
    captured: dict[str, object] = {}

    def fake_execute(sql, params=None, *, max_rows=None, caller):  # noqa: ANN001, ANN202
        captured.update(sql=sql, params=params, max_rows=max_rows, caller=caller)
        return [{"order_id": 1, "status": "paid", "total_amount": 10}], {"audit_id": "tc-x", "row_count": 1}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(data_mod, "execute_readonly_query", fake_execute)
    try:
        out = await data_node(
            state_of("我的订单", user_id=1),
            llm=FakeLLM(schema_reply(VALID_SQL, uid=999, n=10)),
        )
    finally:
        monkey.undo()

    assert out["sql"] == VALID_SQL
    assert captured["sql"] == VALID_SQL
    # ① 那条 SQL 能过守卫（等于「执行前校验过」这句话的可验证版本）
    from app.security.sql_guard import validate_sql

    validate_sql(str(captured["sql"]), max_rows=50)
    # ② 身份绑定值由**程序**覆写：模型给的 uid=999 不许生效
    assert captured["params"] == {"uid": 1, "n": 10}
    assert captured["caller"] == "agent:data"


async def test_data_retries_then_gives_up() -> None:
    """文档用例：非法 SQL 重试 N 次后**降级**（返回 refusals，不抛异常）。"""
    bad = schema_reply("SELECT order_id FROM orders LIMIT :n", n=10)  # 没有 WHERE
    llm = FakeLLM(bad)
    state = state_of("我的订单", user_id=1)

    out = await data_node(state, llm=llm, max_attempts=3)

    assert llm.call_count == 3, "应当用满重试预算"
    assert out["rows"] == [] and out["sql"] == ""
    assert out["sql_attempts"] == 3
    assert len(out["refusals"]) == 1 and "降级为无法查询" in out["refusals"][0]
    # 每次失败的具体规则都留在 row_meta 里（第 11 步排障要看它）
    assert any("missing_where" in e for e in out["row_meta"]["errors"])


async def test_data_repairs_on_guard_error() -> None:
    """第一次没有 WHERE → 守卫拦下 → 把**具体规则**回喂 → 第二次通过。"""
    llm = FakeLLM(
        schema_reply("SELECT order_id FROM orders LIMIT :n", n=5),
        schema_reply(VALID_SQL, uid=1, n=5),
    )
    state = state_of("我的订单", user_id=1)

    out = await data_node(state, llm=llm)

    assert out["sql_attempts"] == 2
    assert out["sql"] == VALID_SQL
    repair = llm.calls[1][-1]["content"]
    assert "missing_where" in repair, "回喂的必须是具体规则名，而不是「再试一次」"
    assert "不要用 `WHERE 1=1`" in repair, "回喂里要有可照做的动作"


async def test_data_customer_row_filter_is_enforced_and_retried() -> None:
    """对客 SQL 缺 `user_id = :uid` 时被本节点拦下并回喂（守卫查不出这一条）。"""

    def fake_execute(sql, params=None, *, max_rows=None, caller):  # noqa: ANN001, ANN202
        return [], {"row_count": 0}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(data_mod, "execute_readonly_query", fake_execute)
    try:
        llm = FakeLLM(
            schema_reply("SELECT order_id FROM orders WHERE status = 'paid' LIMIT :n", n=5),
            schema_reply(VALID_SQL, uid=1, n=5),
        )
        out = await data_node(state_of("已支付的订单", user_id=1), llm=llm)
    finally:
        monkey.undo()

    assert out["sql_attempts"] == 2
    repair = llm.calls[1][-1]["content"]
    assert "user_id = :uid" in repair
    # 第一条 SQL **确实**过了守卫（说明拦它的是行级过滤自检，不是守卫）
    from app.security.sql_guard import validate_sql

    validate_sql("SELECT order_id FROM orders WHERE status = 'paid' LIMIT :n")


async def test_data_literal_user_id_is_rejected() -> None:
    """把身份写死在 SQL 文本里（`user_id = 1`）不算行级过滤：必须用绑定参数。"""
    llm = FakeLLM(schema_reply("SELECT order_id FROM orders WHERE user_id = 1 LIMIT :n", n=5))
    out = await data_node(state_of("我的订单", user_id=1), llm=llm, max_attempts=2)

    assert out["rows"] == []
    assert any("customer_row_filter" in e for e in out["row_meta"]["errors"])


async def test_data_internal_path_does_not_require_row_filter() -> None:
    """内部路径不强制 `user_id`：跨用户查询是它的正常工作。

    ⚠️ 这不代表「内部随便查」—— 范围与敏感级由 RBAC 与脱敏出口管（第 4 步），
    data 节点只负责「不把对客规则错误地套到对内路径上」。
    """

    def fake_execute(sql, params=None, *, max_rows=None, caller):  # noqa: ANN001, ANN202
        return [{"order_id": 1}], {"row_count": 1, "truncated": False}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(data_mod, "execute_readonly_query", fake_execute)
    try:
        out = await data_node(
            internal_state("今天有多少订单"),
            llm=FakeLLM(
                schema_reply(
                    "SELECT count(*) AS n FROM orders WHERE created_at > :since LIMIT :n",
                    since="2026-01-01",
                    n=10,
                )
            ),
        )
    finally:
        monkey.undo()

    assert out["sql_attempts"] == 1
    assert out["row_meta"]["row_count"] == 1


async def test_data_without_read_permission_never_calls_llm() -> None:
    """没有 `read:own` 权限时**连 LLM 都不调**：不该为一个必然被拒的请求花钱。"""
    llm = FakeLLM(schema_reply(VALID_SQL, uid=1, n=5))
    out = await data_node(state_of("我的订单", user_id=1, role="ghost"), llm=llm)

    assert llm.call_count == 0
    assert "read:own" in out["refusals"][0]


def test_prompt_few_shot_params_are_real_values_not_placeholders() -> None:
    """few-shot 里的「绑定参数」必须是**真实值**，不能是 `<值>` 这类占位符。

    ⚠️ 这条是第 16 步（Web 端）实测撞出来的（`docs/runbook/16-web-runbook.md` §10.1 坑 5）：
    第一版示例写的是 `{'uid': <值>, 'n': <值>}`，模型**照抄尖括号**，于是
    `LIMIT :n` 绑成字符串 `"<值>"` → Postgres 报 `invalid input syntax for type bigint`；
    带时间的查询绑成 `"<统计起始时间，如 2024-01-01>"` → `InvalidDatetimeFormat`。
    两种都让查询查回 0 行，而界面上看起来像「库里没数据」。
    示例教坏了模型，所以这条用例钉的是**示例本身**。
    """
    system = build_data_messages("我最近的三笔订单", is_customer=True, user_id=2)[0]["content"]
    assert "<值>" not in system, "示例里的绑定参数又变成占位符了（模型会照抄）"
    # 示例必须真的展示「uid 绑的是程序给的身份、n 绑的是整数」
    params_line = [line for line in system.splitlines() if line.startswith("绑定参数：")]
    assert params_line, "示例缺少「绑定参数」那一行"
    for line in params_line:
        assert "<" not in line and ">" not in line, f"这行示例里有尖括号占位符：{line}"


async def test_data_retries_when_bound_params_are_placeholders() -> None:
    """绑定参数是占位符 → 回喂报错重写，**不让它落到数据库层**。

    占位符形状的 params 会通过守卫与行级过滤自检（形状完全合规），
    只有数据库执行时才炸 —— 那时已经浪费了一次 LLM 调用与一次连接。
    """
    llm = FakeLLM(
        schema_reply("SELECT status, count(*) AS c FROM orders WHERE status IS NOT NULL GROUP BY status LIMIT :n", n="<值>"),
        schema_reply("SELECT status, count(*) AS c FROM orders WHERE status IS NOT NULL GROUP BY status LIMIT :n", n=20),
    )
    plan = await plan_sql("按状态统计订单", is_customer=False, user_id=7, llm=llm)

    assert plan.ok is True
    assert plan.attempts == 2, "第一次的占位符参数应当被拦下并要求重写"
    assert plan.params["n"] == 20
    assert any("placeholder_params" in error for error in plan.errors)
    # 回喂给模型的话必须说清「怎么改」
    assert "真实值" in llm.calls[1][-1]["content"]


async def test_data_gives_up_on_persistent_placeholder_params() -> None:
    """一直给占位符 → 用尽预算后降级（**不是**无限重试，也不是静默通过）。"""
    llm = FakeLLM(
        schema_reply("SELECT status, count(*) AS c FROM orders WHERE status IS NOT NULL GROUP BY status LIMIT :n", n="<返回的状态种类上限>")
    )
    plan = await plan_sql("按状态统计订单", is_customer=False, user_id=7, llm=llm)
    assert plan.ok is False
    assert plan.attempts == MAX_SQL_ATTEMPTS
    assert any("placeholder_params" in error for error in plan.errors)


def test_placeholder_param_names_detects_the_shapes_we_actually_saw() -> None:
    """识别器必须覆盖实测见过的形状，且**不误伤**真值。"""
    from app.agents.data import placeholder_param_names

    # 实测见过的（第 16 步日志原文）
    assert placeholder_param_names({"since": "<统计起始时间，如 2024-01-01>"}) == ("since",)
    assert placeholder_param_names({"n": "<值>"}) == ("n",)
    assert placeholder_param_names({"n": "<返回的状态种类上限>"}) == ("n",)
    assert placeholder_param_names({"n": "值"}) == ("n",)
    assert placeholder_param_names({"n": "xxx"}) == ("n",)
    # 真值不许被误伤
    assert placeholder_param_names({"n": 20, "uid": 2}) == ()
    assert placeholder_param_names({"since": "2026-01-01", "status": "delivered"}) == ()
    assert placeholder_param_names({"q": "小于 5 件"}) == ()


def test_retry_actions_cover_every_guard_rule() -> None:
    """`RETRY_ACTIONS` 覆盖**守卫的每一条**规则，且不留死条目。

    两个方向都查：
      * 守卫有、表里没有 → 该规则会落到兜底话术（= 「笼统说再试一次」，§5 第三条要避免的）；
      * 表里有、守卫没有 → 死条目（规则改名后留下的化石，会让人以为还支持）。

    规则清单从 `sql_guard` 的源码里抽（`rule="..."` 字面量），
    所以守卫新增规则时这条会红 —— 那正是提醒你回来补一行映射。
    """
    import re
    from pathlib import Path

    import app.security.sql_guard as guard_mod

    source = Path(guard_mod.__file__).read_text(encoding="utf-8")
    guard_rules = set(re.findall(r'rule="([a-z_]+)"', source))

    assert guard_rules, "没能从 sql_guard 源码里抽到规则名（正则或写法变了，先修用例）"
    assert guard_rules <= set(data_mod.RETRY_ACTIONS), (
        f"这些守卫规则没有登记修复动作：{sorted(guard_rules - set(data_mod.RETRY_ACTIONS))}"
    )
    assert set(data_mod.RETRY_ACTIONS) <= guard_rules, (
        f"这些修复动作对应的规则已不存在（死条目）：{sorted(set(data_mod.RETRY_ACTIONS) - guard_rules)}"
    )


def test_retry_hint_maps_every_guard_rule() -> None:
    """每条已登记规则的提示里都带规则名与可照做的动作。"""
    for rule, action in data_mod.RETRY_ACTIONS.items():
        error = SQLSecurityError(f"测试用报错 {rule}", rule=rule, sql="SELECT 1")
        hint = data_mod._retry_hint(error, is_customer=True)
        assert rule in hint
        assert action in hint, f"规则 {rule} 的动作没进提示"
    # 未登记的规则走兜底，并且**必须留 WARNING**（否则漏登记是静默的）
    unknown = SQLSecurityError("未登记的规则", rule="brand_new_rule", sql="SELECT 1")
    hint = data_mod._retry_hint(unknown, is_customer=False)
    assert "请按 PostgreSQL 只读查询的规范重写。" in hint
    assert "另外" not in hint  # 非对客路径不加行级过滤要求


async def test_data_plan_reports_attempts_and_raw_outputs() -> None:
    """`DataPlan` 把「试了几次、每次原始输出」都带回来（排障与评测都要）。"""
    llm = FakeLLM(
        schema_reply("DROP TABLE orders"),
        schema_reply(VALID_SQL, uid=1, n=5),
    )
    plan = await plan_sql("我的订单", is_customer=True, user_id=1, llm=llm)

    assert plan.ok and plan.attempts == 2
    assert len(plan.raw_outputs) == 2
    assert any("not_read_only" in e for e in plan.errors)


async def test_data_zero_budget_degrades_without_calling_llm() -> None:
    """`max_attempts=0` 是「不要重试」而不是「无限重试」。"""
    llm = FakeLLM(schema_reply(VALID_SQL, uid=1, n=5))
    plan = await plan_sql("我的订单", is_customer=True, user_id=1, llm=llm, max_attempts=0)
    assert plan.ok is False and plan.attempts == 0 and llm.call_count == 0


async def test_data_logs_degradation_as_warning(caplog) -> None:
    """降级必须留下 WARNING：它是 A1（可降级）的唯一可观测信号。"""
    with caplog.at_level(logging.WARNING, logger="app.agents.data"):
        await data_node(
            state_of("我的订单", user_id=1),
            llm=FakeLLM(schema_reply("SELECT order_id FROM orders LIMIT :n", n=1)),
            max_attempts=1,
        )
    assert any("data 降级" in r.getMessage() for r in caplog.records)


# ── 真库端到端（第 6 步的工具 + 第 4 步的只读角色 + 第 3 步的种子数据）───────


@pytest.mark.db
async def test_data_node_end_to_end_against_real_database(seeded) -> None:
    """真库端到端：NL→SQL→校验→执行，读的是种子数据里真实存在的行。

    这条用例的价值在于**把所有环节接起来验一次**：
    守卫放行的 SQL、执行层的绑定参数、只读引擎、审计 id —— 任何一环错位都会红。
    """
    sql = (
        "SELECT order_id, status, total_amount FROM orders "
        "WHERE user_id = :uid ORDER BY order_id LIMIT :n"
    )
    out = await data_node(
        state_of("我最近的订单", user_id=1),
        llm=FakeLLM(schema_reply(sql, uid=1, n=5)),
    )

    assert out["sql"] == sql
    assert out["sql_attempts"] == 1
    assert out["refusals"] == []
    assert out["rows"], "种子数据里 user_id=1 应当有订单"
    assert all(row["status"] for row in out["rows"])
    meta = out["row_meta"]
    assert meta["truncated"] is False
    assert meta["tables"] == ["orders"]
    assert str(meta["audit_id"]).startswith("tc-")


@pytest.mark.db
async def test_data_node_reports_other_users_rows_when_llm_lies(seeded) -> None:
    """模型把 uid 写成别人的 id 也没用：绑定值由程序覆写（身份不能自证）。

    这是 D1 在节点层的证据：SQL 形状合法、模型「想查别人」，
    最终查到的是**当前用户**的行。
    """
    sql = "SELECT order_id, user_id FROM orders WHERE user_id = :uid LIMIT :n"
    out = await data_node(
        state_of("查一下订单", user_id=2),
        llm=FakeLLM(schema_reply(sql, uid=1, n=20)),  # 模型故意给 uid=1
    )

    assert out["rows"], "user_id=2 应当也订单"
    assert {row["user_id"] for row in out["rows"]} == {2}


async def test_data_node_requires_user_id() -> None:
    """没有 user_id 的 state 直接拒（行级过滤没有依据，不许用 0 兜底）。"""
    from app.agents.state import MissingIdentityError

    state = state_of("我的订单", user_id=1)
    state.pop("user_id")
    with pytest.raises(MissingIdentityError):
        await data_node(state, llm=FakeLLM(schema_reply(VALID_SQL, uid=1, n=1)))


def test_max_sql_attempts_is_a_hard_cap_in_code() -> None:
    """重试预算是**代码里的常量**，不是配置项：调用方（含被 LLM 影响的路径）改不大它。"""
    assert MAX_SQL_ATTEMPTS == 3
    import inspect

    parameters = inspect.signature(data_node).parameters
    assert parameters["max_attempts"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["max_attempts"].default == MAX_SQL_ATTEMPTS
