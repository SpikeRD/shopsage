# MCP（Model Context Protocol，把工具暴露给外部客户端）

## 本项目用它解决什么

把 `app/tools/` 的四个能力（SQL 查询、知识库检索、建工单、出图表）以 MCP 协议暴露给 Cursor / Claude Desktop 这类客户端，
让"工具层"在 API 之外多一个消费者，同时**不新增一套业务实现**。

## 实际用法（代码片段 + 文件:行号）

### 1）SDK 是 2.x：`MCPServer` 而不是 `FastMCP`

```python
# app/mcp/server.py:12（模块 docstring 里的真实报错）
# ModuleNotFoundError: No module named 'mcp.server.fastmcp'; This is mcp 2.x,
# where FastMCP was renamed to MCPServer …

# app/mcp/server.py:95
def build_server(settings: McpSettings | None = None) -> MCPServer: ...
```

### 2）四个工具名

```python
# app/mcp/tools.py:59-62
SQL_QUERY: Final[str] = "sql_query"
RETRIEVE_KNOWLEDGE: Final[str] = "retrieve_knowledge"
CREATE_TICKET: Final[str] = "create_ticket"
BUILD_CHART: Final[str] = "build_chart"
```

### 3）两条身份边界

```python
# app/mcp/settings.py:58-60
POLICY_CUSTOMER: Final[str] = "customer"
POLICY_INTERNAL: Final[str] = "internal"
```

```python
# app/mcp/sync_calls.py:149 / :182 / :211
def assert_customer_row_filter(sql: str) -> None: ...          # customer 策略强制行级过滤
def check_params_match_identity(...) -> None: ...              # 参数身份必须与请求身份一致
def resolve_ticket_target(user_id: int | None, settings: McpSettings) -> int: ...
```

### 4）审计归因

```python
# app/mcp/sync_calls.py:52
class caller_stamp:  # 当上下文管理器用
    # 把 caller 记成 mcp:<tool>
```

### 5）入口与退出码

```python
# app/mcp/server.py:248
def main(argv: list[str] | None = None) -> int: ...
# 不给身份 → 退出码 2（且报错说清怎么修）；给了 → 列出四个工具
```

```bash
uv run python -m app.mcp --policy customer --actor-user-id 1
uv run python -m app.mcp.server --policy customer --actor-user-id 1   # 行为必须逐字节一致
```

## 最小原理

1. **MCP 是"客户端 ↔ 服务器"的 JSON-RPC 协议**：客户端先 `initialize` 握手，再 `tools/list` 拿工具清单，最后 `tools/call` 调用。传输层可以是 stdio 或 HTTP（streamable-http / sse）。
2. **stdio 传输的关键**：stdio 既是"协议通道"也是"控制台" —— 所以**日志必须走 stderr**，任何打到 stdout 的东西都会污染协议流。
3. **工具描述（description + schema）就是给模型看的"说明书"**：写得含糊，模型就会乱传参。
4. **身份不能由客户端自称**：所以 `MCP_POLICY` 在**服务端**决定，客户端传的 `user_id` 只用于校验一致性。

## 常见坑

| 坑 | 症状 | 依据 |
|---|---|---|
| 用 `mcp.server.fastmcp` | `ModuleNotFoundError`（2.x 已改名） | `app/mcp/server.py:12` |
| 往 stdout 打日志 | 协议流被污染，客户端报解析错误 | `app/mcp/server.py:68` 的 `_pin_utf8_streams()` |
| Windows 控制台编码 | `UnicodeEncodeError: gbk` | `docs/runbook/15.mcp-runbook.md` §10.1 最后一个坑 |
| MCP 层 import `app.api` | 为了一个工具把整个 Web 栈拖进来 | `docs/runbook/15.mcp-runbook.md` §2.4 |
| 信客户端传的 `user_id` | 等于让调用者自己决定权限 | `app/mcp/sync_calls.py:182` |

## 深入关键词

`JSON-RPC 2.0` · `initialize` / `tools/list` / `tools/call` · stdio vs streamable-http transport · `MCPServer` / `FastMCP` 改名 · 工具输入 schema（JSON Schema）· stdout 与协议流隔离 · `.mcp.json` / Cursor 配置
