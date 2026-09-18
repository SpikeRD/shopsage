# graph — LangGraph 编排与断点重放（checkpointer）

## 1. 职责

把六个 Agent（`agents_module.md`）组装成**有状态的有向图**，负责：路由分支、节点顺序、状态流转、失败恢复。
Agent 是「节点」，graph 是「图 + 状态机 + 持久化」。

## 2. 对外接口

```python
from app.graph.builder import build_graph, compile_graph
from app.graph.state import GraphState           # TypedDict：定义节点间共享状态

graph = compile_graph()                           # 编译后的可执行图
result = await graph.ainvoke(initial_state, config={"configurable": {"thread_id": "会话ID"}})
```

## 3. 依赖

- `langgraph`（StateGraph、checkpointer、条件边）
- `langgraph-checkpoint-postgres` / `langgraph-checkpoint-redis`（持久化 checkpointer）
- `agents_module.md`（节点）、`config.md`（checkpointer 连接串）

## 4. 关键设计决策

1. **图结构 = 架构第 4 节的图**：`router → (data | rag | analysis) → synthesis`，guardrail 在 router 后、synthesis 后横切。
2. **checkpointer 外置到 Postgres/Redis**：节点状态每步落盘，进程重启、多副本都不丢状态（A2）。
3. **`thread_id` = 会话 id**：同一会话复用同一 `thread_id`，checkpoint 按 thread 隔离，天然支持多用户并发。
4. **条件边做路由**：`router` 输出 `route` 字段，用 `add_conditional_edges` 决定下一步，而不是在图里硬编码 if/else。
5. **失败从断点重放**：节点抛异常时 checkpoint 已保存已完成节点，重试只跑失败节点及其后续，不重头跑（A2）。

## 5. 核心技术点（要懂为什么）

- **State 是「显式的数据契约」**：`GraphState` 用 TypedDict 定义每个节点能读写哪些字段（如 `messages`、`route`、`user_id`、`sql`、`answer`），比隐式传参可读、可测、可持久化。
- **checkpointer 为什么能「断点重放」**：LangGraph 在每个节点执行后把 state 快照 + 输入输出序列化到存储；恢复时按 `thread_id` 找到最新 checkpoint，从下一节点继续。
- **幂等是重放的配套前提**：图能重放，但若节点内部「下单/建工单/计费」不幂等，重放会重复执行——所以写操作要带幂等 key（呼应 `tools.md`、A2）。
- **human-in-the-loop**：对「退款/工单」等敏感写操作，用 `interrupt` 在节点前暂停，等人工批准再继续（架构「human-in-the-loop」）。

## 6. 开发步骤

1. 定义 `GraphState`（TypedDict 字段清单）。
2. 写 `builder.py`：`StateGraph` → `add_node`（六个 agent）→ `add_conditional_edges`（按 route）→ `compile(checkpointer=...)`。
3. 接 Postgres checkpointer（`langgraph-checkpoint-postgres`）。
4. 写最小可跑链路：router → data → synthesis 先通，guardrail 在 router 后 / synthesis 后横切接入（第 7 步已实现）；rag/analysis 留到第 11 步再补。
5. 写「断点重放」演示脚本：在某节点人为抛错 → 观察从断点恢复。
6. 对敏感写操作加 `interrupt`（human-in-the-loop）。

## 7. 测试用例

```python
def test_router_to_data_path():          # route=data 走 data→synthesis，不经过 rag
def test_checkpoint_persists_state():    # 节点 2 抛错后，checkpoint 存在，重放不重跑节点 1
def test_replay_is_idempotent():         # 重放后写操作不重复（工单只建一次）
def test_thread_isolation():             # 两个 thread_id 的 state 互不干扰
def test_guardrail_short_circuits():     # guardrail 拒绝时，后续节点不执行
```

## 8. 验收标准

- [ ] 完整图 `router → data/rag/analysis → synthesis` + guardrail 横切可跑通。
- [ ] 节点中途失败后从断点重放，不重头跑、不重复写操作（A2）。
- [ ] checkpoint 持久化到 Postgres/Redis，进程重启不丢状态。
- [ ] 证据链接：`PR#__` / `断点重放演示#__`
