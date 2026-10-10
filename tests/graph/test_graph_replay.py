"""断点重放的用例：**从失败那一节点继续，不重头跑、不重复写**（`8.graph.md` §4 第 5 条、§7 第二/三条）。

三条性质，三种证法（都不靠「读代码确信」）：

| 性质 | 怎么证 |
|---|---|
| checkpoint 在失败后仍然存在，并记住「还没跑完哪一步」 | 注入一次故障 → 断言 `aget_state().next` 停在失败节点；`guardrail_input`/`router` **一次都不再跑** |
| 重放不重头跑 | 数「真正执行过的节点」：`run_turn` 的 `visited`；已完成的节点计数不增长 |
| 重放**不重复写** | 一个带幂等键的假写节点：执行两次，最终注册表里只有一行 |

为什么「不重复写」要单独证：`8.graph.md` §5 第三条说得很清楚 ——
**图能重放，但节点内部若不幂等，重放就会重复执行**。这是重放机制自带的代价，
而不是它的保证。所以写节点**照真实写法**用幂等键（`app/tools/write_path.py` 那套口径），
然后证明「跑两遍只建一张单」。

⚠️ 本文件与「重放=重试」的区别：重放**不是**重试。重试是同一个进程里再跑一次失败的动作；
重放是「进程可能已经死了，从 checkpoint 里把状态捞回来，接着跑」。
所以第二段刻意**换一个全新的图对象**来恢复 —— 那才是进程重启的等价物。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

import pytest
from langgraph.channels import LastValue
from langgraph.graph import END, START, StateGraph

from app.agents import data as data_mod
from app.graph import (
    NODE_DATA,
    NODE_GUARDRAIL_INPUT,
    NODE_ROUTER,
    NODE_SYNTHESIS,
    GraphRuntime,
    compile_graph,
    failure_update,
    graph_config,
    run_turn,
)
from app.graph.nodes import safe_node
from tests.graph_helpers import (
    AgentFakeLLM,
    build_test_graph,
    customer_state,
    fake_query_result,
    memory_saver,
    resolve_agent,
)

SQL = "SELECT order_id FROM orders WHERE user_id = :uid LIMIT :n"


class Boom(RuntimeError):
    """人为注入的**永久性**失败（不是 RetryableToolError）。"""


class FlakyLLM(AgentFakeLLM):
    """可控故障注入：`faults` 里登记的名字下次被调用时抛一次错，之后正常。

    为什么用**开关**而不是「第 N 次调用失败」：本文件的用例要精确控制
    「哪一次执行里哪个节点失败」——「第 N 次」在「图会重放」的世界里是个会漂移的计数
    （一次 `ainvoke` 里含多个超级步），用它写出来的用例难以自解释。
    """

    def __init__(self, **replies: Any) -> None:
        super().__init__(**replies)
        self.faults: set[str] = set()
        self._counts: dict[str, int] = {}

    def __call__(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        agent = resolve_agent(messages, kwargs)
        self._counts[agent] = self._counts.get(agent, 0) + 1
        if agent in self.faults:
            self.faults.discard(agent)  # 只抛一次
            raise Boom(f"{agent} 节点被注入了一次故障")
        return super().__call__(messages, **kwargs)

    def count_of(self, agent: str) -> int:
        return self._counts.get(agent, 0)


@pytest.fixture
def patched_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_mod, "execute_readonly_query", fake_query_result([{"order_id": 1024}]))


def _llm(**overrides: Any) -> FlakyLLM:
    replies: dict[str, Any] = {
        "router": {"route": "data", "certainty": 0.9, "reason": "问订单"},
        "data": {"sql": SQL, "params": {"n": 5}, "reason": "查订单"},
        "synthesis": {"answer": "查到 1 笔订单。", "cited": []},
        "guardrail": {"allowed": True, "reason": "ok"},
    }
    replies.update(overrides)
    return FlakyLLM(**replies)


# ── ⑧ 断点存在 + 重放不重头跑 ─────────────────────────────────────────────────


async def test_failed_node_so_far_that_checkpoint_points_at_it(patched_query: None) -> None:
    """节点失败后：**断点仍然指向它**（这就是「能重放」的全部含义）。

    这一条是重放机制的**地基**：如果失败被节点就地吞掉（本步第一版就是这么写的），
    LangGraph 会认为这一步成功完成、`next` 变空 —— 断点消失，重放无从谈起。
    所以这里只断言一件事，但它是关键路径：`next == (失败节点,)`。
    """
    llm = _llm()
    graph = build_test_graph(llm)
    llm.faults.add("data")

    with pytest.raises(Boom):
        # 直接 `ainvoke`（不走 run_turn 的兜底）：异常必须**穿透**到调用方
        await graph.ainvoke(customer_state("我的订单到哪了"), graph_config("t-breakpoint"))

    snapshot = await graph.aget_state(graph_config("t-breakpoint"))
    assert tuple(snapshot.next or ()) == (NODE_DATA,), (
        f"断点没有停在失败节点上：next={snapshot.next}（失败被就地吞掉的话这里会是空）"
    )
    assert snapshot.values.get("route") == "data", "失败之前已完成的那一步必须留在 checkpoint 里"
    assert llm.count_of("router") == 1


async def test_replay_resumes_at_the_breakpoint_without_rerunning_finished_nodes(
    patched_query: None,
) -> None:
    """重放：从断点继续，`guardrail_input` / `router` **一次都不再跑**。"""
    llm = _llm()
    graph = build_test_graph(llm)
    llm.faults.add("data")

    first = await run_turn(
        graph, customer_state("我的订单到哪了"), thread_id="t-replay", replay_budget=1
    )
    assert first.replayed == [NODE_DATA], f"应当重放 data 一次，实际 {first.replayed}"
    assert first.visited.count(NODE_GUARDRAIL_INPUT) == 1, f"输入护栏跑了多次：{first.visited}"
    assert first.visited.count(NODE_ROUTER) == 1, f"router 跑了多次：{first.visited}"
    assert first.visited.count(NODE_DATA) == 2, f"data 应当跑两次（失败 + 重放）：{first.visited}"
    assert first.answer == "查到 1 笔订单。", "重放之后这一轮应当正常答完"
    assert not first.exhausted
    assert llm.count_of("router") == 1, "重放重跑了已完成的 router"


async def test_replay_budget_exhaustion_degrades_gracefully(patched_query: None) -> None:
    """重放预算用尽：`exhausted=True`，且这一轮**仍然答得出来**（少一份材料≠答不了）。

    断言四件事：`exhausted=True`、`visited` 里 data 跑两次、`refusals` 里有一句人话、
    最终 `answer` 是**模型基于「材料缺失」写的实话**（不是 500、也不是空白）。
    """
    llm = _llm()
    graph = build_test_graph(llm)

    # 让 data **每次都失败**：把回执换成一个必然抛错的假模型
    class AlwaysBoom(FlakyLLM):
        def __call__(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
            if resolve_agent(messages, kwargs) == "data":
                raise Boom("data 永久失败")
            return super().__call__(messages, **kwargs)

    always = AlwaysBoom(
        router={"route": "data", "certainty": 0.9, "reason": "问订单"},
        synthesis={"answer": "查到 1 笔订单。", "cited": []},
        guardrail={"allowed": True, "reason": "ok"},
    )
    graph = build_test_graph(always)
    turn = await run_turn(graph, customer_state("我的订单到哪了"), thread_id="t-exhausted")

    assert turn.exhausted is True, "预算用尽必须被标记（调用方据此决定状态码/告警）"
    assert turn.visited.count(NODE_DATA) == 2, f"data 应当跑两次（首发 + 一次重放）：{turn.visited}"
    assert any("数据查询" in reason for reason in turn.state["refusals"])
    assert turn.answer == "查到 1 笔订单。", "材料缺失时仍要回答（synthesis 会说明查不到）"
    assert turn.blocked is False, "网络型节点的失败不该把整轮标成拦截"


# ── 进程重启的等价物：换一个全新的图对象 ─────────────────────────────────────


async def test_replay_survives_a_completely_new_graph_object(patched_query: None) -> None:
    """**换一个全新的图对象**（= 进程重启的等价物）也能接着跑：状态在 saver 里，不在图里。

    这条用例是「checkpoint 外置」与「checkpoint 挂在图对象上」的分水岭：
    后者在单进程测试里**完全看不出来**（同一个对象当然记得住）。
    """
    saver = memory_saver()
    llm1 = _llm()
    first_graph = compile_graph(saver, GraphRuntime(llm=llm1))
    llm1.faults.add("synthesis")
    turn1 = await run_turn(first_graph, customer_state("我的订单到哪了"), thread_id="t-restart")
    assert turn1.answer == "查到 1 笔订单。", "synthesis 失败一次后被重放修好"
    assert turn1.replayed == [NODE_SYNTHESIS]
    del first_graph  # 丢弃第一个图对象：模拟进程退出

    # 新进程：新的假模型（计数从零开始），同一份 saver。
    # 把断点拨回 `router`（= 输入护栏之后的下一步），再让 **data** 失败一次：
    # 这样第二轮能同时观察到两件事 —— 「router 之前的不再跑」与「失败的那一步被重放」。
    llm2 = _llm()
    llm2.faults.add("data")
    second_graph = compile_graph(saver, GraphRuntime(llm=llm2))
    await second_graph.aupdate_state(graph_config("t-restart"), {}, as_node=NODE_GUARDRAIL_INPUT)
    turn2 = await run_turn(second_graph, None, thread_id="t-restart")

    assert turn2.answer == "查到 1 笔订单。", "新图对象应当从 saver 里读回状态并接着跑"
    assert llm2.count_of("router") == 1, "router 跑了恰好一次（第一次执行的 data 失败后重放它）"
    assert llm2.count_of("data") == 2, "data 失败一次 + 重放一次"
    assert llm2.count_of("guardrail") == 1
    assert set(turn2.visited) == {NODE_ROUTER, NODE_DATA, NODE_SYNTHESIS, "guardrail_output"}, (
        f"应当从 router 走到输出护栏，实际 {turn2.visited}"
    )
    assert NODE_GUARDRAIL_INPUT not in turn2.visited, "重放不该重头跑（输入护栏已完成）"


# ── 幂等：重放不重复写 ────────────────────────────────────────────────────────


class FakeTicketStore:
    """一个**带幂等键**的假写路径（与 `app/tools/write_path.insert_once` 同口径）。

    刻意不 monkeypatch 数据库：这里要证的是**图的重放语义**，不是 SQL 的正确性。
    假 store 保留了唯一约束那条关键性质 —— `(user_id, idempotency_key)` 命中的第二次
    返回**同一张**工单，而不是新建一张。
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[int, str], int] = {}
        self.attempts: int = 0

    async def create_ticket(self, state: Any) -> dict[str, Any]:
        key = str(state.get("idempotency_key") or "")
        user_id = int(state.get("user_id") or 0)
        self.attempts += 1
        existing = self.rows.get((user_id, key))
        if existing is not None:
            return {"ticket_id": existing, "row_meta": {"idempotent_hit": True}}
        ticket_id = len(self.rows) + 1001
        self.rows[(user_id, key)] = ticket_id
        return {"ticket_id": ticket_id, "row_meta": {"idempotent_hit": False}}


