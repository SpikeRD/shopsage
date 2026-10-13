"""MCP Server 装配：把四个薄壳工具注册到 `MCPServer` 上（`15.mcp.md` 第 6 节第 1、2 步）。

关于「FastMCP」这个名字
------------------------

`15.mcp.md` 第 3、6 节写的是 `FastMCP`。本仓装的是 **MCP Python SDK 2.x**
（`mcp==2.3.0`），它把 v1 的 `mcp.server.fastmcp.FastMCP` **改名为**
`mcp.server.mcpserver.MCPServer`：

```
>>> from mcp.server.fastmcp import FastMCP
ModuleNotFoundError: No module named 'mcp.server.fastmcp';
This is mcp 2.x, where FastMCP was renamed to MCPServer
```

三种处置（`mcp<2` 钉版本 / 装独立的 `fastmcp` 包 / 用 2.x 的 `MCPServer`）的取舍、
以及为什么选第三种，写在 `docs/runbook/15.mcp-runbook.md` §3.1 —— 结论一句话：
**能力与协议完全一致（`@server.tool` 装饰器、docstring 自动成为工具描述、stdio/SSE/HTTP
三种传输都在），而 2.x 多给了一个测试用的进程内 `Client`**，第 7 节的冒烟用例直接受益。

装配里唯一「有判断」的地方是注解（annotations）
----------------------------------------------

MCP 的客户端（Claude/Cursor）会拿 `ToolAnnotations` 决定**要不要弹权限确认框**：
`read_only_hint=True` 的只读工具通常可以自动放行，`destructive_hint` 的要人工点一下。
这是一条真实存在的、**由服务端声明、客户端执行**的安全机制 —— 所以它必须诚实：

| 工具 | read_only | destructive | idempotent | 依据 |
|---|---|---|---|---|
| `sql_query` | ✅ | ❌ | ❌（查询无副作用） | 只读引擎 + 静态守卫 |
| `retrieve_knowledge` | ✅ | ❌ | ❌ | 纯读 |
| `build_chart` | ✅ | ❌ | ❌ | 纯函数（不碰库） |
| `create_ticket` | ❌ | ❌ | **✅** | 会写库；幂等由 DB 唯一约束保证 |

⚠️ `create_ticket` 的 `destructive_hint` 刻意**不设 True**：它建的是新工单，
不修改/删除既有数据（工单的后续流转是第 9 步的审批通道，不在 MCP 暴露范围内）。
把「新建」标成 destructive 会让客户端对每一次正常建单都弹确认框，最后的结果是
使用者习惯性点「允许」—— 那比不标更危险（`4.security.md` 的告警疲劳同一条道理）。
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Final, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from app.mcp import tools as tool_impl
from app.mcp.settings import (
    EXPOSED_TOOLS,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    SERVER_TITLE,
    SERVER_VERSION,
    TOOL_DESCRIPTIONS,
    McpSettings,
    get_settings,
)

#: 传输方式（`MCPServer.run` 的白名单）。默认 stdio：MCP 客户端配置里最常见、
#: 也最少暴露面（进程由客户端自己拉起，不监听端口）。
Transport = Literal["stdio", "sse", "streamable-http"]


def build_server(settings: McpSettings | None = None) -> MCPServer:
    """装配一个 MCP server（**纯构造，不启动、不连库**）。

    这是全模块唯一把「工具」和「协议」缝在一起的地方。三个刻意的做法：

    1. **每个工具一个闭包**，把 `settings` 捕获进去（不是模块级全局）：
       同一个进程里造两个不同身份的 server（对客 + 内部）互不干扰，
       测试与多租户部署都靠这一点。
    2. **工具名与描述显式传入**（`name=` / `description=`），不依赖函数名与 docstring：
       名字是**对外契约**（`15.mcp.md` 第 2 节那张表），而函数名是可以被重命名重构的。
       描述取自 `settings.TOOL_DESCRIPTIONS` 这张表，于是契约测试能逐个断言。
    3. **docstring 仍然写全**（这些函数本体在 `tools.py`，那里是给人看的详细说明）。
    """
    resolved = settings if settings is not None else get_settings()
    problems = resolved.validate()
    if problems:  # 启动期 fail-fast：身份不明就不该起来（见 settings.validate 的说明）
        raise ValueError("MCP 配置不可用：" + "；".join(problems))

    server = MCPServer(
        name=SERVER_NAME,
        title=SERVER_TITLE,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
    )

    async def _sql_query(
        sql: str,
        params: dict[str, Any] | None = None,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        """在只读数据库上执行一条单语句 SELECT。

        Args:
            sql: 单条 SELECT。表/列必须在白名单内、必须带 WHERE、必须用 :user_id 命名参数
                做行级过滤（对客策略下）。
            params: 命名参数绑定（如 {"user_id": 1, "n": 50}）。
            max_rows: 期望行数上限（只能比服务端配置更小）。
        """
        return await tool_impl.sql_query(sql, params, max_rows, settings=resolved)

    async def _retrieve_knowledge(query: str, top_k: int = 5) -> dict[str, Any]:
        """检索知识库，返回带 source 溯源的片段（无溯源的一律丢弃）。

        Args:
            query: 用户问题原文。
            top_k: 期望条数（默认 5，服务端上限见 MCP_MAX_TOP_K）。
        """
        return await tool_impl.retrieve_knowledge(query, top_k, settings=resolved)

    async def _create_ticket(
        ticket_type: str,
        idempotency_key: str,
        user_id: int | None = None,
        assignee_id: int | None = None,
    ) -> dict[str, Any]:
        """为客户建一张工单（幂等；对客策略下只能建自己的）。

        Args:
            ticket_type: 工单类型，如 refund / logistics / other（枚举见 db.models.TICKET_TYPES）。
            idempotency_key: 幂等键（≤64 字符、无空白），由调用方提供。
            user_id: 工单归属客户（对客策略下省略即用服务端身份；内部策略下必填）。
            assignee_id: 预分配客服（可空）。
        """
        return await tool_impl.create_ticket(
            ticket_type, idempotency_key, user_id, assignee_id, settings=resolved
        )

    async def _build_chart(
        rows: list[dict[str, Any]],
        x: str,
        y: str | None = None,
        series: list[str] | None = None,
        chart_type: str = "bar",
        title: str | None = None,
        sort_by: str | None = None,
        descending: bool = False,
        max_points: int | None = None,
    ) -> dict[str, Any]:
        """把查询结果组装成结构化图表 spec（不返回图片）。

        Args:
            rows: 行字典列表（直接把 sql_query 的 rows 传进来）。
            x: X 轴/分类列名。
            y: 单系列时的数值列名（与 series 二选一）。
            series: 多系列时的数值列名列表。
            chart_type: bar / line / pie / scatter / table。
            title: 图表标题。
            sort_by: 按 'x' 或某个系列列排序；descending 控制方向。
            max_points: 期望点数上限（只能比服务端配置更小）。
        """
        return await tool_impl.build_chart(
            rows,
            x,
            y,
            series,
            chart_type,
            title,
            sort_by,
            descending,
            max_points,
            settings=resolved,
        )

    # ── 注册（名字与描述来自 settings 里的常量表；注解是对客户端的诚实声明）────────
    server.add_tool(
        _sql_query,
        name=EXPOSED_TOOLS[0],
        description=TOOL_DESCRIPTIONS[EXPOSED_TOOLS[0]],
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        _retrieve_knowledge,
        name=EXPOSED_TOOLS[1],
        description=TOOL_DESCRIPTIONS[EXPOSED_TOOLS[1]],
        annotations=ToolAnnotations(read_only_hint=True),
    )
    server.add_tool(
        _create_ticket,
        name=EXPOSED_TOOLS[2],
        description=TOOL_DESCRIPTIONS[EXPOSED_TOOLS[2]],
        annotations=ToolAnnotations(idempotent_hint=True),
    )
    server.add_tool(
        _build_chart,
        name=EXPOSED_TOOLS[3],
        description=TOOL_DESCRIPTIONS[EXPOSED_TOOLS[3]],
        annotations=ToolAnnotations(read_only_hint=True),
    )
    return server


def describe_tools(settings: McpSettings | None = None) -> list[dict[str, Any]]:
    """把「对外暴露了什么」整理成可打印/可断言的结构（运维与契约测试共用）。

    ⚠️ 它列出的是**注册事实**（直接问 server），而不是 `EXPOSED_TOOLS` 常量 ——
    否则「常量写了四个、实际注册了三个」这种事故看不出来。常量只用来对账。
    """
    resolved = settings if settings is not None else get_settings()
    server = build_server(resolved)
    out: list[dict[str, Any]] = []
    for tool in server._tool_manager.list_tools():  # noqa: SLF001 - 只读，用于自检与运维
        out.append(
            {
                "name": tool.name,
                "description": tool.description,
                "read_only": bool(getattr(tool.annotations, "read_only_hint", False)),
                "idempotent": bool(getattr(tool.annotations, "idempotent_hint", False)),
                "parameters": sorted((tool.parameters or {}).get("properties", {}).keys()),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    """`python -m app.mcp` 的入口。

    退出码约定（与第 14 步评测同一套口径，CI 与人工都靠它）：

    | 码 | 含义 |
    |---|---|
    | 0 | 正常（`--check` 通过 / 服务正常退出） |
    | 2 | 配置或参数不可用（**启动之前**就失败，不是跑到一半断） |
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.mcp",
        description="ShopSage MCP server（默认 stdio 传输）",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="传输方式；stdio 给本地客户端（Cursor/Claude 桌面端），HTTP 类给共享服务",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做自检（配置 + 工具清单）然后退出，不启动服务",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="自检结果以 JSON 输出（给脚本/CI 消费；默认给人看）",
    )
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except ValueError as exc:
        print(f"[MCP 无法启动] {exc}")
        return 2

    if args.check:
        listing = describe_tools(settings)
        if args.json:
            print(
                json.dumps(
                    {
                        "server": SERVER_NAME,
                        "version": SERVER_VERSION,
                        "policy": settings.policy,
                        "actor_user_id": settings.actor_user_id,
                        "actor_role": settings.actor_role,
                        "tools": listing,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"{SERVER_TITLE}（{SERVER_NAME} {SERVER_VERSION}）")
            print(
                f"策略：{settings.policy} | 身份：user_id={settings.actor_user_id} "
                f"role={settings.actor_role} | 上限：top_k≤{settings.max_top_k} "
                f"rows≤{settings.max_rows} points≤{settings.max_chart_points}"
            )
            for item in listing:
                flags = "只读" if item["read_only"] else "写"
                if item["idempotent"]:
                    flags += " 幂等"
                print(f"  - {item['name']:<20} [{flags}] 参数：{', '.join(item['parameters'])}")
        return 0

    server = build_server(settings)
    # ⚠️ stdio 传输下 **stdout 是协议通道**：任何调试打印都会污染 JSON-RPC 流
    # （症状是客户端报「无法解析服务端消息」）。所以日志一律走 stderr。
    import sys

    print(
        f"[mcp] 启动 {SERVER_NAME} {SERVER_VERSION} | transport={args.transport} | "
        f"policy={settings.policy} | actor_user_id={settings.actor_user_id}",
        file=sys.stderr,
        flush=True,
    )
    server.run(args.transport)
    return 0


__all__ = ["Transport", "build_server", "describe_tools", "main"]


if __name__ == "__main__":  # pragma: no cover - 让 `python -m app.mcp.server` 与 `-m app.mcp` 等价
    # ⚠️ 这四行不是可有可无的装饰（本步实测撞到）：`python -m <module>` 把该模块当 `__main__`
    # 执行，**不会**触发 `if __name__ == "__main__"` 之外的东西 —— 没有这个 guard，
    # `-m app.mcp.server` 会「什么都不做、静默退出 0」，而手册 §2.3 声称两条命令行为一致。
    # 一个静默成功的命令比一个报错的命令更危险：它会被写进运维脚本。
    raise SystemExit(main())
