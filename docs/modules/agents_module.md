# agents — 六个业务 Agent（router / data / rag / analysis / synthesis / guardrail）

## 1. 职责

LangGraph 图中的**节点实现**。每个 Agent 只做一件边界清晰的事，由 `graph.md` 负责把它们串成有状态图。
单个 Agent 不感知整体流程，只负责「吃输入 → 出结构化结果」。

| Agent | 文件 | 职责 |
|---|---|---|
| Router | `router.py` | 意图识别 + 客户/员工分流 + 权限上下文注入 |
| Data | `data.py` | NL→SQL、执行、校验重试（调用 `security.sql_guard` + `tools`） |
| RAG | `rag.py` | 知识库混合检索 + rerank + 溯源（调用 `rag/`） |
| Analysis | `analysis.py` | 对内数据聚合解读（可选图表） |
| Synthesis | `synthesis.py` | 按角色合成最终回答 |
| Guardrail | `guardrail.py` | 输入/输出双向合规审查（注入防护） |

## 2. 对外接口

```python
# 每个 Agent 暴露一个可被 LangGraph 节点调用的函数，输入输出走共享 state（TypedDict）
def router_node(state: GraphState) -> GraphState: ...   # 决定 next 走哪条边
def data_node(state: GraphState) -> GraphState: ...
def rag_node(state: GraphState) -> GraphState: ...
def analysis_node(state: GraphState) -> GraphState: ...
def synthesis_node(state: GraphState) -> GraphState: ...
def guardrail_node(state: GraphState) -> GraphState: ...
```

## 3. 依赖

- `langchain`（SQLDatabaseToolkit 等）+ `llm`（LiteLLM 接入层）
- `tools/`（SQL 执行器、检索、图表、工单）
- `security/`（data 执行前校验、synthesis 执行后脱敏、guardrail 合规）
- `db/`、`rag/`、`cache/`

## 4. 关键设计决策

1. **每个 Agent 输出结构化 JSON，而非自由文本**：`router` 输出 `{"route": "data", "user_role": "customer"}`，便于图里用条件边分支、便于 guardrail 校验（架构「LLM 只接受结构化、受约束指令」）。
2. **Guardrail 是「横切」而非「顺序节点」**：在 router 之后、synthesis 之后都挂 guardrail，输入输出双向把关（D2），失败即短路。
3. **Data Agent 的「校验重试」**：SQL 生成 → `validate_sql` 拦截 → 把错误回喂 LLM 让它重写，最多 N 次，仍失败则降级为「无法查询」（A1 思想的节点级应用）。
4. **权限上下文在 Router 注入**：`user_id` / `role` 进入 state，后续 data/analysis 只消费 state，不自行判断身份（避免越权判断分散在各处）。

## 5. 核心技术点（要懂为什么）

- **为什么用「意图分类」而不是一个大 prompt 全干**：单 Agent 大 prompt 意图模糊、难调试、难评测；拆开后每个 Agent 的输入输出可单独测试、单独调 prompt。
- **few-shot 怎么做**：给 Data Agent 塞「schema + 3~5 条 (问题, 正确SQL) 示例」，显著提升 NL→SQL 准确率，且示例固定可回归。
- **报错重试的 prompt 怎么写**：把 `validate_sql` 的**具体报错**（如「缺少 WHERE」）回喂模型，让它针对性修复，而不是笼统说「再试一次」。
- **合成角色的语气差异**：对客「礼貌、简短、口语」；对内「专业、详细、可含数据表格」，用两套 system prompt 模板切换。

## 6. 开发步骤

1. 先写 `guardrail.py`（最小可用：注入关键词 + LLM 判定双重校验）。
2. 写 `router.py`：意图分类 prompt + 结构化输出解析 + 权限上下文注入。
3. 写 `data.py`：接 `SQLDatabaseToolkit` → few-shot → `validate_sql` → 执行 → 报错重试。
4. 写 `rag.py`：调 `rag/` 检索接口 + rerank + 溯源字段。
5. 写 `analysis.py`：聚合查询 + 结论生成 + 可选图表数据。
6. 写 `synthesis.py`：两套角色模板合成。
7. 每个 Agent 单独写单测（mock LLM，测 prompt 输出解析与分支）。

## 7. 测试用例

```python
def test_router_routes_to_data():       # "查我的订单" → route=data, role=customer
def test_router_routes_to_rag():        # "退货政策是什么" → route=rag
def test_data_retries_then_gives_up():  # 非法 SQL 重试 N 次后返回降级答案
def test_data_sql_passes_guard():       # 生成的 SQL 一定先过 validate_sql
def test_synthesis_customer_tone():     # 对客回答短、无内部术语
def test_guardrail_blocks_injection():  # 输入/输出注入都被拦（D2）
```

## 8. 验收标准

- [ ] 六个 Agent 均可独立调用、独立测试（不依赖整图跑通）。
- [ ] Router 分流准确率在 golden set 上达标（配合 `eval.md`）。
- [ ] Data Agent 生成 SQL 100% 过 `validate_sql`，越权被拦（D1）。
- [ ] Guardrail 双向拦截注入（D2）。
- [ ] 证据链接：`PR#__` / `eval/report#__`
