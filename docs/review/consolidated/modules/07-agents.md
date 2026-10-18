# 模块 07 · agents（业务 Agent / 图节点实现）

## 目的

七个"只做一件事"的函数，每个都能**脱离整图独立调用与独立测试**（`fn(state, *, llm=...) -> dict`），由 `app/graph/nodes.py` 包成图节点。

## 关键结论

1. **是 7 个节点，不是 6 个 Agent**：router / data / rag / analysis / **refund**（变更 003 新增）/ synthesis / guardrail。`docs/modules/7.agents_module.md` 与 `docs/architecture.md:105-112` 的"六个 Agent"是变更 003 之前的口径 —— 见 `../../02-异常清单.md`。

   | 节点 | 文件 | 职责 | 注入点 |
   |---|---|---|---|
   | `router` | `app/agents/router.py:181` | 意图分类（data/rag/analysis/ticket/refund/final）+ **权限注入** | `llm=` |
   | `data` | `app/agents/data.py:436` | NL→SQL → 守卫 → 执行 → 报错回喂重试 | `llm=` `row_limit=` `max_attempts=` |
   | `rag` | `app/agents/rag.py:87` | 混合检索 + 溯源 + 语义缓存前置 | `search=` `top_k=` `min_score=` `cache=` |
   | `analysis` | `app/agents/analysis.py:160` | 对内聚合结论 + 可选图表（输入是 data 的 `rows`） | `llm=` `chart_type=` |
   | `refund` | `app/agents/refund.py:158` | 对客退款申请 —— **唯一写节点，且刻意不接 LLM** | 无 |
   | `synthesis` | `app/agents/synthesis.py:269` | 按角色三套模板合成 `answer` + `citations`；命中缓存直接返回 | `llm=` `cache=` |
   | `guardrail` | `app/agents/guardrail.py:188` | 输入侧（零 token）与输出侧（LLM 判定）双向把关 | `stage=` `llm=` |

2. **权限只在 router 注入一次**（`app/agents/state.py:238 inject_identity()`）：权限从 `rbac.ROLE_PERMISSIONS` **查表**得来，绝不采信模型输出。后续节点只读 `state["permissions"]` / `is_customer_path(state)`。
3. **对客路径的三个硬约束**（`app/agents/data.py`）：
   - 强制 `WHERE user_id = :uid`（形状校验）；
   - **绑定值由程序覆写**，不采信模型给的 `uid`（`_bind_customer_id` :420）；
   - 无 `read:own` 权限 → **不花 LLM 调用**，直接落 `refusals`（`:461-470`）。
4. **重试的砝码是"具体报错"**：`RETRY_ACTIONS`（`app/agents/data.py:247`）把校验失败规则映射成"该怎么改"的提示，回喂给模型，最多 `MAX_SQL_ATTEMPTS=3` 次；用尽则**降级**（写 `refusals`，不抛异常）。
5. **结构化输出集中处理**：`app/agents/agent_llm.py` 的 `generate_json()`（:191）负责"要一个 JSON、解析、失败回喂修复一次"（`DEFAULT_REPAIRS=1`）；失败时 `AgentOutputError` 带**原始文本 + 失败原因**（:52）。
6. **合成按角色分三套模板**：`_CUSTOMER_SYSTEM`（≤300 字，`CUSTOMER_MAX_CHARS` :46）/ `_INTERNAL_SYSTEM` / `_FINANCE_SYSTEM`，路由函数 `role_template()`（:90）。
7. **输出护栏要"补事实"**：`_materials_digest()`（`app/agents/guardrail.py:291`）必须把 `refund` 的结果与 `cache_info` 一起喂给判定者 —— 否则"这次真的受理了一笔退款""这条答案来自缓存"都会被判成编造。这条规则踩过三次（`issues/009`）。

## 实际用法

```python
# 独立调用一个节点（测试与脚本都这么用）
from app.agents.data import data_node
update = await data_node(state, llm=fake_llm, row_limit=20, max_attempts=3)
# → {"rows": [...], "sql": "...", "row_meta": {...}} 或 {"refusals": [...]}

# app/agents/state.py:238
inject_identity(user_id=2, role="customer")
# → {"permissions": frozenset({'read:own','write:ticket','request:refund'}), "is_customer": True, ...}
```

## 待办 / 已知边界

- `docs/modules/7.agents_module.md` §8 六条验收**全部未打勾**（`[ ]`），但实现与用例都在 —— 属"证据未回填"，不是"未完成"（`../../02-异常清单.md` E-07）。
- `issues/001`：`analysis` 出图是**非确定性**的（模型偶尔给不出严格 JSON），只做了部分处理，剩余部分未修。
- 未实现"对话历史摘要 / 上下文窗口管理"（`docs/problem-solving.md` E2 的方案之一）⚠️ —— 见 `../../03-待确认清单.md`。

## 来源

- `docs/modules/7.agents_module.md`（65 行）、`docs/runbook/7.agents-runbook.md`（1081 行）
- `docs/changes/003`、`issues/001`、`issues/009`
- 代码：`app/agents/*.py`；测试：`tests/test_agents_*.py`
