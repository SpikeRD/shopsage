# 模块 15 · mcp（MCP Server 薄壳）

## 目的

用 MCP 协议把 `app/tools/` 的四个能力暴露给外部客户端（Cursor / Claude Desktop 等），**不重新实现业务逻辑**，只加一层协议适配与身份边界。

## 关键结论

1. **是薄壳**：`app/mcp/server.py:95 build_server()` 只做协议装配，具体动作在 `app/mcp/sync_calls.py` 里转发给 `app/tools/`。
2. **四个工具**（`app/mcp/tools.py:59-62`）：`sql_query`、`retrieve_knowledge`、`create_ticket`、`build_chart`。
3. **SDK 是 2.x**：`FastMCP` 在 mcp 2.x 已改名为 **`MCPServer`**（`app/mcp/server.py:95`、模块 docstring :12 给了 `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` 这个真实报错）。
4. **身份边界是两条策略**：`MCP_POLICY=customer|internal`（`app/mcp/settings.py:58-60`）。
   - `customer` 策略下 `sql_query` **强制注入行级过滤**（`app/mcp/sync_calls.py:149 assert_customer_row_filter`）；
   - 参数里的身份与请求身份不一致 → 拒（`:182 check_params_match_identity`）。
5. **审计归因**：`caller_stamp`（`app/mcp/sync_calls.py:52`）把 `caller` 记成 `mcp:<tool>`。
6. **两个 `-m` 入口行为必须逐字节一致**：`python -m app.mcp` 与 `python -m app.mcp.server` 是同一个 `main()`（`app/mcp/server.py:248`）。
7. **不 import web 层**：MCP 层不许出现 `app.api` / `fastapi` / `uvicorn` / `starlette`（有静态检查用例）。

## 实际用法

```bash
# 不给身份 → 退出码 2，且报错说清怎么修
uv run python -m app.mcp
# 给身份 → 列出四个工具
uv run python -m app.mcp --policy customer --actor-user-id 1
```

```json
// Cursor / Claude Desktop 配置片段（形如）
{
  "mcpServers": {
    "shopsage": {
      "command": "uv",
      "args": ["run", "python", "-m", "app.mcp", "--policy", "customer", "--actor-user-id", "1"]
    }
  }
}
```

## 待办 / 已知边界

- ⚠️ MCP 的**真实 stdio 端到端**是否在本机验证过：`docs/runbook/15.mcp-runbook.md` 声称"进程内 Client 与真 stdio 双证据"，本次未复跑。→ `../../03-待确认清单.md` Q-04。
- `create_ticket` 在 `customer` 策略下会覆盖 `user_id`（`app/mcp/sync_calls.py:211 resolve_ticket_target`）—— 即"客户不能用 MCP 给别人的账号建工单"。

## 来源

- `docs/modules/15.mcp.md`（75 行）、`docs/runbook/15.mcp-runbook.md`（1035 行）
- 代码：`app/mcp/*.py`（6 文件）；测试：`tests/mcp/*.py`（7 文件）
