"""MCP 的**同步侧**公共件：一致的结果形状 + 身份边界校验。

为什么单独一个模块（而不是塞进 `tools.py`）
--------------------------------------------

`tools.py` 里的四个函数是 `async`（MCP 工具必须是协程，见 `server.py` 的说明），
但「一件事」有两类调用方：

* MCP 协议路径 → 走 `tools.py` 的 async 薄壳（线程 + 异常翻译）；
* **运维脚本 / 演示 / 别的 Python 调用方**（`scripts/mcp_demo.py`）→ 不想为一个自检
  引入一个事件循环，也不该拿到「另一种结果形状」。

两者共用同一份同步实现，结果形状与边界校验就只有一处真相。这条做法与第 6 步
`app/tools/write_path.py`（把两个写工具共用的零件抽出来）是同一个理由：
**要错也只错一处**。

⚠️ 本模块的每个函数都必须传 `settings`（身份）—— 它存在的意义就是「把服务端声明的身份
变成工具层能执行的形状」。没有默认值，就没有「忘了传身份」这种调用形态。

⚠️ 调用工具层时一律传 `caller=""`：真正的 caller 由入口用
`app/tools/audit.caller_override` 盖章（见 `app/tools/audit.py` 的说明）。
这样审计里是 `mcp:<工具名>`（入口可归属），而且**只有一条**记录。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.settings import EXPOSED_TOOLS, McpSettings
from app.security.policy import ROW_FILTER_COLUMN
from app.security.sql_guard import validate_sql
from app.tools import (
    build_chart_spec,
    create_ticket as create_ticket_tool,
    execute_readonly_query,
    retrieve_docs,
)
from app.tools.audit import caller_override
from app.tools.retriever import MAX_TOP_K

#: 行级过滤的绑定参数名（`WHERE user_id = :user_id` 里的那个名字）
FILTER_PARAM: Final[str] = ROW_FILTER_COLUMN


class caller_stamp:  # noqa: N801 - 当上下文管理器用，小写更自然
    """给本次调用盖一个 `caller`（`mcp:<工具名>`），退出时还原。

    为什么同步侧也要盖（实测撞到）：本模块的同步函数既被 `tools.py` 的 async 薄壳调用
    （那里已经设了 contextvar），也被**运维脚本/演示**直接调用（`scripts/mcp_demo.py`）。
    不在这里设，脚本那条路就会撞上「工具调用必须显式传 caller」而失败 ——
    而失败信息会把「脚本忘盖章」误导成「后端不可用」（第一版实测就是这个症状）。
    盖上之后，两条路走的是同一套审计语义：**入口盖章、工具内部复用**。

    嵌套安全：内层 `set` 会覆盖外层（值相同），退出时按 token 还原，不会把外层弄丢。
    """

    __slots__ = ("_token",)

    def __init__(self, settings: McpSettings, tool: str) -> None:
        self._token = caller_override.set(settings.caller(tool))

    def __enter__(self) -> "caller_stamp":
        return self

    def __exit__(self, *exc_info: object) -> None:
        caller_override.reset(self._token)

#: 行级过滤的等值条件：同一处正则同时服务「有没有过滤」与「过滤是不是绑定的」两个判断。
#: 用 AST 判它更严谨（`sql_guard` 就是这么做的），但这里只回答一个更窄的问题
#: ——「文本里到底有没有 `user_id = :user_id` 这个绑定形态」，所以简单可读优先。
_FILTER_RE: Final[re.Pattern[str]] = re.compile(
    rf"\b{re.escape(ROW_FILTER_COLUMN)}\b\s*=\s*:{re.escape(FILTER_PARAM)}\b",
    re.IGNORECASE,
)


def jsonable(value: Any) -> Any:
    """把工具结果收敛成 JSON 原生类型（MCP 是跨进程协议，出口只能是 JSON）。

    为什么必须过这一道：第 6 步的工具**故意**保留驱动原样 ——
    `execute_readonly_query` 的 `rows[0]` 里是 `Decimal('2745.98')` 与 `datetime`，
    理由写在它的 docstring 里（「金额要参与比较，顺手 float()/str() 会让下游拿到
    看起来一样、算起来不一样的数据」）。工具层不做 JSON 化是对的，**出口必须做**。

    两条转换口径（与 `app/tools/chart.py::_jsonable` 同源但**不同**，这是有意的）：

    * `Decimal` → **字符串**（`Decimal('141.00')` → `"141.00"`）：图表那边转 float 是因为
      它只要比例关系；协议出口转字符串是因为**精度不能在这条边界上丢**。
    * `datetime` → ISO-8601 字符串；其余未知类型 → `str()`（协议出口不该因为一个怪类型整体失败）。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return str(value)


