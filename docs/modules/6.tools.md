# tools — 工具层（SQL 执行器 / 知识库检索 / 图表 / 工单）

## 1. 职责

把「Agent 需要调用外部能力」的动作封装成**可复用、可审计、可限流**的工具函数。
Agent 只调用工具，不直接碰数据库/检索库/工单系统。是 `mcp/` 对外暴露的能力来源（架构第 3 节补充模块 6）。

## 2. 对外接口

```python
from app.tools.sql_executor import execute_readonly_query
from app.tools.retriever import retrieve_docs
from app.tools.chart import build_chart_spec
from app.tools.ticket import create_ticket, get_ticket_status
```

## 3. 依赖

- `db/`（SQL 执行器走读写分离的只读引擎）
- `rag/`（检索工具）
- `security/`（SQL 执行器内部先过 `validate_sql`）
- 被 `agents/`、`mcp/` 调用

## 4. 关键设计决策

1. **工具是「唯一副作用出口」**：所有写操作（建工单、退款）只能通过工具里的**预定义函数**，参数显式、带审计日志；LLM 永远接触不到直接写库能力（读写路径分离，D1）。
2. **每个工具带审计字段**：调用者、时间、参数、结果行数，写入日志，满足「工单/退款走审计」要求。
3. **检索工具只返回「带溯源」的结果**：`retrieve_docs` 返回 `(文本, 来源文档, 页码/片段)`，供 synthesis 附出处，杜绝「编造答案」。

## 5. 核心技术点（要懂为什么）

- **SQL 执行器为何二次校验**：即使 `agents/data.py` 已过 `validate_sql`，工具层再验一遍（纵深防御），因为工具可能被 `mcp/` 或其他路径直接调用。
- **结果行数硬上限**：执行器强制 `LIMIT`，超限截断并标记 `truncated`，防止内存/带宽打爆。
- **图表工具返回「规范」而非「图片」**：返回结构化图表 spec（类型、数据、坐标），前端渲染；解耦后端与前端绘图库。
- **工单工具幂等**：`create_ticket` 带幂等 key，重复请求不重复建单（呼应 A2 幂等）。

## 6. 开发步骤

1. 写 `sql_executor.py`：入参 SQL → `validate_sql` → 只读引擎执行 → 返回行 + 行数 + truncated 标记。
2. 写 `retriever.py`：包 `rag/` 的检索，输出带溯源结构。
3. 写 `chart.py`：`build_chart_spec` 结构化输出。
4. 写 `ticket.py`：`create_ticket`（幂等）/ `get_ticket_status`，走写路径 service API。
5. 每个工具写单测（含审计日志断言、越权断言）。

## 7. 测试用例

```python
def test_sql_executor_rejects_write():
    with pytest.raises(SQLSecurityError):
        execute_readonly_query("DELETE FROM orders")

def test_sql_executor_truncates():
    rows, meta = execute_readonly_query("SELECT * FROM orders")
    assert meta["truncated"] is True and len(rows) <= MAX_ROWS

def test_create_ticket_idempotent():
    k1 = create_ticket(..., idempotency_key="x"); k2 = create_ticket(..., idempotency_key="x")
    assert k1 == k2  # 不重复建单

def test_retrieve_docs_has_citation():
    results = retrieve_docs("退货政策")
    assert all(r.source for r in results)
```

## 8. 验收标准

- [ ] 所有写操作只经预定义工具函数，LLM 无写库能力（D1）。
- [ ] 工具调用带审计日志；SQL 执行器二次校验 + 行数截断（C1）。
- [ ] 工单/退款幂等（A2）。
- [ ] 证据链接：`PR#__`
