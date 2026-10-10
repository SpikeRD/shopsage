"""LangGraph 编排层（第 8 步）：把六个 Agent 组装成**有状态的有向图**。

`8.graph.md` §2 的三行用法在门面里都能拿到：

```python
from app.graph import GraphState, build_graph, compile_graph, open_checkpointer, graph_config

async with open_checkpointer("postgres") as saver:      # 状态外置（进程重启不丢）
    graph = compile_graph(saver)
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "我的订单到哪了"}], "user_id": 2, "role": "customer"},
        graph_config("会话ID"),                          # thread_id = 会话 id + durability="sync"
    )
```

包的边界（与第 7 步同一条纪律）：

1. **本包不实现节点**。它只做四件事：绑定注入点、归一状态、装配图、管 checkpointer。
   六个节点的实现仍在 `app/agents/`，改 prompt 不需要动这里。
2. **本包不取数、不调模型**。模型出口是 `app.llm`（经 `app.agents`），取数是 `app.tools`。
   `app/graph/` 里出现 `litellm` / `SessionLocal` 就是越界（`tests` 用源码检查钉住）。
3. **本包不产生 `thread_id`**。它是会话 id，来自第 9 步的鉴权结果；
   本包只提供 `thread_config()` / `graph_config()` 这两个装配函数。
"""

from app.graph.builder import GRAPH_NAME, TURN_DURABILITY, build_graph, compile_graph, graph_config
from app.graph.checkpointer import (
    BACKENDS,
    CheckpointBackend,
    CheckpointUnavailable,
    ensure_compatible_event_loop,
    open_checkpointer,
    thread_config,
)
from app.graph.config import (
    APPROVAL_QUESTION,
    REJECTED_REASON,
    SENSITIVE_ROUTES,
    GraphRuntime,
    InterruptPolicy,
)
from app.graph.nodes import FALLBACK_MESSAGE, failure_update, normalize_input
from app.graph.routing import route_by_intent
from app.graph.run import DEFAULT_REPLAY_BUDGET, GraphTurnFailed, Turn, run_turn
from app.graph.state import (
    NODE_ANALYSIS,
    NODE_DATA,
    NODE_GATE,
    NODE_GUARDRAIL_INPUT,
    NODE_GUARDRAIL_OUTPUT,
    NODE_RAG,
    NODE_ROUTER,
    NODE_SYNTHESIS,
    ROUTE_NODES,
    GraphState,
    GraphStateDoc,
)

__all__ = [
    "APPROVAL_QUESTION",
    "BACKENDS",
    "CheckpointBackend",
    "CheckpointUnavailable",
    "DEFAULT_REPLAY_BUDGET",
    "FALLBACK_MESSAGE",
    "GRAPH_NAME",
    "GraphRuntime",
    "GraphState",
    "GraphStateDoc",
    "GraphTurnFailed",
    "InterruptPolicy",
    "NODE_ANALYSIS",
    "NODE_DATA",
    "NODE_GATE",
    "NODE_GUARDRAIL_INPUT",
    "NODE_GUARDRAIL_OUTPUT",
    "NODE_RAG",
    "NODE_ROUTER",
    "NODE_SYNTHESIS",
    "REJECTED_REASON",
    "ROUTE_NODES",
    "SENSITIVE_ROUTES",
    "TURN_DURABILITY",
    "Turn",
    "build_graph",
    "compile_graph",
    "ensure_compatible_event_loop",
    "failure_update",
    "graph_config",
    "normalize_input",
    "open_checkpointer",
    "route_by_intent",
    "run_turn",
    "thread_config",
]
