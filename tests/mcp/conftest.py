"""MCP 用例的公共装置：**两个身份 + 一个进程内客户端**。

为什么装置长这样（三条与前面几步不同的决定）：

1. **没有 tmp_path、没有 monkeypatch 环境变量**。第 14 步的实测教训（`14.eval-runbook.md`
   §10.1 坑 11）在沙箱里更严重：`tmp_path` 会 `PermissionError`，而 `monkeypatch.setenv`
   对 `_env_value` 那种「先看 `os.environ`」的读取路径有效、对已经 `lru_cache` 过的
   `get_settings()` 无效 —— 两种都会让用例**看起来在测配置、其实没测到**。
   所以身份全部**显式构造** `McpSettings(...)` 传进去（这也是 `build_server(settings)` 的意义）。
2. **客户端直接用 SDK 的进程内 `Client(server)`**（`mcp.Client` 支持传 `MCPServer` 实例）：
   不起子进程、不占端口、不需要 `stdio`。第 7 节的冒烟用例才真的走一次子进程 ——
   两条路各有各的证据价值，但日常回归只该跑前者（快、稳、无环境依赖）。
3. **写路径的用例共用 `test-mcp-` 前缀**（与 `6.tools-runbook.md` §7.3 同一条纪律：
   假实现测不出唯一约束，所以真插行，但必须能收尾）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from app.mcp.server import build_server
from app.mcp.settings import McpSettings
from app.security.policy import CUSTOMER_ROLE

#: 用例造的工单/退款行一律带这个前缀（收尾时按它清理）
TEST_KEY_PREFIX = "test-mcp-"


def pytest_collection_modifyitems(config, items):
    """默认 skip 真依赖那组，并**说清怎么跑**（与 `tests/rag` 的 `--rag-live` 同一条口径）。

    开关本身注册在**根** `tests/conftest.py`（本仓踩过的坑：子目录 conftest 注册的
    选项在全量运行时会导致 `unrecognized arguments`），这里只负责"默认不跑"。
    """
    if config.getoption("--mcp-live", default=False):
        return
    skip = pytest.mark.skip(reason="需要真依赖栈：加 `--mcp-live` 才跑（见 tests/conftest.py）")
    for item in items:
        if item.get_closest_marker("mcp_live"):
            item.add_marker(skip)


@pytest.fixture
def customer_settings() -> McpSettings:
    """对客身份：user_id=1（种子数据里的真实客户），角色 customer。"""
    return McpSettings(policy="customer", actor_user_id=1, actor_role=CUSTOMER_ROLE)


@pytest.fixture
def internal_settings() -> McpSettings:
    """内部身份：员工角色（有 `read:all_orders` 与 `write:ticket`）。"""
    return McpSettings(policy="internal", actor_user_id=1, actor_role="internal_employee")


@pytest.fixture
def customer_server(customer_settings: McpSettings):
    return build_server(customer_settings)


@pytest.fixture
def internal_server(internal_settings: McpSettings):
    return build_server(internal_settings)


def run(coro):
    """同步用例里跑协程（本仓 `asyncio_mode=auto`，async 用例直接写 `async def` 即可）。

    保留这个工具是给「需要在同步 fixture 里跑一次协程」的场景用的（例如断言服务端
    工具清单需要 `list_tools()`）。它只在没有事件循环时新建一个，避免嵌套循环报错。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("run() 不能在已有事件循环里调用：直接把用例写成 async def")


def tool_text(result: Any) -> str:
    """从 `CallToolResult` 里取出文本内容（错误消息也在里面，见 `mcp_error_text`）。"""
    parts: list[str] = []
    for item in result.content or ():
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def tool_json(result: Any) -> Any:
    """把工具返回的 JSON 文本解析回对象（工具结果统一是 `json.dumps` 过的 dict）。"""
    return json.loads(tool_text(result))


def mcp_error_text(result: Any) -> str:
    """失败调用的可读消息。

    ⚠️ SDK 会加一层前缀：`Error executing tool <name>: <我们抛的 ToolError 文本>`。
    所以「消息里有 `[rule]`」这类断言要在这层之上去做 —— 这也正是我们翻译异常时
    坚持把规则名写进消息的原因（第 6 步 §2.6 的教训）。
    """
    return tool_text(result)


def unique_key(label: str = "t") -> str:
    """本次用例专用的幂等键（唯一，避免用例之间通过数据库串味）。"""
    return f"{TEST_KEY_PREFIX}{label}-{uuid.uuid4().hex[:10]}"