def dumps(payload: Any) -> str:
    """稳定序列化（缩进 2、保留中文）—— 运维脚本与文档里的实测输出都出自这里。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ── 参数上限：只允许「变严」────────────────────────────────────────────────────


def positive_int(value: Any, *, field: str) -> int:
    """正整数校验（`bool` 是 `int` 的子类，必须显式排除：`top_k=True` 会被当成 1）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolError(f"[tool_contract] {field} 必须是正整数，收到 {value!r}")
    return value


def capped(value: int | None, *, cap: int, subject: str) -> int:
    """把调用方给的上限收敛到服务端上限（**只允许变严**）。

    理由与 `sql_executor._resolve_rows_cap` 完全一致：上限是安全参数，而 MCP 的调用方
    是一个可能被提示注入影响的模型。「允许客户端放宽」等于把 C1 的上限变成建议值。
    要放宽请改服务端配置（`MCP_MAX_ROWS` / `MCP_MAX_TOP_K` / `MCP_MAX_CHART_POINTS`）——
    那里有运维可见的 diff 与可 review 的理由。
    """
    if value is None:
        return cap
    resolved = min(positive_int(value, field=subject), cap)
    if resolved != value:
        import logging

        logging.getLogger("app.mcp").info(
            "mcp 客户端请求 %s=%s 超过服务端上限 %s，已收敛为 %s", subject, value, cap, resolved
        )
    return resolved


# ── 身份边界（D1 的第二半）────────────────────────────────────────────────────


def assert_customer_row_filter(sql: str) -> None:
    """对客策略下：SQL 必须带 `WHERE user_id = :user_id` 形式的行级过滤。

    拦三件事（每一件都对应一个真实的越权形态）：

    | 形态 | 后果 | 这里的处置 |
    |---|---|---|
    | 完全没有 `user_id` 过滤 | 一条 `SELECT ... FROM orders` 拉走全站订单 | 拒绝（`mcp_missing_row_filter`） |
    | 过滤写成字面量 `user_id = 1` | 值写在 SQL 文本里，没有可校验的对象 | 拒绝（`mcp_row_filter_needs_binding`） |
    | 过滤写成 `user_id = :other` | 换个参数名绕开「绑定值必须等于身份」的检查 | 拒绝（同一条规则） |

    规则落在**参数名**上、值在同处校验（`check_params_match_identity`）——
    两者叠起来才等于「这条连接只能看这一行的数据」。
    """
    if _FILTER_RE.search(sql):
        return
    if FILTER_PARAM not in sql.lower():
        raise ToolError(
            f"[mcp_missing_row_filter] 对客策略（MCP_POLICY=customer）下，SQL 必须带行级过滤 "
            f"`WHERE {ROW_FILTER_COLUMN} = :{FILTER_PARAM}`。"
            "这不是建议而是本入口的硬要求：MCP 客户端是不可信输入源，"
            "一旦放行无过滤的查询，第 4 步的行级过滤（D1 的第二半）在这个入口就整个失效了。"
            "若这个服务确实需要跨用户查询，请让运维显式改成 MCP_POLICY=internal（见 runbook §4.3）。"
        )
    raise ToolError(
        f"[mcp_row_filter_needs_binding] 行级过滤必须用命名参数绑定"
        f"（`{ROW_FILTER_COLUMN} = :{FILTER_PARAM}`），不能把值写进 SQL 文本。"
        f"收到：{sql[:120]!r}。理由有两层：① 字面量过滤只能靠字符串匹配判断，"
        "注释/大小写/等价改写都能绕过；② 绑定参数的值会与本服务声明的身份做等值校验，"
        "字面量则没有可校验的对象。"
    )


def check_params_match_identity(
    params: Mapping[str, Any] | None, settings: McpSettings
) -> dict[str, Any]:
    """绑定参数里的 `user_id` 必须**等于本服务的身份**（身份不是客户端申报的）。"""
    bound = dict(params or {})
    if FILTER_PARAM not in bound:
        raise ToolError(
            f"[mcp_missing_row_filter] 缺少绑定参数 `{FILTER_PARAM}`："
            "SQL 里用了过滤参数，但参数表里没有它（查询会因为缺绑定值而失败）。"
        )
    expected = settings.actor_user_id
    if expected is None:  # pragma: no cover - McpSettings.validate() 已保证
        raise ToolError("[mcp_config] 对客策略下缺少身份配置（MCP_ACTOR_USER_ID）")
    raw = bound[FILTER_PARAM]
    # `bool` 是 `int` 的子类，`int(True) == 1` 会静默放行 —— 与 `positive_int` 同一条口径
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolError(
            f"[mcp_invalid_row_filter] 绑定参数 {FILTER_PARAM} 必须是整数（不接受浮点/字符串/布尔），"
            f"收到 {raw!r}"
        )
    if raw != int(expected):
        raise ToolError(
            f"[mcp_row_filter_mismatch] 越权拦截：本 MCP 服务声明的身份是 user_id={expected}，"
            f"但绑定参数 {FILTER_PARAM}={raw} 指向另一个用户的数据。"
            "身份来自服务端配置（MCP_ACTOR_USER_ID），不接受客户端申报。"
        )
    return bound


