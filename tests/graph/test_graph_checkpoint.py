"""checkpointer 的用例：三种后端、**thread 隔离**、以及「状态真的离开了进程」。

分三层（与 `8.graph.md` §8 的第三、四条验收对应）：

| 层 | 用什么 | 证什么 |
|---|---|---|
| 内存（默认） | `InMemorySaver` | thread 隔离、历史可读、`thread_id` 校验 —— **离线全跑** |
| 真库 `@pytest.mark.db` | `AsyncPostgresSaver` | 进程重启不丢状态：落库 → 新连接 → 读回 |
| Redis `@pytest.mark.db` | `AsyncRedisSaver` | 同上，但**需要 RedisJSON + RediSearch**；缺模块时 skip 并打印原因 |

为什么 Redis 要单独一条而不是「和 Postgres 合起来参数化」：本机 dev 的 redis 镜像
**没有**那两个模块（实测 `RedisSearchError: unknown command 'FT.INFO'`），
所以它大概率是 skip。把「为什么 skip」写成可读的一行，比让整条用例消失更有价值 ——
下一代人（或三个月后的自己）才知道该装什么。
"""

from __future__ import annotations

import os

import pytest

from app.agents import data as data_mod
from app.graph import (
    NODE_DATA,
    NODE_GUARDRAIL_INPUT,
    NODE_ROUTER,
    NODE_SYNTHESIS,
    GraphRuntime,
    compile_graph,
    graph_config,
    open_checkpointer,
    run_turn,
    thread_config,
)
from app.graph.checkpointer import CheckpointUnavailable, ensure_compatible_event_loop
from tests.graph_helpers import (
    AgentFakeLLM,
    build_test_graph,
    customer_state,
    fake_query_result,
    memory_saver,
)

SQL = "SELECT order_id FROM orders WHERE user_id = :uid LIMIT :n"
DB_MARKER = pytest.mark.db


def _llm(answer: str = "查到 1 笔订单。") -> AgentFakeLLM:
    return AgentFakeLLM(
        router={"route": "data", "certainty": 0.9, "reason": "问订单"},
        data={"sql": SQL, "params": {"n": 5}, "reason": "查订单"},
        synthesis={"answer": answer, "cited": []},
        guardrail={"allowed": True, "reason": "ok"},
    )


# ── 内存层：thread 隔离与状态可读（§7 第四条）───────────────────────────────────


async def test_threads_are_isolated_from_each_other(monkeypatch: pytest.MonkeyPatch) -> None:
    """`8.graph.md` §7 第四条：两个 `thread_id` 的 state 互不干扰。

    构造两个**不同用户**的会话（2 与 7），各自跑一轮，然后交叉核对：
    各自的 `user_id` / `answer` / 历史都只属于自己的 thread。
    这正是「`thread_id` = 会话 id」这条设计在运行期的可观察形式（§4 第 3 条）。
    """
    monkeypatch.setattr(data_mod, "execute_readonly_query", fake_query_result([{"order_id": 1024}]))
    saver = memory_saver()
    graph = compile_graph(saver, GraphRuntime(llm=_llm()))

    first = await run_turn(graph, customer_state("我的订单到哪了", user_id=2), thread_id="thread-A")
    # 第二个 thread 用**另一个**假模型（回答不同，便于区分状态是不是串了）
    other_graph = compile_graph(saver, GraphRuntime(llm=_llm("查到 3 笔订单。")))
    second = await run_turn(
        other_graph, customer_state("我的订单到哪了", user_id=7), thread_id="thread-B"
    )

    assert first.state["user_id"] == 2 and second.state["user_id"] == 7
    assert first.answer == "查到 1 笔订单。"
    assert second.answer == "查到 3 笔订单。"
    assert first.state["messages"] != second.state["messages"] or first.state["user_id"] != 7

    # 再各跑一轮：状态各自累积，互不串台
    again_a = await run_turn(
        graph,
        {**first.state, "messages": [*first.state["messages"], {"role": "user", "content": "再问一次"}]},
        thread_id="thread-A",
    )
    snapshot_b = await other_graph.aget_state(graph_config("thread-B"))
    assert again_a.state["user_id"] == 2
    assert snapshot_b.values["user_id"] == 7, "A 的第二轮污染了 B 的状态"


async def test_thread_state_history_is_readable_and_ordered(monkeypatch: pytest.MonkeyPatch) -> None:
    """checkpoint 历史可读，且**新→旧**给出（重放与排障都靠它）。"""
    monkeypatch.setattr(data_mod, "execute_readonly_query", fake_query_result([{"order_id": 1}]))
    graph = build_test_graph(_llm())
    await run_turn(graph, customer_state("我的订单到哪了"), thread_id="t-history")

    history = [
        snapshot async for snapshot in graph.aget_state_history(graph_config("t-history"))
    ]
    assert len(history) >= 4, f"每一步都该有 checkpoint，实际只有 {len(history)} 条"
    assert history[0].metadata["step"] >= history[-1].metadata["step"]
    assert dict(history[0].values)["answer"] == "查到 1 笔订单。"


async def test_memory_checkpointer_does_not_share_state_between_graph_objects() -> None:
    """**反面证据**：内存 saver 是进程内的 —— 两个 saver 之间互不可见。

    这条用例的价值在于把「为什么要外置」变成可执行的说明：
    进程重启（= 换一个 saver）之后，内存模式什么都读不回来。
    """
    graph_a = compile_graph(memory_saver(), GraphRuntime(llm=_llm()))
    graph_b = compile_graph(memory_saver(), GraphRuntime(llm=_llm()))

    await run_turn(graph_a, customer_state("我的订单到哪了"), thread_id="same-thread")
    other = await graph_b.aget_state(graph_config("same-thread"))
    assert other.values == {} or not other.values.get("answer"), (
        "两个内存 saver 不该互相看见（否则这条用例的前提就不成立）"
    )


