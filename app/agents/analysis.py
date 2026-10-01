"""Analysis Agent：**对内数据聚合解读（可选图表）**（`7.agents_module.md` §1、§6 第二批第 6 条）。

定位：`data` 节点回答「有哪些行」，`analysis` 节点回答「这些行说明什么」。
两个节点都读 `state["rows"]`，但**只有 analysis 允许对内**——
它输出的是结论与图表，一旦泄漏给客户，泄漏的不是一行数据而是一句判断。

三段式，每段都有明确的「不做什么」

1. **取数**：复用 `state["rows"]`（图里 data 刚查过就别重查）。
   没有数据就落 `refusals` —— **刻意不自己生成 SQL**（那会出现第二处 SQL 生成逻辑，
   而 SQL 生成只该有一处）。
2. **图表**（可选）：`build_chart_spec` 出**纯函数**的 spec（第 6 步交付），
   数据形状不合适就**不出图**（而不是出一张误导性的图）。
3. **结论**：LLM 把行渲染成结构化结论（`headline` + `bullets` + `caveats`）。
   结论里**必须带数字**（prompt 硬要求），否则「解读」会退化成读后感。

权限：`analysis` 只对内部角色开放
--------------------------------

判据用 router 注入的 `permissions`（**含 `read:all_orders`**），不是「角色名字符串」——
第 4 步已经定好「对内的唯一真相是权限点」，这里再写一遍角色白名单就是第二处真相。
权限不足时本节点**不抛异常也不静默返回空**，而是写 `refusals`：
对客请求落到这里的正确处置是「告诉他这件事需要内部身份」，而不是 500。

图表为什么可以从结论里缺席（而不是报错）
----------------------------------------

`build_chart_spec` 在「列名不存在 / 非数值列 / 饼图配多系列」时抛 `ToolContractError` ——
那些是**调用方写错**。但本节点是**自己**挑列（`_pick_chart_axes` 只看类型与行数），
挑不出来（全是文本列、只有一行）属于**数据形状问题**，不是错误。
这时把 `chart_spec=None` 写回去并记一条 `caveats`，比抛错有用得多。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.agents.agent_llm import (
    JsonLLM,
    build_system_prompt,
    format_rows,
    generate_json,
)
from app.agents.state import GraphState, last_user_message, permissions_of
from app.security.rbac import PERM_READ_ALL_ORDERS, has_permission
from app.tools import build_chart_spec
from app.tools.errors import ToolContractError
from app.tools.sql_executor import ABSOLUTE_MAX_ROWS

logger = logging.getLogger(__name__)

CALLER: Final[str] = "agent:analysis"

#: 参与分析的行的硬上限（E2）。取工具的绝对上限而不是自创一个数：
#: 「一次分析看多少行」与「一次查询能返回多少行」是同一个资源问题，两处各写一个数必然漂移。
MAX_ANALYSIS_ROWS: Final[int] = ABSOLUTE_MAX_ROWS

#: 出图需要的最少数据点数。1 个点画不出分布（只能画一个柱子），
#: 那种图会让读者以为「只有这一项」——宁可不出图。
MIN_CHART_POINTS: Final[int] = 3

#: 送进 prompt 的行数（比 data 宽：聚合结果通常本来就不多，结论要靠它们支撑）
PROMPT_ROWS: Final[int] = 30

ANALYSIS_SYSTEM: Final[str] = build_system_prompt(
    "你是电商企业的**内部数据分析师**，面向内部员工（主管/运营）解读查询结果。\n\n"
    "硬约束：\n"
    "1. 结论必须**只依据给定的数据行**，不许补充外部常识、不许推算没给的数据；\n"
    "2. 每条要点都要**带具体数字或占比**（数据里没有的量词一律不写，如「大量」「明显偏高」）；\n"
    "3. 数据不足以支撑判断时，写进 caveats（「样本仅 3 行，不足以说明趋势」这种），"
    "而不是含糊其辞地给个结论；\n"
    "4. 只输出 JSON，不要解释。",
    output_schema='{"headline": "一句话结论（含关键数字）", '
    '"bullets": ["带数字的要点", "…"], '
    '"caveats": ["需要提醒的口径/样本限制", "…"]}',
)


def _has_internal_scope(state: GraphState) -> bool:
    """是否持有对内数据的读取权限（`read:all_orders`）。"""
    permissions = permissions_of(state)
    if PERM_READ_ALL_ORDERS in permissions:
        return True
    # 兜底：state 里没写 permissions 时按角色现查（与 `permissions_of` 同口径）
    return has_permission(str(state.get("role") or ""), PERM_READ_ALL_ORDERS)


def _numeric(value: Any) -> bool:
    """值能不能当图表里的数值（`Decimal` 也算；`bool` 不算 —— 它画出来是 0/1 的假数据）。"""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    from decimal import Decimal

    return isinstance(value, Decimal)


def _pick_chart_axes(rows: Sequence[Mapping[str, Any]]) -> tuple[str, str] | None:
    """挑一对 (分类列, 数值列) 给图表用；挑不出来返回 None。

    规则刻意简单、可预测：`x` = 第一个**非数值**列（类别/时间），`y` = 第一个数值列。
    为什么不让 LLM 挑列：图表列名写错是本仓最常见的一类失败
    （`build_chart_spec` 的报错信息里专门列了可用列），
    让确定性的代码挑、让模型只负责解读，能把这类失败从「偶发」变成「不发生」。
    """
    if len(rows) < MIN_CHART_POINTS:
        return None
    first = rows[0]
    numeric_columns = [name for name, value in first.items() if _numeric(value)]
    label_columns = [name for name, value in first.items() if not _numeric(value)]
    if not numeric_columns or not label_columns:
        return None
    return label_columns[0], numeric_columns[0]


def build_analysis_messages(
    question: str, rows: Sequence[Mapping[str, Any]], *, chart: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """拼分析 prompt（纯函数，便于断言「数据与问题都进了 prompt」）。"""
    chart_note = ""
    if chart:
        chart_note = (
            f"\n已生成的图表：{chart.get('type')} 图，x={chart.get('x', {}).get('field')}（"
            f"{chart.get('meta', {}).get('points')} 个数据点）。可以引用它。"
        )
    return [
        {"role": "system", "content": ANALYSIS_SYSTEM},
        {
            "role": "user",
            "content": (
                f"内部问题：{question}\n"
                f"查询结果（{len(rows)} 行，制表符风格）：\n{format_rows(rows, limit=PROMPT_ROWS)}"
                f"{chart_note}"
            ),
        },
    ]


def _bullet_list(value: Any, *, limit: int = 8) -> list[str]:
    """把 LLM 给的列表归一成字符串列表（容忍它给成单个字符串/含非字符串项）。"""
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, Sequence):
        items = list(value)
    else:
        return []
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


async def analysis_node(
    state: GraphState,
    *,
    llm: JsonLLM | None = None,
    chart_type: str = "bar",
) -> dict[str, Any]:
    """图节点：读 `rows` → 可选出图 → 出结构化结论。

    输出：`analysis`（`headline` / `bullets` / `caveats` / `row_count` / `chart`）、
    `chart_spec`（数据形状不合适时为 `None`）、必要时 `refusals`
    （缺 `read:all_orders` 时、或 `rows` 为空时）。
    """
    if not _has_internal_scope(state):
        reason = (
            "数据分析（analysis）仅对内部角色开放：当前身份缺少 read:all_orders 权限。"
            "如需查看自己的数据，请直接描述要看的订单/退款字段。"
        )
        logger.warning("analysis 被权限拦住：%s", reason)
        return {"analysis": {}, "chart_spec": None, "refusals": [reason]}

    rows = [row for row in (state.get("rows") or []) if isinstance(row, Mapping)]
    if not rows:
        reason = (
            "没有可分析的数据行：analysis 需要先由 data 节点查出数据"
            "（本节点刻意不自己生成 SQL，避免出现第二处 SQL 生成逻辑）。"
        )
        logger.warning("analysis 无数据可分析：%s", reason)
        return {"analysis": {}, "chart_spec": None, "refusals": [reason]}

    if len(rows) > MAX_ANALYSIS_ROWS:
        logger.warning("分析输入 %d 行超过上限 %d，按上限截断", len(rows), MAX_ANALYSIS_ROWS)
        rows = rows[:MAX_ANALYSIS_ROWS]

    chart_spec: dict[str, Any] | None = None
    caveats: list[str] = []
    axes = _pick_chart_axes(rows)
    if axes is None:
        caveats.append(
            f"数据形状不适合出图（仅 {len(rows)} 行，或缺少可配对的文本/数值列），本次只给结论文本。"
        )
    else:
        x_field, y_field = axes
        try:
            chart_spec = build_chart_spec(
                rows,
                x=x_field,
                y=y_field,
                chart_type=chart_type,
                sort_by=y_field,
                descending=True,
                caller=CALLER,
            )
        except ToolContractError as exc:
            # 自己挑的列仍可能不被图表工具接受（例如该列全是 NULL）。这是**数据形状**问题，
            # 记进 caveats 后继续出结论 —— 让「图出不来」把「结论也丢了」是过度反应。
            logger.info("图表生成被跳过：%s", exc)
            caveats.append(f"图表生成跳过（{exc}）")

    question = last_user_message(state).strip() or "（未提供原始问题）"
    payload = await generate_json(
        build_analysis_messages(question, rows, chart=chart_spec),
        llm=llm,
        agent="analysis",
        temperature=0.2,
        max_tokens=900,
    )

    analysis = {
        "headline": str(payload.get("headline", "")).strip(),
        "bullets": _bullet_list(payload.get("bullets")),
        "caveats": caveats + _bullet_list(payload.get("caveats"), limit=5),
        "row_count": len(rows),
        "chart": chart_spec.get("type") if chart_spec else None,
    }
    logger.info("analysis 完成：rows=%d chart=%s", len(rows), analysis["chart"])
    return {"analysis": analysis, "chart_spec": chart_spec, "refusals": []}


__all__ = [
    "ANALYSIS_SYSTEM",
    "CALLER",
    "MAX_ANALYSIS_ROWS",
    "MIN_CHART_POINTS",
    "PROMPT_ROWS",
    "analysis_node",
    "build_analysis_messages",
]
