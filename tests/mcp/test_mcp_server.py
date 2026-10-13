"""MCP Server 的发现与协议用例（`15.mcp.md` §7 的 `test_mcp_discovery`）。

「能被外部客户端发现」是一句需要**证据**的话，它至少包含四件事：

1. `initialize` 握手成功，服务端自报的名字/版本与配置一致；
2. `tools/list` 能列出工具，且**描述非空**（客户端模型靠描述决定怎么调）；
3. 注解诚实（只读 / 幂等）—— 客户端靠它决定要不要弹权限确认框；
4. 输入 schema 形状正确（必填项、类型、默认值），否则客户端会拼出不合法的调用。

这四条都走**真的协议**（进程内 `mcp.Client`），而不是直接读内部对象：
读内部对象只能证明"我们以为注册了"，走协议才能证明"客户端真的能看到"。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client

from app.mcp.server import build_server, describe_tools
from app.mcp.settings import EXPOSED_TOOLS, McpSettings, SERVER_NAME, SERVER_VERSION
from tests.mcp.conftest import tool_text

#: 仓库根（CLI 必须在根目录跑：`uv run` 不改变工作目录，`app` 包从根解析）
REPO_ROOT = Path(__file__).resolve().parents[2]


async def test_client_discovers_the_four_tools(customer_server):
    """客户端能发现四个工具，且每个都带非空描述与合法输入 schema。"""
    async with Client(customer_server) as client:
        listed = await client.list_tools()
        tools = listed.tools

    names = [tool.name for tool in tools]
    assert names == list(EXPOSED_TOOLS), f"发现到的工具与对外清单不一致：{names}"

    for tool in tools:
        assert tool.description and len(tool.description) > 20, f"{tool.name} 的描述太短，客户端无法判断用途"
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert schema.get("properties"), f"{tool.name} 没有参数（schema 形状不对）"
        # 参数名不许泄露身份口径（D1）：见 test_mcp_contracts.py
        assert "role" not in schema["properties"]


async def test_initialize_reports_the_configured_server_identity(customer_server):
    """握手时服务端自报家门（客户端据此展示"连上了谁"）。"""
    async with Client(customer_server) as client:
        info = client.server_info
        assert info.name == SERVER_NAME
        assert info.version == SERVER_VERSION
        # instructions 是「边界的说明书」：没有它，客户端会以为这是一个通用 SQL 服务
        assert client.instructions and "create_ticket" in client.instructions


async def test_annotations_are_honest(customer_server):
    """注解是对客户端的承诺：只读/幂等标注错了，客户端的权限放行就是错的。

    * 三个读工具：`read_only_hint=True`（客户端可以自动放行）
    * `create_ticket`：`idempotent_hint=True`（重复调用安全）、**不标 destructive**
      （它只新建，不修改/删除既有数据；滥用 destructive 会让确认框失去意义）
    """
    async with Client(customer_server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    for name in ("sql_query", "retrieve_knowledge", "build_chart"):
        assert tools[name].annotations.read_only_hint is True, f"{name} 应当标只读"
        assert not tools[name].annotations.destructive_hint, f"{name} 不该标 destructive"

    ticket = tools["create_ticket"]
    assert ticket.annotations.idempotent_hint is True
    assert ticket.annotations.read_only_hint is not True, "建工单会写库，不能标只读"
    assert not ticket.annotations.destructive_hint, "建工单不是破坏性操作，标了会让确认框失去意义"


async def test_unknown_tool_name_is_reported_as_an_error(customer_server):
    """调一个不存在的工具（协议层）→ 错误结果，且消息里有工具名。"""
    async with Client(customer_server) as client:
        result = await client.call_tool("create_refund", {"user_id": 1})

    assert result.is_error is True
    assert "create_refund" in tool_text(result) or "Unknown tool" in tool_text(result)


async def test_invalid_arguments_are_reported_with_the_tool_name(customer_server):
    """参数形状不对（缺必填）→ 错误结果由**客户端自己**能读懂并改正。"""
    async with Client(customer_server) as client:
        result = await client.call_tool("sql_query", {})  # 缺 sql

    assert result.is_error is True
    assert "sql_query" in tool_text(result)


def test_describe_tools_reports_the_registration_facts(customer_settings):
    """`describe_tools()` 读的是**注册事实**（不是常量表），运维自检与文档输出都用它。"""
    listing = describe_tools(customer_settings)
    assert [item["name"] for item in listing] == list(EXPOSED_TOOLS)
    sql = next(item for item in listing if item["name"] == "sql_query")
    assert sql["read_only"] is True
    assert set(sql["parameters"]) == {"sql", "params", "max_rows"}
    ticket = next(item for item in listing if item["name"] == "create_ticket")
    assert ticket["idempotent"] is True
    assert "idempotency_key" in ticket["parameters"]


def test_build_server_refuses_an_unusable_configuration():
    """配置不可用时**构造就失败**（不是等到第一次调用才暴露身份缺失）。"""
    with pytest.raises(ValueError, match="MCP_ACTOR_USER_ID"):
        build_server(McpSettings(policy="customer", actor_user_id=None))
    with pytest.raises(ValueError, match="policy 必须是"):
        build_server(McpSettings(policy="wide-open", actor_user_id=1))


def test_two_servers_with_different_identities_do_not_interfere(customer_settings, internal_settings):
    """同一个进程里造两个不同身份的 server：身份是闭包捕获的，不是模块级全局。

    这条不是「顺便测一下」：多租户部署（一个对客服务 + 一个内部服务）就靠它，
    而模块级全局会让后构造的那个把前一个的身份覆盖掉 —— 那种 bug 只在生产上显形。
    """
    customer = build_server(customer_settings)
    internal = build_server(internal_settings)
    assert customer is not internal
    assert customer.name == internal.name == SERVER_NAME
    for server in (customer, internal):
        names = [tool.name for tool in server._tool_manager.list_tools()]  # noqa: SLF001
        assert names == list(EXPOSED_TOOLS)


# ── CLI 的两个入口 ─────────────────────────────────────────────────────────────


def _run_cli(module: str, *args: str, actor_user_id: str = "1") -> subprocess.CompletedProcess[str]:
    """在子进程里跑一次 CLI（`-m app.mcp` / `-m app.mcp.server`）。

    为什么必须起子进程：CLI 的坑几乎全在**进程边界**上（`sys.exit` 的码、
    `-m` 与包的 `__import__` 顺序、stdout 与 stderr 的分工）。
    用 `inspect` 读函数签名是看不出这些的 —— 本步实测就是靠这条用例抓到
    「`-m app.mcp.server` 静默退出 0」的（runbook §10.1 坑 10）。
    """
    env = {**os.environ, "MCP_ACTOR_USER_ID": actor_user_id, "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )


def test_check_json_lists_the_exposure_and_exits_zero():
    """`--check --json`：供 CI/脚本消费的自检出口（退出码 0）。"""
    done = _run_cli("app.mcp", "--check", "--json")
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["server"] == SERVER_NAME
    assert payload["policy"] == "customer"
    assert [item["name"] for item in payload["tools"]] == list(EXPOSED_TOOLS)
    assert all(item["description"] for item in payload["tools"])


def test_check_refuses_to_start_without_an_identity():
    """★配置不可用 = 退出码 **2**（没跑起来），不是 1（不达标）也不是 0。"""
    done = _run_cli("app.mcp", "--check", actor_user_id="")
    assert done.returncode == 2, f"stdout={done.stdout!r} stderr={done.stderr!r}"
    assert "MCP_ACTOR_USER_ID" in done.stdout, "报错必须说清怎么修（缺哪个配置项）"


def test_both_cli_entry_points_behave_identically():
    """★`-m app.mcp` 与 `-m app.mcp.server` 必须**完全一致**（它们是同一个 main）。

    实测撞到的坑：`server.py` 第一版没有 `if __name__ == "__main__"` 的 guard，
    而 `python -m app.mcp.server` 是把该模块当 `__main__` 执行 —— 于是那条命令
    **什么都不做、静默退出 0**。一个静默成功的命令比报错的命令更危险（会被写进运维脚本）。
    """
    package = _run_cli("app.mcp", "--check", "--json")
    module = _run_cli("app.mcp.server", "--check", "--json")
    assert package.returncode == module.returncode == 0
    assert package.stdout == module.stdout, "两个入口的输出不一致"
    assert "RuntimeWarning" not in package.stderr, package.stderr
    assert "RuntimeWarning" not in module.stderr, module.stderr
