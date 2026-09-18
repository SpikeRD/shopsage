# mcp — MCP Server（以统一协议对外暴露 tools 能力）

## 1. 职责

把 `tools.md` 的能力（数据库只读查询、知识库检索、图表、工单）以 **MCP 协议**暴露出去，让外部 MCP 客户端（如 Claude/Cursor/其他 Agent）也能安全调用，与内部直接调用并存（架构第 3 节补充模块 6）。

## 2. 对外接口

```text
MCP 工具列表（经 MCP 协议发现/调用）：
- sql_query          # 只读 SQL 查询（过 validate_sql）
- retrieve_knowledge # 知识库检索
- create_ticket      # 建工单（写路径，走预定义函数）
- build_chart        # 图表 spec
```

## 3. 依赖

- `mcp`（Python SDK，`FastMCP`）
- `tools.md`（直接复用，不重复实现）
- `security.md`（MCP 暴露的 `sql_query` 同样过 `validate_sql` + 行级过滤）

## 4. 关键设计决策

1. **MCP 是「薄壳」，不是「重写」**：MCP 的每个工具内部直接调 `tools.md` 的已有函数，避免两套实现、两套安全校验。
2. **MCP 与内部调用共用同一安全层**：外部 MCP 客户端和内部 Agent 一样受 `security.md` 约束（D1 不因入口不同而失效）。
3. **MCP 只暴露「允许外部调用」的子集**：内部敏感能力（如直接写库）不暴露，只暴露经过审计的工具。

## 5. 核心技术点（要懂为什么）

- **MCP 解决什么**：让外部 AI 客户端用「统一协议 + 工具描述」发现并调用能力，而不是每个客户端各写一套集成。
- **为什么复用 tools 而非直接暴露 LLM 动态 SQL**：MCP 客户端也是「不可信输入」，必须走 `validate_sql` 白名单 + 行级过滤，与 D1 一致。

## 6. 开发步骤

1. `FastMCP` 初始化 server，注册 4 个工具（内部调 `tools.md`）。
2. 给每个工具写 MCP 工具描述（供客户端发现）。
3. 接入 `security.md` 校验。
4. 写 MCP 客户端冒烟测试。

## 7. 测试用例

```python
def test_mcp_sql_passes_guard():
    # MCP 调 sql_query 传入 DELETE → 被 validate_sql 拦截（D1）

def test_mcp_tools_match_tools():
    # MCP 暴露的工具与 tools.md 一一对应，无重复实现

def test_mcp_discovery():
    # 客户端能发现并列出工具及描述
```

## 8. 验收标准

- [ ] MCP Server 可被外部客户端发现并调用，能力与 `tools.md` 一致。
- [ ] MCP 入口同样受安全层约束，越权/写操作被拦（D1）。
- [ ] 证据链接：`PR#__`