def resolve_ticket_target(user_id: int | None, settings: McpSettings) -> int:
    """工单的归属用户（对客只能是自己；内部必须显式给）。"""
    actor = settings.actor_user_id
    if settings.is_customer_scoped:
        if actor is None:  # pragma: no cover - validate() 已保证
            raise ToolError("[mcp_config] 对客策略下缺少身份配置（MCP_ACTOR_USER_ID）")
        if user_id is not None and int(user_id) != int(actor):
            raise ToolError(
                f"[mcp_row_filter_mismatch] 越权拦截：本服务身份 user_id={actor}，"
                f"但请求为 user_id={user_id} 建工单。对客入口只能建自己的工单。"
            )
        return int(actor)
    if user_id is None:
        raise ToolError(
            "[tool_contract] 内部策略下必须显式给 user_id：工单要挂在真实客户上，"
            "不存在「默认客户」这种东西。"
        )
    return positive_int(user_id, field="user_id")


# ── 四个工具的同步实现（结果形状的唯一真相）────────────────────────────────────


def sql_query_sync(
    sql: str,
    params: Mapping[str, Any] | None,
    max_rows: int | None,
    *,
    settings: McpSettings,
) -> dict[str, Any]:
    """只读查询 → `{"rows": [...], "meta": {...}}`。"""
    if not isinstance(sql, str) or not sql.strip():
        raise ToolError("[tool_contract] sql 不能为空（必须是单条 SELECT 文本）")
    cap = capped(max_rows, cap=settings.max_rows, subject="max_rows")

    # ⚠️ **校验顺序是有意的：先守卫，再行级过滤。** 反过来会让报错指错方向 ——
    # 实测踩到（runbook §10.1 坑 1）：`DELETE FROM orders` 会先撞上「缺行级过滤」，
    # 于是客户端以为「这条 SQL 加个 WHERE 就行」，而它真正的问题是**写语句**（`not_read_only`）。
    # 规则名是给模型自我纠正用的，必须先答「这条 SQL 是什么形态」，
    # 再答「它能不能通过本入口的身份口径」。
    # 这一步只做静态校验、**不执行**（执行在工具内部又来一次，那是第 6 步的纵深防御）。
    validate_sql(sql, max_rows=cap)

    if settings.is_customer_scoped:
        assert_customer_row_filter(sql)
        bound = check_params_match_identity(params, settings)
    else:
        bound = dict(params or {})

    with caller_stamp(settings, "sql_query"):
        rows, meta = execute_readonly_query(sql, bound, max_rows=cap, caller=None)
    # 只回传元数据里**有诊断价值**的键：`columns` 会透露表结构（守卫已按白名单放过，
    # 但没必要在协议里再送一份），`declared_limit` / `limit_clamped` 属内部口径。
    return {
        "rows": jsonable(rows),
        "meta": {
            "row_count": meta["row_count"],
            "truncated": meta["truncated"],
            "rows_cap": meta["rows_cap"],
            "tables": meta["tables"],
            "duration_ms": meta["duration_ms"],
            "audit_id": meta["audit_id"],
        },
    }


def retrieve_knowledge_sync(
    query: str, top_k: int, *, settings: McpSettings
) -> dict[str, Any]:
    """知识库检索 → `{"results": [...], "meta": {...}}`（只含带溯源的片段）。"""
    if not isinstance(query, str) or not query.strip():
        raise ToolError("[tool_contract] query 不能为空（检索必须带问题文本）")
    # 双重夹取：这里夹到本服务上限，工具层再夹到 MAX_TOP_K(20)。两层都是「只能变严」。
    cap = capped(top_k, cap=min(settings.max_top_k, MAX_TOP_K), subject="top_k")
    with caller_stamp(settings, "retrieve_knowledge"):
        docs = retrieve_docs(query, cap, caller=None)
    return {
        "results": [
            {
                "text": doc.text,
                "source": doc.source,
                "citation": doc.citation(),
                "chunk_id": doc.chunk_id,
                "page": doc.page,
                "score": doc.score,
            }
            for doc in docs
        ],
        "meta": {
            "returned": len(docs),
            "top_k": cap,
            # 后端名写死在结果里是有意的：第 10 步换检索实现时，下游能看出「这份结果来自谁」
            "backend": "app.tools.retriever.retrieve_docs",
        },
    }


