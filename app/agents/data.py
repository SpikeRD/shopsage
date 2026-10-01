"""Data Agent：**NL→SQL → 校验 → 执行 → 报错重试**（`7.agents_module.md` §4 第 3 条、§5 第二/三条）。

一条链路四步，缺一不可
----------------------

```
问题 + schema + few-shot
        │
        ▼
   ① 生成 SQL（LLM）──────────────┐
        │                         │ 校验失败：把**具体报错**（规则名 + 原文）
        ▼                         │ 追加进 prompt，让它**针对性**重写
   ② validate_sql（守卫，第 4 步）  │（最多 MAX_SQL_ATTEMPTS 次）
        │  通过                    │
        ▼                         │
   ③ 对客行级过滤自检（本模块）─────┘
        │  通过
        ▼
   ④ execute_readonly_query（工具，第 6 步）
        │  工具内部**再验一遍** SQL（纵深防御）
        ▼
      rows + meta（含 audit_id）
```

三条刻意设计
------------

1. **生成与执行分开，中间夹校验**：绝不允许「模型给什么就执行什么」。
   守卫（`sql_guard`）是静态的（拦写语句/白名单/无 WHERE/超行数），
   工具里的二次校验是纵深防御（工具会被 MCP 等路径直接调用，见第 6 步的说明）。
   本模块校验一次不是为了替代它们，而是为了**在花钱查库之前**拿到可回喂的报错。

2. **重试的砝码是「具体报错」，不是「再试一次」**：`7.agents_module.md` §5 第三条。
   `_retry_hint()` 把 `SQLSecurityError.rule` 翻成一句**可照做**的修复指示
   （`missing_where` → 「补真实过滤条件，不要用 1=1」）。
   实测：只回喂「SQL 不合规」时，模型大概率原样重写一遍同样的 SQL。

3. **重试用尽就降级，不抛异常**：`DataPlan` 返回 `ok=False` + `reason`，
   由 `data_node` 落成 `refusals`，synthesis 据此如实回答「暂时查不到」。
   抛异常会让整张图失败（第 8 步），而用户其实只需要一句实话（A1 思想的节点级应用）。

⚠️ 本模块**不写 SQL**（那是模型的事）、**不碰数据库**（那是 `tools/sql_executor.py` 的事）、
**不做脱敏**（那是 `security/masking.py` 在出口做的事）。它只负责编排这四步。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.agents.agent_llm import (
    JsonLLM,
    build_system_prompt,
    customer_row_filter_error,
    generate_json,
)
from app.agents.state import GraphState, is_customer_path, last_user_message, require_user_id
from app.security.policy import PROTECTED_COLUMNS
from app.security.sql_guard import SQLSecurityError, validate_sql
from app.tools import execute_readonly_query
from app.tools.audit import audit_tool_call
from app.tools.errors import ToolError

logger = logging.getLogger(__name__)

#: 生成尝试次数上限（含首发）。**为什么是 3**：
#: 第一次失败通常是「忘了 WHERE/LIMIT」这类可修复问题；第二次仍失败说明模型没理解
#: 约束（继续重试是白花钱）；再留一次给「模型被具体报错点醒」的情形。
#: 上限写死而不是做成配置项：调用方（含被 LLM 影响的路径）不该能把它调大，
#: 否则「重试预算」就变成了 C1（成本与资源）的开口。
MAX_SQL_ATTEMPTS: Final[int] = 3

#: 审计里的 caller（与 `6.tools-runbook.md` §3.3 的取值约定一致）
CALLER: Final[str] = "agent:data"

#: 塞进 prompt 的结果行数上限（E2：上下文与成本）。**不是**查询上限 ——
#: 查询上限由工具的 `max_rows`（≤200）管；这里只是「给模型看多少行」。
PROMPT_ROWS: Final[int] = 20

#: `data_node` 默认向工具要多少行。刻意**小于**工具的绝对上限 200：
#: 用户问「我的订单」时要的是答案不是数据导出，200 行既贵又没意义。
DEFAULT_ROW_LIMIT: Final[int] = 50

#: 刻意**不进 prompt** 的列（在 `PROTECTED_COLUMNS` 之外的两类）。
#:
#: * `idempotency_key` 是写路径的内部实现细节（第 6 步的幂等键，取值是请求 id）。
#:   它没有任何业务含义，但**可用于探测**「某个请求是否发生过」——
#:   属于「不该白白告诉模型」的信息（第一英里的最小暴露，与 `4.security.md` 同一口径）。
#:   需要按幂等键排查时走 `tools/audit.py` 的审计日志，而不是让模型查库。
HIDDEN_COLUMNS: Final[frozenset[str]] = frozenset({"idempotency_key"})


def schema_text() -> str:
    """渲染可查的表/列清单（few-shot 的 schema 部分，`7.agents_module.md` §5 第二条）。

    为什么从 `models` 的 `__table__` 反射生成，而不是在 prompt 里手写一段 DDL：
    手写的 schema 会**悄悄过期** —— 加了一列之后模型永远不知道，
    症状是「模型一直说没有这列」，而代码里明明有。反射生成让 schema 只有一处真相。
    实测生成的文本已包含 8 张业务表，见 `docs/runbook/7.agents-runbook.md` §2.1。

    ⚠️ **受保护列（`policy.PROTECTED_COLUMNS`）刻意不列出**：`users.phone` / `email` /
    `address` / `id_card` 与 `stock_movements.operator_id` 一律写不进 SQL（守卫会拒），
    列进 prompt 只会让模型反复尝试、白白消耗重试预算。
    **不列 ≠ 不拦**：真正的拒绝在 `validate_sql` 里（这一处只是减少无效尝试）。
    """
    from app.db.base import Base
    import app.db.models  # noqa: F401 - 触发模型注册（否则 Base.metadata 是空的）

    lines: list[str] = []
    for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
        protected = PROTECTED_COLUMNS.get(table.name, frozenset())
        columns = [
            c.name for c in table.columns if c.name not in protected and c.name not in HIDDEN_COLUMNS
        ]
        lines.append(f"{table.name}({', '.join(columns)})")
    return "\n".join(lines)


#: few-shot 示例（「(问题, 正确 SQL)」成对）——`7.agents_module.md` §5 第二条要求 3~5 条。
#: 选例标准：① 都对客（本系统最高频、也最容易被越权）；② 每条覆盖一条守卫规则
#: （行级过滤 / LIMIT / 参数绑定 / 聚合 / 主键等值），让「正确的形状」被反复示范。
SQL_EXAMPLES: Final[tuple[tuple[str, str], ...]] = (
    (
        "我最近的三笔订单是什么状态？",
        "SELECT order_id, status, total_amount, created_at FROM orders "
        "WHERE user_id = :uid ORDER BY created_at DESC LIMIT :n",
    ),
    (
        "订单 1024 什么时候下的？",
        "SELECT order_id, created_at, status FROM orders WHERE user_id = :uid AND order_id = :oid",
    ),
    (
        "我申请过几次退款？",
        "SELECT count(*) AS refund_count FROM refunds WHERE user_id = :uid",
    ),
    (
        "我买过的商品里有哪些类目？",
        "SELECT DISTINCT p.category FROM order_items oi "
        "JOIN orders o ON o.order_id = oi.order_id "
        "JOIN products p ON p.product_id = oi.product_id "
        "WHERE o.user_id = :uid LIMIT :n",
    ),
)


def build_data_messages(
    question: str, *, is_customer: bool, user_id: int, problem: str = "", previous: str = ""
) -> list[dict[str, Any]]:
    """拼 NL→SQL 的 prompt；`problem` 非空时表示这是**修复轮**。

    修复轮与首轮的差别只有两处（追加 assistant 回显 + 一条 user 指明问题），
    这样「首轮 prompt」与「修复 prompt」共享同一份 system 契约，
    few-shot 不会在修复时被丢掉（丢了才是真的「重写」）。
    """
    system = build_system_prompt(
        "你是电商库的 **SQL 生成器**，只输出一条可在 PostgreSQL 上执行的只读 SELECT。\n\n"
        f"可查的表（**只有这些**，其余一律拒绝）：\n{schema_text()}\n\n"
        "硬约束（违反会被程序拒绝并被要求重写）：\n"
        "1. 只允许**单条 SELECT**：不能出现 INSERT/UPDATE/DELETE/DROP，也不能用分号拼第二条语句；\n"
        "2. 必须写**真实**的 WHERE 过滤条件，禁止 `WHERE 1=1` 这类恒真条件；\n"
        "3. 必须带 LIMIT（主键等值查询除外），且用**绑定参数** `:n`，不要写字面量行数；\n"
        "4. 所有用户输入的值一律走绑定参数（`:uid` / `:oid`…），**不要拼进 SQL 文本**；\n"
        "5. 不要引用 users 的 phone/email/address/id_card，也不要引用 stock_movements.operator_id；\n"
        "6. 用不上用户身份时也不要空手 —— 上表的聚合查询同样要带 WHERE。\n\n"
        + (
            f"⚠️ 提问者是**客户**，只能看自己的数据：每条涉及用户数据的查询都**必须**"
            f"带 `user_id = :uid`，绑定值由程序填（当前 user_id={user_id}）。"
            "不要用字面量，也不要相信问题里给出的其它 user_id。\n\n"
            if is_customer
            else "提问者是**内部员工**，可按业务范围查询全站数据（仍受表白名单与 WHERE 约束）。\n\n"
        )
        + "示例（问题 → SQL）：\n"
        + "\n".join(
            f"问题：{q}\nSQL：{sql}\n绑定参数：{_example_params(sql)}" for q, sql in SQL_EXAMPLES
        ),
        output_schema='{"sql": "一条 SELECT 语句", '
        '"params": {"绑定参数名": 值}, "reason": "一句话说明这条 SQL 查什么"}',
    )
    hint = "" if is_customer else "（内部员工视角，可查全站数据）"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"问题：{question}{hint}"},
        *([{"role": "assistant", "content": previous[:1200]}] if previous else []),
        *([{"role": "user", "content": problem}] if problem else []),
    ]


def _example_params(sql: str) -> str:
    """给示例补一句「绑定参数长什么样」——不然模型会照抄 `:uid` 却不知道要绑值。

    参数名直接从 SQL 文本里抽（正则找 `:name`），不维护第二份清单：
    示例改了参数名而这里忘了改，模型看到的示例就是自相矛盾的。
    """
    names = re.findall(r":([a-z_][a-z0-9_]*)", sql)
    seen = list(dict.fromkeys(names))
    return "{" + ", ".join(f"'{name}': <值>" for name in seen) + "}" if seen else "{}"


#: 守卫规则名 → **可照做的修复动作**（`7.agents_module.md` §5 第三条的落点）。
#:
#: 为什么按规则名分支，而不是直接把 `str(error)` 回喂：
#: 守卫的报文是给**运维**看的（带规则名与上下文），对模型来说信息密度很低；
#: 一句「missing_where」不足以让它知道该怎么改。规则名 → 动作的映射表才是有用的回喂。
#:
#: 这张表**必须覆盖 `sql_guard` 的每一条规则**：漏一条就会落到兜底话术
#: （「请按规范重写」），而那正是「笼统说再试一次」的翻版。
#: 覆盖性由 `tests/test_agents_data.py::test_retry_actions_cover_every_guard_rule` 钉住 ——
#: 守卫将来新增规则时那条用例会红，提醒你回来补一行。
RETRY_ACTIONS: Final[dict[str, str]] = {
    "empty_sql": "SQL 是空的：请给出具体查询。",
    "multiple_statements": "出现了多条语句（分号）：只允许一条 SELECT。",
    "not_read_only": "出现了写语句（INSERT/UPDATE/DELETE/DROP…）：这里只允许单条只读 SELECT。",
    "write_in_read": "SELECT 里混进了写操作（SELECT ... INTO / WITH ... AS (DELETE ...)）：去掉写操作。",
    "table_not_allowed": "引用了不可查的表：只能用给定 schema 里的表。",
    "column_not_allowed": "引用了受保护列（PII/操作人）：换成其它列，或改成不带该列的查询。",
    "star_on_protected_table": "对含敏感列的表用了 `SELECT *`：请显式列出需要的列。",
    "forbidden_function": "用了被禁函数（如 pg_sleep/pg_read_file）：去掉它。",
    "parse_error": "SQL 语法解析失败：请写成一条语法正确的 PostgreSQL SELECT。",
    "missing_where": "缺少 WHERE 过滤条件：请补上**真实**的过滤（如 user_id / order_id / 时间范围），"
    "不要用 `WHERE 1=1`。",
    "trivial_where": "WHERE 是恒真条件（如 `1=1`、`'a'='a'`）：必须换成真实过滤列。",
    "missing_limit": "缺少 LIMIT：请加上 `LIMIT :n`（用绑定参数，不要写字面量）。",
    "limit_too_large": "LIMIT 的字面量超过上限：请改成绑定参数 `LIMIT :n`，由程序填值。",
}

#: 表里没有的规则走这句兜底（**不静默**：它出现就说明 `RETRY_ACTIONS` 漏了）
_FALLBACK_ACTION: Final[str] = "请按 PostgreSQL 只读查询的规范重写。"


def _retry_hint(error: SQLSecurityError, *, is_customer: bool) -> str:
    """把守卫的**规则名**翻成一句可照做的修复指示（`7.agents_module.md` §5 第三条）。"""
    rule = getattr(error, "rule", "")
    action = RETRY_ACTIONS.get(rule, _FALLBACK_ACTION)
    if action is _FALLBACK_ACTION:
        logger.warning(
            "守卫规则 %r 没有登记到 RETRY_ACTIONS，回喂给模型的是兜底话术："
            "请补一行规则名 → 修复动作的映射（否则「针对性修复」会退化成「再试一次」）。",
            rule,
        )
    extra = ""
    if is_customer:
        extra = "另外：对客查询**必须**带 `user_id = :uid` 绑定参数。"
    return (
        f"你生成的 SQL 未通过安全校验。\n规则：{rule}\n程序报错：{error}\n"
        f"请针对性修改（不要原样重写）：{action}{extra}"
    )


@dataclass(frozen=True)
class DataPlan:
    """NL→SQL 的结果：**成功/降级两种结局都从返回值表达**，不靠异常（见模块 docstring 第 3 条）。"""

    ok: bool
    sql: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    attempts: int = 0
    reason: str = ""
    errors: tuple[str, ...] = field(default=())
    raw_outputs: tuple[str, ...] = field(default=())

    @property
    def audit_reason(self) -> str:
        return self.reason or "ok"


async def plan_sql(
    question: str,
    *,
    is_customer: bool,
    user_id: int,
    llm: JsonLLM | None = None,
    max_attempts: int = MAX_SQL_ATTEMPTS,
) -> DataPlan:
    """生成 → 校验 →（失败就回喂报错重写）；用尽次数后返回 `ok=False` 的降级结果。

    `max_attempts <= 0` 直接返回降级结果（而不是死循环）：调用方传 0 的意图就是「不要重试」，
    这时它要的是「一次机会」还是「零次机会」由它自己决定 —— 传 0 就是零次。
    """
    if max_attempts < 1:
        return DataPlan(ok=False, attempts=0, reason="重试预算为 0，未生成 SQL")

    problem = ""
    previous = ""
    errors: list[str] = []
    raws: list[str] = []

    for attempt in range(1, max_attempts + 1):
        messages = build_data_messages(
            question, is_customer=is_customer, user_id=user_id, problem=problem, previous=previous
        )
        payload = await generate_json(
            messages, llm=llm, agent="data", temperature=0.0, max_tokens=800
        )
        sql = str(payload.get("sql") or "").strip()
        params = payload.get("params") or {}
        if not isinstance(params, Mapping):
            problem = "params 必须是对象（键是绑定参数名，值是参数值）。"
            errors.append(problem)
            continue
        raws.append(str(payload))
        previous = str(payload)

        # ② 守卫校验（写语句/多语句/白名单/无 WHERE/超上限 全在这里被拦）
        try:
            validate_sql(sql, max_rows=DEFAULT_ROW_LIMIT)
        except SQLSecurityError as exc:
            errors.append(f"[{exc.rule}] {exc}")
            problem = _retry_hint(exc, is_customer=is_customer)
            logger.info(
                "data 第 %d 次生成的 SQL 被守卫拦下（rule=%s），准备回喂报错重写", attempt, exc.rule
            )
            continue

        # ③ 对客行级过滤自检（守卫不知道「你是谁」，见 `customer_row_filter_error`）
        if is_customer:
            filter_error = customer_row_filter_error(sql, user_id=user_id)
            if filter_error:
                errors.append(f"[customer_row_filter] {filter_error}")
                problem = f"{filter_error}\n请针对性修改（不要原样重写）。"
                logger.info("data 第 %d 次生成的 SQL 缺少行级过滤，准备回喂报错重写", attempt)
                continue

        return DataPlan(
            ok=True,
            sql=sql,
            params=dict(params),
            attempts=attempt,
            errors=tuple(errors),
            raw_outputs=tuple(raws),
        )

    reason = (
        f"连续 {max_attempts} 次未能生成合规 SQL，已降级为无法查询。"
        f"最后一次的问题：{problem or '未知'}"
    )
    logger.warning("data 降级：%s（历次错误：%s）", reason, " | ".join(errors))
    return DataPlan(
        ok=False, attempts=max_attempts, reason=reason, errors=tuple(errors), raw_outputs=tuple(raws)
    )


def _bind_customer_id(params: Mapping[str, Any], *, user_id: int, sql: str) -> dict[str, Any]:
    """把行级过滤要用的绑定值**由程序**填进去（不采信模型给的 `uid`）。

    ⚠️ 为什么必须强制覆写：模型完全可能给出 `{"uid": 1}` 而当前用户是 2 ——
    若照它的值绑定，`validate_sql` 与行级过滤自检**都会通过**（形状对），
    但查出来的是别人的数据。身份的来源只能是鉴权结果，不能是被检查的对象自己提供的值。

    只在 SQL 真的用了 `:uid` 时才覆写：别的查询（不含行级过滤的聚合）不强行加参数，
    否则会把「没有该占位符」变成数据库层的绑定错误。
    """
    bound = dict(params)
    if ":uid" in sql:
        bound["uid"] = user_id
    return bound


async def data_node(
    state: GraphState,
    *,
    llm: JsonLLM | None = None,
    row_limit: int = DEFAULT_ROW_LIMIT,
    max_attempts: int = MAX_SQL_ATTEMPTS,
) -> dict[str, Any]:
    """图节点：把问题变成 `rows` + `sql` + `row_meta`；失败则写 `refusals`。

    权限消费方式（`7.agents_module.md` §4 第 4 条）：本节点**不重新判断角色**，
    只读 `state["permissions"]` / `is_customer_path(state)`。对客路径的额外约束
    （强制行级过滤 + 强制覆写 `uid`）都由这一个标记推导出来。

    什么时候会「合理地查不到」而不算故障：
      * 权限不足（缺 `read:own`，例如未登记角色）→ 写 `refusals`，不查库；
      * 检索能力未接上（`ToolNotAvailableError`）→ 写 `refusals`；
      * 重试用尽 → 写 `refusals`（**这就是 A1 在节点级的应用**）。
    数据库的**瞬时**故障（`RetryableToolError`）**不吞**：那是要重投的，
    吞掉等于把可恢复故障伪装成「查不到」，第 12 步的队列再也看不到它。
    """
    user_id = require_user_id(state)
    question = _question_of(state)
    is_customer = is_customer_path(state)

    # 权限自检放在最前：没有读权限就不该花一次 LLM 调用去生成 SQL。
    from app.security.rbac import PERM_READ_OWN, has_permission

    role = str(state.get("role") or "customer")
    if not has_permission(role, PERM_READ_OWN):
        reason = (
            f"角色 {role} 没有 read:own 权限，无法查询业务数据"
            "（未登记角色一律零权限，这是默认拒绝）"
        )
        logger.warning("data 被权限拦住：%s", reason)
        return {"refusals": [reason], "sql": "", "sql_attempts": 0, "rows": [], "row_meta": {}}

    plan = await plan_sql(
        question, is_customer=is_customer, user_id=user_id, llm=llm, max_attempts=max_attempts
    )
    if not plan.ok:
        return {
            "refusals": [plan.reason],
            "sql": "",
            "sql_attempts": plan.attempts,
            "rows": [],
            "row_meta": {"errors": list(plan.errors)},
        }

    bound = _bind_customer_id(plan.params, user_id=user_id, sql=plan.sql)
    try:
        rows, meta = execute_readonly_query(
            plan.sql, bound, max_rows=row_limit, caller=CALLER
        )
    except ToolError as exc:
        # 工具自己的**永久**失败（含业务拒绝与契约错误）→ 降级；可重试的照旧上抛。
        from app.tools.errors import RetryableToolError

        if isinstance(exc, RetryableToolError):
            raise
        reason = f"查询执行失败（不可重试）：{exc}"
        logger.warning("data 执行失败：%s", reason)
        return {
            "refusals": [reason],
            "sql": plan.sql,
            "sql_attempts": plan.attempts,
            "rows": [],
            "row_meta": {},
        }

    logger.info(
        "data 完成：attempts=%d rows=%d truncated=%s audit_id=%s",
        plan.attempts,
        len(rows),
        meta.get("truncated"),
        meta.get("audit_id"),
    )
    # 审计补一条「本轮 NL→SQL 的编排结果」：工具只记了 SQL 执行，
    # 「这条 SQL 是第几次尝试、之前被哪条规则拦过」只有编排层知道（C1/D1 的证据链要它）。
    with audit_tool_call(
        "agent_nl2sql",
        caller=CALLER,
        question=question[:200],
        attempts=plan.attempts,
        errors=list(plan.errors),
    ) as handle:
        handle.rows = len(rows)
        handle.details.update(
            {"sql": plan.sql[:400], "row_count": len(rows), "tables": meta.get("tables", [])}
        )

    return {
        "sql": plan.sql,
        "sql_attempts": plan.attempts,
        "rows": rows,
        "row_meta": meta,
        "refusals": [],
    }


def _question_of(state: GraphState) -> str:
    """取要翻译成 SQL 的问题文本（复用 `last_user_message` 的口径，空则报错）。

    空问题在这里报错而不是降级：那是接线问题（调用方忘了放消息），
    降级成「查不到」会让第 9 步的接口永远返回 200 + 一句免责声明，问题被藏起来。
    """
    text = last_user_message(state).strip()
    if not text:
        raise ValueError(
            "data_node 收到空问题：state['messages'] 里没有 user 消息。"
            "请检查入口是否把用户输入写进了 messages。"
        )
    return text


def allowed_tables() -> Sequence[str]:
    """给测试与文档用的「可查表」清单（= `policy.ALLOWED_TABLES` 的排序结果）。"""
    from app.security import policy

    return sorted(policy.ALLOWED_TABLES)


__all__ = [
    "CALLER",
    "DEFAULT_ROW_LIMIT",
    "HIDDEN_COLUMNS",
    "MAX_SQL_ATTEMPTS",
    "PROMPT_ROWS",
    "RETRY_ACTIONS",
    "SQL_EXAMPLES",
    "DataPlan",
    "allowed_tables",
    "build_data_messages",
    "data_node",
    "plan_sql",
    "schema_text",
]
