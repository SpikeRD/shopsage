"""MCP 工具层（协议侧）：四个 `async` 薄壳 = **线程 + 异常翻译**（`15.mcp.md` 第 4 节第 1 条）。

```
参数形状校验 → 身份边界校验（D1 第二半）→ 调 app/tools/ 的既有函数 → 结果 JSON 化 → 异常翻译
```

**没有第六件事。** 本模块不写 SQL、不拼 prompt、不查库、不决定业务规则 ——
每一条「怎么做」都在第 6 步的工具里（那里有审计、幂等、只读引擎、行数截断）。
这条纪律的理由是 `15.mcp.md` 第 4 节第 1 条的原话（「避免两套实现、两套安全校验」），
落成可执行检查就是 `tests/mcp/test_mcp_contracts.py` 里的源码扫描：
`app/mcp/` 下**不许出现** `INSERT` / `UPDATE` / `DELETE` / 建表语句 / 写会话。

本模块只做两件「协议侧」的事，其余全部委托给 `sync_calls.py`
------------------------------------------------------------

1. **不阻塞事件循环**：工具体是同步阻塞的 I/O（psycopg 查库、本地 embedding 推理）。
   直接在事件循环线程里跑，会让同一进程的其他请求一起卡住（streamable-http 传输下这是真实的）。
   MCP SDK 2.x 对同步函数也会 `to_thread`，但那是 SDK 的实现细节 —— 本层显式做一次，
   行为就不随 SDK 版本漂移。
2. **错误口径可控**：SDK 把「非 MCPError 的异常」一律降级成 `Error executing tool <name>`
   （原始报文只留在服务端）。安全性对，但对**客户端**没有信息量 ——
   而 `6.tools-runbook.md` §2.6 的教训正是「安全异常包一层就丢了 `rule`，
   排障从看规则名退化成翻日志」。所以可预期的失败在这里翻译成带 `[rule]` 的 `ToolError`。

为什么身份边界校验落在 MCP 这一层（而不是工具层）
--------------------------------------------------

工具层**没有**「你是谁」这个概念：`execute_readonly_query(sql, params, caller=...)`
只回答「这条 SQL 合规吗、这条连接能写吗」。行级过滤靠 `requester_user_id` 驱动，
而那个值只有入口知道（内部 Agent 来自第 9 步的会话，MCP 来自服务端配置）。
所以本层的核心动作只有一个：**把服务端声明的身份，变成工具层能执行的形状**：

* `sql_query` → 强制 SQL 带 `WHERE user_id = :user_id`（复用第 4 步的行级过滤口径）；
* `create_ticket` → 强制 `user_id` 等于身份（对客只能建自己的工单）；
* `retrieve_knowledge` / `build_chart` → 无行归属（知识库是所有客户同一份政策文档；
  图表的数据是调用方自己查出来的行）。这两条在 `docs/runbook/15.mcp-runbook.md` §4.2
  有明确的「拦得住 / 拦不住」说明 —— 不写清楚就会被人当成「MCP 已经做了行级过滤」。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from anyio import to_thread
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp import sync_calls as core
from app.mcp.settings import McpSettings
from app.security.rbac import PermissionDenied
from app.security.sql_guard import SQLSecurityError
from app.tools import ToolContractError
from app.tools import ToolError as ToolLayerError
from app.tools.audit import caller_override
from app.tools.errors import ToolNotAvailableError
from app.tools.retriever import DEFAULT_TOP_K

# ── 对外工具名（与 `app/tools/*.py` 的 TOOL_NAME 一致；契约测试遍历真实对象对账）──────
SQL_QUERY: Final[str] = "sql_query"
RETRIEVE_KNOWLEDGE: Final[str] = "retrieve_knowledge"
CREATE_TICKET: Final[str] = "create_ticket"
BUILD_CHART: Final[str] = "build_chart"

#: 可翻译成「客户端能照着改」的异常。**白名单**而不是 `except Exception`：
#: 不在这里的类型必须原样冒泡（SDK 会把它记成 crash，只回一句通用错误），
#: 否则一次 `TypeError` 会被包装成看起来像调用方错误的 `[tool_contract]`，误导排查方向。
_CLIENT_FIXABLE: Final[tuple[type[BaseException], ...]] = (
    SQLSecurityError,
    PermissionDenied,
    ToolLayerError,  # 含 ToolContractError / PermanentToolError / RetryableToolError
)


def as_tool_error(exc: BaseException) -> ToolError:
    """可预期失败 → MCP 的 `ToolError`，消息带**规则名**（客户端据此自我纠正）。

    消息里**不带**库结构、连接串、堆栈：MCP 客户端是不可信输入源，
    错误文本会进它的上下文（甚至可能被它对用户复述）。
    """
    if isinstance(exc, SQLSecurityError):
        return ToolError(f"[{getattr(exc, 'rule', '') or 'unknown'}] {exc}")
    if isinstance(exc, PermissionDenied):
        missing = ", ".join(getattr(exc, "missing", ()) or ())
        role = getattr(exc, "role", "") or ""
        detail = f"（角色 {role}；缺权限 {missing}）" if role or missing else ""
        return ToolError(f"[permission_denied] {exc}{detail}")
    if isinstance(exc, ToolNotAvailableError):
        return ToolError(f"[not_available] {exc}")
    if isinstance(exc, ToolContractError):
        return ToolError(f"[tool_contract] {exc}")
    return ToolError(f"[{type(exc).__name__}] {exc}")


async def _in_thread(fn: Callable[[], Any], *, settings: McpSettings, tool: str) -> Any:
    """在线程里跑同步实现，并做异常翻译（见模块顶部两条理由）。

    三个动作缺一不可：

    1. **盖章**：把 `caller_override` 设成 `mcp:<工具名>`。
       `ContextVar` 是 per-task 的，而 `anyio.to_thread.run_sync` 会把**当前
       context 复制**进工作线程，所以线程里的 `audit_tool_call` 能读到它 ——
       审计里就会出现 `caller="mcp:sql_query"` 而不是工具的默认调用者。
       （这一条的实测证据在 `tests/mcp/test_mcp_audit.py`；
       它同时是「同一进程并发服务多个客户端也不会串 caller」的保证。）
    2. **下线程**：同步阻塞 I/O 不能占着事件循环线程（streamable-http 下这会拖垮所有并发请求）。
    3. **翻译异常**：可预期的失败 → 带 `[rule]` 的 `ToolError`；崩溃原样冒泡。
    """
    token = caller_override.set(settings.caller(tool))
    try:
        return await to_thread.run_sync(fn)
    except ToolError:  # 本模块自己抛的协议错误：原样冒泡
        raise
    except _CLIENT_FIXABLE as exc:
        raise as_tool_error(exc) from exc
    finally:
        caller_override.reset(token)


# ── 四个工具（每一个都只是「线程 + 翻译」，逻辑全在 sync_calls）──────────────────


async def sql_query(
    sql: str,
    params: Mapping[str, Any] | None = None,
    max_rows: int | None = None,
    *,
    settings: McpSettings,
) -> dict[str, Any]:
    """只读查询（`app/tools/sql_executor.py::execute_readonly_query` 的薄壳）。"""
    return await _in_thread(
        lambda: core.sql_query_sync(sql, params, max_rows, settings=settings),
        settings=settings,
        tool=SQL_QUERY,
    )


async def retrieve_knowledge(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    settings: McpSettings,
) -> dict[str, Any]:
    """知识库检索（`app/tools/retriever.py::retrieve_docs` 的薄壳）。"""
    return await _in_thread(
        lambda: core.retrieve_knowledge_sync(query, top_k, settings=settings),
        settings=settings,
        tool=RETRIEVE_KNOWLEDGE,
    )


async def create_ticket(
    ticket_type: str,
    idempotency_key: str,
    user_id: int | None = None,
    assignee_id: int | None = None,
    *,
    settings: McpSettings,
) -> dict[str, Any]:
    """建工单（`app/tools/ticket.py::create_ticket` 的薄壳，写路径的唯一出口）。"""
    return await _in_thread(
        lambda: core.create_ticket_sync(
            ticket_type, idempotency_key, user_id, assignee_id, settings=settings
        ),
        settings=settings,
        tool=CREATE_TICKET,
    )


async def build_chart(
    rows: Sequence[Mapping[str, Any]],
    x: str,
    y: str | None = None,
    series: Sequence[str] | None = None,
    chart_type: str = "bar",
    title: str | None = None,
    sort_by: str | None = None,
    descending: bool = False,
    max_points: int | None = None,
    *,
    settings: McpSettings,
) -> dict[str, Any]:
    """图表 spec（`app/tools/chart.py::build_chart_spec` 的薄壳）。"""
    return await _in_thread(
        lambda: core.build_chart_sync(
            rows, x, y, series, chart_type, title, sort_by, descending, max_points, settings=settings
        ),
        settings=settings,
        tool=BUILD_CHART,
    )


async def dispatch(
    tool: str, arguments: Mapping[str, Any] | None, *, settings: McpSettings
) -> dict[str, Any]:
    """按名字分发（测试与演示用；协议路径由 `server.py` 注册的函数走）。"""
    return await _in_thread(
        lambda: core.dispatch_sync(tool, arguments, settings=settings), settings=settings, tool=tool
    )


__all__ = [
    "BUILD_CHART",
    "CREATE_TICKET",
    "RETRIEVE_KNOWLEDGE",
    "SQL_QUERY",
    "as_tool_error",
    "build_chart",
    "create_ticket",
    "dispatch",
    "retrieve_knowledge",
    "sql_query",
]