def create_ticket_sync(
    ticket_type: str,
    idempotency_key: str,
    user_id: int | None,
    assignee_id: int | None,
    *,
    settings: McpSettings,
) -> dict[str, Any]:
    """建工单 → `{"ticket_id", "user_id", "type", "status", ...}`（幂等）。"""
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ToolError(
            "[tool_contract] idempotency_key 不能为空，且必须由调用方提供："
            "幂等的语义是「同一个键的重复调用返回同一个结果」，键若由服务端生成，"
            "每次调用都会是一条新工单（幂等形同虚设）。"
        )
    target = resolve_ticket_target(user_id, settings)
    with caller_stamp(settings, "create_ticket"):
        ref = create_ticket_tool(
            target,
            ticket_type,
            idempotency_key=idempotency_key,
            caller=None,
            # 角色也来自服务端配置：客户端无法用一个参数把自己提权成 customer_service
            role=settings.actor_role,
            assignee_id=assignee_id,
        )
    return {
        "ticket_id": ref.ticket_id,
        "user_id": ref.user_id,
        "type": ref.type,
        "status": ref.status,
        # ⚠️ 返回结构里**故意没有** `created`：工具层的返回值也不带它（幂等的类型保证，
        # 见 `app/tools/ticket.py` 的 docstring）。「本次是新建还是命中」在审计字段
        # `details.created` 里（`docs/runbook/15.mcp-runbook.md` §5.3 有实测）。
        "note": "同一 (user_id, idempotency_key) 的重复调用返回同一张工单；是否本次新建见审计 details.created",
    }


def build_chart_sync(
    rows: Sequence[Mapping[str, Any]],
    x: str,
    y: str | None,
    series: Sequence[str] | None,
    chart_type: str,
    title: str | None,
    sort_by: str | None,
    descending: bool,
    max_points: int | None,
    *,
    settings: McpSettings,
) -> dict[str, Any]:
    """图表 spec → `shopsage.chart/v1`（纯计算，不碰库）。"""
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise ToolError(
            "[tool_contract] rows 必须是行字典的列表（直接把 sql_query 返回的 rows 传进来即可）"
        )
    cap = capped(max_points, cap=settings.max_chart_points, subject="max_points")
    with caller_stamp(settings, "build_chart"):
        spec = build_chart_spec(
            rows,
            x=x,
            y=y,
            series=list(series) if series is not None else None,
            chart_type=chart_type,
            title=title,
            max_points=cap,
            sort_by=sort_by,
            descending=descending,
            caller=None,
        )
    return jsonable(spec)


def dispatch_sync(
    tool: str, arguments: Mapping[str, Any] | None, *, settings: McpSettings
) -> dict[str, Any]:
    """按名字分发（运维脚本用；协议路径由 `server.py` 注册的函数走）。"""
    if tool not in EXPOSED_TOOLS:
        raise ToolError(
            f"[unknown_tool] 未暴露的工具：{tool!r}。对外只暴露 {', '.join(EXPOSED_TOOLS)}"
            "（15.mcp.md 第 4 节第 3 条：内部敏感能力不对外）。"
        )
    payload = dict(arguments or {})
    if tool == "sql_query":
        return sql_query_sync(
            payload.get("sql", ""),
            payload.get("params"),
            payload.get("max_rows"),
            settings=settings,
        )
    if tool == "retrieve_knowledge":
        return retrieve_knowledge_sync(
            payload.get("query", ""), payload.get("top_k", 5), settings=settings
        )
    if tool == "create_ticket":
        return create_ticket_sync(
            payload.get("ticket_type", ""),
            payload.get("idempotency_key", ""),
            payload.get("user_id"),
            payload.get("assignee_id"),
            settings=settings,
        )
    return build_chart_sync(
        payload.get("rows", []),
        payload.get("x", ""),
        payload.get("y"),
        payload.get("series"),
        payload.get("chart_type", "bar"),
        payload.get("title"),
        payload.get("sort_by"),
        bool(payload.get("descending", False)),
        payload.get("max_points"),
        settings=settings,
    )


__all__ = [
    "FILTER_PARAM",
    "assert_customer_row_filter",
    "build_chart_sync",
    "caller_stamp",
    "capped",
    "check_params_match_identity",
    "create_ticket_sync",
    "dispatch_sync",
    "dumps",
    "jsonable",
    "positive_int",
    "resolve_ticket_target",
    "retrieve_knowledge_sync",
    "sql_query_sync",
]
