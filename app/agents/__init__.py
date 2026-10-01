"""六个业务 Agent（第 7 步）：LangGraph 图的**节点实现**，每个只做一件边界清晰的事。

按 `7.agents_module.md` 分两批交付：

| 批次 | Agent | 文件 | 职责 |
|---|---|---|---|
| 一（第 7 步） | Router | `router.py` | 意图识别 + 客户/员工分流 + 权限上下文注入 |
| 一 | Data | `data.py` | NL→SQL → 校验 → 执行 → 报错重试 |
| 一 | Synthesis | `synthesis.py` | 按角色合成最终回答（对客/对内/财务三套模板） |
| 一 | Guardrail | `guardrail.py` | 输入/输出双向合规审查（横切） |
| 二（第 11 步） | RAG | `rag.py` | 知识库检索 + 溯源（后端属第 10 步） |
| 二 | Analysis | `analysis.py` | 对内聚合解读（结论 + 可选图表） |

三条使用口径（与 `docs/runbook/7.agents-runbook.md` §0 一致）：

1. **节点只写自己那几个键**：`await router_node(state)` 返回 `{"route": ...}` 这样的**增量**，
   由第 8 步的图合并进主 state。不要 `state.update(await node(state))` 之后再传整份 state ——
   那会让「哪个键是谁写的」在调试时无处可查。
2. **LLM 一律可注入**：每个节点都有 `llm=` 参数，测试传假函数即可跑全部路径。
   生产路径默认走 `agent_llm.default_llm` → `app.llm.llm_call`（**唯一**的模型出口）。
3. **Agent 不碰数据库、不碰 litellm**：取数走 `app.tools`，模型调用走 `app.llm`。
   第 6 步已经把「唯一副作用出口」钉在 tools 上，agent 这一层只做编排。

⚠️ 本包**不 import langgraph**（`langgraph` 至今不在依赖里，属第 8 步）。
第 8 步的 `app/graph/builder.py` 直接 `add_node("router", router_node)` 即可。
"""

from app.agents.agent_llm import AgentOutputError, JsonLLM, generate_json
from app.agents.analysis import analysis_node
from app.agents.data import data_node
from app.agents.guardrail import guardrail_node
from app.agents.rag import rag_node
from app.agents.router import classify, router_node
from app.agents.state import GraphState, inject_identity
from app.agents.synthesis import synthesis_node

__all__ = [
    "AgentOutputError",
    "GraphState",
    "JsonLLM",
    "analysis_node",
    "classify",
    "data_node",
    "generate_json",
    "guardrail_node",
    "inject_identity",
    "rag_node",
    "router_node",
    "synthesis_node",
]
