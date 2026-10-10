"""图测试的公共装置：**按 agent 名返回不同回执**的假 LLM、造图、跑一轮、读 checkpoint。

与 `tests/agents_helpers.py` 的关系：那边的 `FakeLLM` 是「按调用**顺序**给回执」——
它适合单节点用例（一次调用就够）。但图会把**六个**节点串起来，一次 `ainvoke` 里
router / data / synthesis / guardrail 都会来要回执，再靠顺序数「第 4 次调用是谁」
会让用例极难读、也极脆弱（多一次修复轮就整体错位）。

所以这里按 **`agent=` 关键字**分派（`app/agents/agent_llm.generate_json` 每次都传它）。
好处不止是好读：它让「这个节点的模型契约」与「其它节点的」在测试里天然隔离 ——
改 synthesis 的 prompt 不会让 router 的用例跟着红。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.graph import GraphRuntime, compile_graph, ensure_compatible_event_loop
from app.graph.checkpointer import CheckpointBackend
from app.graph.config import InterruptPolicy

#: 真库用例（`@pytest.mark.db`）用哪个后端落 checkpoint
REAL_BACKENDS: tuple[CheckpointBackend, ...] = ("postgres", "redis")


def use_selector_event_loop() -> None:
    """把事件循环策略换成 Selector（Windows 上 psycopg 异步模式的前提）。

    必须在**创建事件循环之前**调 —— 所以这里在 import 期就执行，
    而不是放在 fixture 里（fixture 运行时 pytest-asyncio 可能已经建好循环了）。
    Linux/生产上是空操作，见 `app/graph/checkpointer.ensure_compatible_event_loop`。
    """
    ensure_compatible_event_loop()


#: import 期就把策略摆正：`from tests.graph_helpers import ...` 与 `import tests.graph_helpers`
#: 两种写法都会执行到这里。`sys.platform` 判断在 `ensure_compatible_event_loop` 内部。
use_selector_event_loop()
_ = sys  # 保留 import 以便在用例里报告平台


class AgentFakeLLM:
    """按 `agent=` 分派的假 LLM：每个 agent 一个回执（字符串或 dict）。

    用法：

        fake = AgentFakeLLM(router={"route": "data", "certainty": 0.9},
                            data={"sql": "SELECT ...", "params": {...}},
                            synthesis={"answer": "已查到", "cited": []},
                            guardrail={"allowed": True})
        graph = compile_graph(InMemorySaver(), GraphRuntime(llm=fake))

    未登记回执的 agent 会**抛断言**（而不是返回 `{}`）：静默返回空 dict 会让
    「节点拿到空回执 → 走降级分支 → 用例红得莫名其妙」，而真相是「用例少准备了一条」。

    ⚠️ 实测的一个坑（runbook §10.1 坑 6）：`synthesis` **不会**把 `agent=` 传进 kwargs
    ——`app/agents/synthesis.py` 调 `generate_json(...)` 时没写 `agent=`（第 7 步的实现如此），
    于是分派会落到「未知」。所以本类有两条识别路径：先看 `agent=`，
    取不到就**按 system prompt 的特征猜**（`_guess_agent`）。
    这不是给测试开后门：它如实反映「谁是调用者」这件事在真实现里就是靠 prompt 里的角色说明
    表达的（模型也是这么认的）。
    """

    def __init__(self, **replies: Mapping[str, Any] | str) -> None:
        self.replies: dict[str, Mapping[str, Any] | str] = dict(replies)
        self.calls: list[dict[str, Any]] = []
        self.unknown: list[str] = []

    def __call__(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        agent = resolve_agent(messages, kwargs)
        self.calls.append({"agent": agent, "messages": [dict(m) for m in messages], **kwargs})
        if agent not in self.replies:
            self.unknown.append(agent)
            raise AssertionError(
                f"假 LLM 没有为 agent={agent!r} 准备回执（已准备：{sorted(self.replies)}）。"
                "请在该节点的用例里补一条 —— 这通常意味着用例没意识到这个节点会被调用。"
            )
        return self.replies[agent]

    @property
    def agents(self) -> list[str]:
        """被调用过的 agent 名（按顺序；断言「哪些节点真的跑了」用）。"""
        return [str(call["agent"]) for call in self.calls]

    def prompt_of(self, agent: str) -> str:
        """某个 agent 收到的**全部消息文本**（断言 prompt 内容用）。"""
        for call in reversed(self.calls):
            if call["agent"] == agent:
                return "\n".join(str(m.get("content", "")) for m in call["messages"])
        raise AssertionError(f"agent={agent!r} 一次都没被调用过")


#: system prompt 里的特征词 → agent 名（顺序即优先级：越靠前的越先匹配）。
#:
#: 为什么要靠特征词兜底：`synthesis` 的 `generate_json` 调用没传 `agent=`（第 7 步的实现），
#: 所以 kwargs 里分不出它是谁。特征词取自各节点 system prompt 里**唯一**的那句角色说明。
_AGENT_MARKERS: tuple[tuple[str, str], ...] = (
    ("意图分流器", "router"),
    ("SQL 生成器", "data"),
    ("回答合规审查员", "guardrail"),
    ("客服助手", "synthesis"),
    ("内部业务助手", "synthesis"),
    ("财务口径助手", "synthesis"),
    ("数据分析师", "analysis"),
)


def _guess_agent(messages: Sequence[Mapping[str, Any]]) -> str:
    """从 prompt 的角色说明里认出调用者（取不到就返回 `unknown`，让断言报出来）。"""
    blob = "\n".join(str(message.get("content", "")) for message in messages)
    for marker, agent in _AGENT_MARKERS:
        if marker in blob:
            return agent
    return "unknown"


def resolve_agent(messages: Sequence[Mapping[str, Any]], kwargs: Mapping[str, Any]) -> str:
    """统一的「这次调用是谁发的」判定：先看 `agent=`，再看 prompt 里的角色说明。

    收成一个公开函数，是因为**假 LLM 的子类**（例如重放用例里的故障注入版）也要用同一条
    判定 —— 各写一份的结果是「主类的计数与子类的计数对不上」，而那种错会让你
    去怀疑被重放的节点，而不是怀疑探针。
    """
    explicit = str(kwargs.get("agent") or "")
    return explicit or _guess_agent(messages)


def memory_saver() -> InMemorySaver:
    """内存 checkpointer（**不是**「假后端」：接口与 Postgres/Redis 完全一致）。"""
    return InMemorySaver()


def build_test_graph(
    llm: Any = None,
    *,
    search: Any = None,
    saver: Any = None,
    interrupts: InterruptPolicy | None = None,
    **runtime_kwargs: Any,
) -> Any:
    """造一张**离线可跑**的图：注入假模型 / 假检索 / 内存 checkpointer。"""
    runtime = GraphRuntime(
        llm=llm,
        search=search,
        interrupts=interrupts or InterruptPolicy(),
        **runtime_kwargs,
    )
    return compile_graph(saver or memory_saver(), runtime)


def customer_state(text: str, *, user_id: int = 2, role: str = "customer") -> dict[str, Any]:
    """造入口 state（与第 9 步 API 会写进去的那三个键一致）。"""
    from app.agents.state import inject_identity

    return {"messages": [{"role": "user", "content": text}], **inject_identity(user_id, role)}


def roles(state: Mapping[str, Any]) -> list[str]:
    """把 `messages` 里的角色抽出来（断言历史里到底追加了什么）。

    ⚠️ 必须归一之后再读：从 checkpoint 读出来的 `messages` 是 `HumanMessage` / `AIMessage`
    **对象**（`add_messages` 的产物），不是 dict —— 直接 `.get("role")` 会
    `AttributeError: 'HumanMessage' object has no attribute 'get'`。
    这正是生产代码里 `nodes.normalize_input` 要解决的问题（runbook §10.1 坑 2），
    测试辅助函数当然也得过同一道闸。
    """
    return [str(m.get("role")) for m in _as_dicts(state)]


def texts(state: Mapping[str, Any]) -> list[str]:
    return [str(m.get("content")) for m in _as_dicts(state)]


def _as_dicts(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    from app.graph.state import to_openai_messages

    return to_openai_messages(state.get("messages"))


def fake_rows(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    return [dict(zip(columns, row, strict=True)) for row in rows]


def fake_query_result(rows: list[dict[str, Any]], *, audit_id: str = "tc-graph", **meta: Any) -> Any:
    """造 `execute_readonly_query` 的返回值（rows, meta 二元组）。"""
    return lambda sql, params=None, **kwargs: (
        rows,
        {"row_count": len(rows), "truncated": False, "audit_id": audit_id, "tables": ["orders"], **meta},
    )


__all__ = [
    "REAL_BACKENDS",
    "AgentFakeLLM",
    "build_test_graph",
    "customer_state",
    "fake_query_result",
    "fake_rows",
    "memory_saver",
    "resolve_agent",
    "roles",
    "texts",
    "use_selector_event_loop",
]