def test_thread_config_rejects_an_empty_thread_id() -> None:
    """空 `thread_id` 直接报错：它会**静默**把所有会话合并成一份状态。

    `graph/checkpointer.py` 里这条校验是刻意放在门面上的：
    空值在实际系统里最可能的来源是「入口忘了传」（例如 API 里那行 config 拼错了），
    而那时的症状是「不同用户的回答互相串」，排查方向会先落到权限上。
    """
    for bad in ("", "   ", None):
        with pytest.raises(ValueError, match="thread_id"):
            thread_config(bad)  # type: ignore[arg-type]
    assert thread_config("ok") == {"configurable": {"thread_id": "ok"}}


def test_unknown_backend_fails_with_an_actionable_message() -> None:
    """未知后端名 → `CheckpointUnavailable`，且报文里列出合法取值。"""
    import asyncio

    async def open_it() -> None:
        async with open_checkpointer("mysql"):  # type: ignore[arg-type]
            pass

    with pytest.raises(CheckpointUnavailable, match="memory"):
        asyncio.run(open_it())


# ── 真库层：进程重启不丢状态（§8 第三条）───────────────────────────────────────


@pytest.mark.parametrize("backend", ["postgres", "redis"])
@DB_MARKER
async def test_checkpoint_survives_a_new_graph_and_connection(backend: str) -> None:
    """checkpoint 落到**真后端**之后：新连接 + 新图对象也能读回状态。

    这就是 `8.graph.md` §8 第三条「进程重启不丢状态」的最小可验证形式 ——
    不能真的重启进程，但可以让**连接**与**图对象**全部换新，
    而 saver 里的数据来自数据库而不是上一个对象的内存。
    """
    if backend == "redis" and not os.environ.get("SHOPSAGE_TEST_REDIS_CHECKPOINTER"):
        pytest.skip(
            "Redis checkpointer 需要 RedisJSON + RediSearch 模块；本机 dev 的 redis 镜像没有它们。"
            "改用 redis/redis-stack-server 之后再跑（见 runbook §4.3 / §10.1 坑 5）"
        )
    ensure_compatible_event_loop()

    thread = f"t-real-{backend}"
    state = customer_state("我的订单到哪了")
    state["messages"] = [{"role": "user", "content": "我的订单到哪了"}, {"role": "assistant", "content": "先前的回答"}]

    async with open_checkpointer(backend) as saver:  # type: ignore[arg-type]
        graph = compile_graph(saver, GraphRuntime(llm=_llm("落库的回答。")))
        # 只跑「输入护栏 → router」就停：不需要模型/数据库查询，就足以留下 checkpoint
        await graph.aupdate_state(graph_config(thread), state, as_node=NODE_GUARDRAIL_INPUT)

    # 换一个全新的 saver（= 新进程会做的事）
    async with open_checkpointer(backend) as fresh:  # type: ignore[arg-type]
        graph2 = compile_graph(fresh, GraphRuntime(llm=_llm()))
        snapshot = await graph2.aget_state(graph_config(thread))
        assert dict(snapshot.values).get("user_id") == 2, f"{backend} 里读不回状态"
        assert tuple(snapshot.next or ()) == (NODE_ROUTER,), (
            f"断点位置也应当落库：next={snapshot.next}"
        )


@DB_MARKER
async def test_postgres_checkpointer_replays_after_a_crash_like_stop() -> None:
    """落库之后换 saver 继续跑：**已完成的节点不再执行**（真后端上的重放）。

    与内存版（`test_graph_replay.py`）的区别只有一处，但很关键：
    这里的状态确实经过了 Postgres 的序列化（`checkpoint_blobs` 里的 msgpack），
    所以「状态里有什么类型能落盘」这件事也被一并验证了
    （`frozenset` / `Decimal` / `datetime` 都在 `GraphState` 里出现过）。
    """
    ensure_compatible_event_loop()
    thread = "t-pg-replay"
    saver_ctx = open_checkpointer("postgres")

    async with saver_ctx as saver:
        llm = _llm()
        graph = compile_graph(saver, GraphRuntime(llm=llm))
        # 把状态与断点写进库：只跑输入护栏，下一步停在 router
        await graph.aupdate_state(
            graph_config(thread), customer_state("我的订单到哪了"), as_node=NODE_GUARDRAIL_INPUT
        )
        assert tuple((await graph.aget_state(graph_config(thread))).next or ()) == (NODE_ROUTER,)

    import app.agents.data as data_mod_local

    original = data_mod_local.execute_readonly_query
    data_mod_local.execute_readonly_query = fake_query_result([{"order_id": 1024}])
    try:
        async with open_checkpointer("postgres") as fresh:
            llm2 = _llm()
            graph2 = compile_graph(fresh, GraphRuntime(llm=llm2))
            turn = await run_turn(graph2, None, thread_id=thread)
    finally:
        data_mod_local.execute_readonly_query = original

    assert turn.answer == "查到 1 笔订单。"
    assert NODE_GUARDRAIL_INPUT not in turn.visited, "输入护栏已在库里标记完成，不该重跑"
    assert turn.visited == [NODE_ROUTER, NODE_DATA, NODE_SYNTHESIS, "guardrail_output"], (
        f"应当只从断点往后跑，实际 {turn.visited}"
    )