class WriteState(TypedDict, total=False):
    """写路径的最小状态：主图状态里的兜底字段 + 幂等键。

    ⚠️ `Annotated[..., LastValue]` 一个都不能省 —— 裸注解的键不进图的
    `output_channels`，`ainvoke`/`aget_state` 的返回值里就没有它（runbook §10.1 坑 5）。
    """

    user_id: Annotated[int, LastValue]
    idempotency_key: Annotated[str, LastValue]
    ticket_id: Annotated[int, LastValue]
    row_meta: Annotated[dict[str, Any], LastValue]
    refusals: Annotated[list[str], LastValue]
    blocked: Annotated[bool, LastValue]
    guardrail_stage: Annotated[str, LastValue]
    guardrail_rules: Annotated[list[str], LastValue]
    guardrail_message: Annotated[str, LastValue]
    answer: Annotated[str, LastValue]


async def test_replay_is_idempotent_for_the_write_path() -> None:
    """`8.graph.md` §7 第三条：重放后写操作不重复（工单只建一次）。

    构造：写节点**第一次抛错**（模拟「事务已提交、节点没返回」这种真实故障），
    重放时再执行一次。两次执行都必须走幂等路径 —— 最终 store 里只有一行。

    为什么自建一张**最小图**而不是往主图上挂节点：主图里还没有写节点（ticket 属第 9 步），
    而「重放 + 幂等」这条性质必须有一个**会写东西**的节点才能被观察。
    自建图用的是同一套 `safe_node` / `run_turn` / `graph_config`，
    所以它验证的是本步的机制，而不是一个平行世界。
    """
    store = FakeTicketStore()
    calls: list[int] = []

    async def write_node(state: Any) -> dict[str, Any]:
        calls.append(len(calls) + 1)
        result = await store.create_ticket(state)
        if len(calls) == 1:
            raise Boom("写操作完成但节点未返回")
        return result

    builder = StateGraph(WriteState)
    builder.add_node("write", safe_node(write_node))
    builder.add_edge(START, "write")
    builder.add_edge("write", END)
    graph = builder.compile(checkpointer=memory_saver())

    turn = await run_turn(
        graph,
        {"user_id": 2, "idempotency_key": "chat:42:1"},
        thread_id="t-idempotent",
        replay_budget=1,
    )

    assert store.attempts == 2, "写节点确实被重放执行了两次（否则这条用例什么也没证明）"
    assert len(store.rows) == 1, "重放建了两张工单：写路径没有幂等，重放就不能用"
    assert turn.state.get("ticket_id") == 1001
    assert turn.visited == ["write", "write"], f"路径应当是「失败一次 + 重放一次」：{turn.visited}"


# ── 兜底函数本身 ─────────────────────────────────────────────────────────────


def test_failure_update_marks_critical_nodes_as_blocked() -> None:
    """兜底函数的两条分支：网络型只记 refusal，收口型必须标 blocked。

    单独测 `failure_update`（而不是每次跑整图）：它是「出错之后怎么收场」的**唯一**实现，
    值得直接钉住 —— 尤其 `guardrail_stage == "fallback"` 这个取值，
    第 11 步的埋点要靠它区分「护栏拦下的」与「系统没做到的」。
    """
    network = failure_update(NODE_DATA, Boom("x"), {"refusals": ["之前的一条"]})
    assert network["refusals"][-1].startswith("数据查询这一步没做成")
    assert "blocked" not in network, "网络型节点失败不该把整轮标成 blocked（只是少一份材料）"
    assert network["row_meta"]["failed_node"] == NODE_DATA

    critical = failure_update(NODE_ROUTER, Boom("x"), {})
    assert critical["blocked"] is True
    assert critical["guardrail_stage"] == "fallback"
    assert critical["answer"] == critical["guardrail_message"]
