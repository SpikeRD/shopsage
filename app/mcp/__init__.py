"""MCP 层（第 15 步）：把 `6.tools.md` 的能力以 **MCP 协议**暴露给外部客户端。

`15.mcp.md` 的三条关键设计决策在这里落成可读的结构：

| 文档的决策 | 本模块的落点 |
|---|---|
| ① MCP 是「薄壳」，不是「重写」 | `tools.py` 的四个函数**只做**线程 + 异常翻译；每个工具内部直接调 `app/tools/` 的既有函数（§4 的源码扫描钉住这一条） |
| ② 与内部调用共用同一安全层（D1） | `sync_calls.py` 把服务端声明的身份变成工具层能执行的形状：SQL 强制行级过滤、工单强制本人；守卫 / 只读引擎 / RBAC / 审计全部复用第 4、6 步的实现 |
| ③ 只暴露「允许外部调用」的子集 | `settings.EXPOSED_TOOLS` 就是**唯一清单**：`sql_query` / `retrieve_knowledge` / `create_ticket` / `build_chart`。退款（`create_refund_request`）与 `get_*` 查询**不暴露** |

文件职责（与第 6 步 `app/tools/` 同样的分法：配置 / 逻辑 / 装配 / 门面各一处）：

```
app/mcp/
├─ settings.py    # 配置面：策略、身份、上限 + 对外工具名与描述的常量表（**唯一真相**）
├─ sync_calls.py  # 同步实现：身份边界 + 结果形状（脚本与协议共用，只有一处真相）
├─ tools.py       # 协议薄壳：async + 线程 + 异常翻译（四个工具，无业务逻辑）
├─ server.py      # 装配：注册工具、注解、CLI 入口（`build_server` / `describe_tools` / `main`）
└─ __main__.py    # `python -m app.mcp`（与 `-m app.mcp.server` 等价）
```

两条使用纪律（与 `4.security.md` 的四条铁律一脉相承）：

1. **身份只能来自 `McpSettings`**，永远不是工具参数。任何一个「客户端传 user_id 就按它查」
   的入口，都等于把 D1 的第二半交给不可信输入。
2. **本包不 import `app.api` / FastAPI**：MCP 是一个可独立部署的进程，
   把 web 层拖进来会让「只想跑 MCP」的场景付一次应用启动的代价（同 `app/eval` 的纪律）。

自查出口（`docs/runbook/15.mcp-runbook.md` §2.2）：

    uv run python -m app.mcp --check

⚠️ **本文件是「惰性门面」**（PEP 562 的模块级 `__getattr__`），不在这里 import 子模块。
理由是一个实测撞到的坑（runbook §10.1 坑 10）：`python -m app.mcp.server` 会先 import 包
`app.mcp`（即本文件），若本文件已经把 `app.mcp.server` 也 import 进来了，Python 就会警告

    RuntimeWarning: 'app.mcp.server' found in sys.modules after import of package 'app.mcp',
    but prior to execution of 'app.mcp.server'; this may result in unpredictable behaviour

—— 而 `-m app.mcp` 与 `-m app.mcp.server` 是**同一件事**，必须都干净可用。
惰性门面同时保住两个性质：`from app.mcp import build_server` 照旧可用（按需加载），
而 `python -m app.mcp.server` 不再提前把该模块塞进 `sys.modules`。
"""

from __future__ import annotations

from typing import Any

#: 出口名 → 它所在的子模块（**唯一真相**；`__getattr__` 与 `__all__` 都读它）
_EXPORTS: dict[str, str] = {
    # app/mcp/server.py：装配与 CLI
    "build_server": "server",
    "describe_tools": "server",
    "main": "server",
    "Transport": "server",
    # app/mcp/settings.py：配置面
    "EXPOSED_TOOLS": "settings",
    "POLICY_CUSTOMER": "settings",
    "POLICY_INTERNAL": "settings",
    "SERVER_NAME": "settings",
    "SERVER_VERSION": "settings",
    "TOOL_DESCRIPTIONS": "settings",
    "McpSettings": "settings",
    "get_settings": "settings",
    # app/mcp/sync_calls.py：身份边界（脚本侧也常用这三个）
    "assert_customer_row_filter": "sync_calls",
    "jsonable": "sync_calls",
    "resolve_ticket_target": "sync_calls",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """按需加载（PEP 562）：第一次访问 `app.mcp.build_server` 时才 import `server`。"""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    globals()[name] = value  # 缓存：后续访问不再走 import
    return value


def __dir__() -> list[str]:
    return __all__
