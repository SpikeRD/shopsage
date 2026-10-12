"""图与 API 的接线：**缓存真的接进了那条链路吗**（离线、假模型、假检索、内存缓存）。

`tests/cache/` 里的用例验的是缓存**自己**对不对（阈值、TTL、失效）。
这一组验的是另一个问题：**它有没有被接上**，以及接上之后行为是不是我们想要的。

| 要验的 | 靠什么发现错了 |
|---|---|
| `rag_node` 在检索**之前**查缓存 | 计数假检索被调了几次（命中时应当是 0） |
| 命中时 `synthesis` **不打模型** | `AgentFakeLLM.calls` 里没有 `synthesis` |
| 强时效问题**不进**缓存 | 订单类问题的第二次请求仍然有 `data` 调用 |
| 未命中时答案被**写回** | 第二次请求命中 |
| 多轮上下文不串味 | 同一句追问换个上下文后不命中 |

⚠️ 缓存是**注入点**（`GraphRuntime.cache`），默认 `None` ——
所以这一组用例都必须显式注入，而那正是「第 10 步的行为逐行不变」的证据。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.cache.api import build_cache
from app.cache.backend import CacheKeySpace, MemoryKeyValue
from app.cache.settings import CacheBackendSettings, CacheConfig, CacheSettings
from app.cache.stores import MemoryEntryStore
from app.graph import GraphRuntime, compile_graph
from tests.cache.conftest import SemanticSpace, make_cache, messages
from tests.graph_helpers import AgentFakeLLM, memory_saver

DOCS = [
    {"text": "自签收之日起 7 天内、商品完好可申请无理由退货。", "source": "知识库/退货政策.md", "chunk_id": "c1", "score": 0.9},
]
ANSWER = "7 天内可无理由退货，运费由买家承担。[1]"


class CountingSearch:
    """假检索后端（记账）：计「检索被调了几次」，这是「命中跳过检索」的唯一证据。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(str(kwargs.get("query") or ""))
        return [dict(item) for item in DOCS]


def build_graph(*, space: SemanticSpace, search: CountingSearch, cache: Any, llm: AgentFakeLLM) -> Any:
    return compile_graph(
        memory_saver(),
        GraphRuntime(llm=llm, search=search, cache=cache),
    )


def rag_llm() -> AgentFakeLLM:
    return AgentFakeLLM(
        router={"route": "rag", "certainty": 0.95, "reason": "问政策"},
        synthesis={"answer": ANSWER, "cited": [1]},
        guardrail={"allowed": True, "reason": "ok"},
    )


class RoutingLLM(AgentFakeLLM):
    """按**当前问题**决定路线（`router` 只看 prompt 里 `当前问题：` 之后那一句）。

    ⚠️ 本类目前**没有被任何用例使用** —— 保留它是因为它记录了一件实测事实：
    `AgentFakeLLM(router={...})` 是固定回执，凡是「同一个问题在不同上下文里的行为不同」
    的用例都不能用它（真实 router 会因历史不同给出不同路线）。
    写多轮相关的用例时优先绕开 LLM（直接断言缓存层的账目），
    只有在必须走完整图、且问题本身决定路线时才用它。
    """

    def __call__(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        from tests.graph_helpers import resolve_agent

        if resolve_agent(messages, kwargs) == "router":
            current = ""
            for message in reversed(messages):
                content = str(message.get("content"))
                if "当前问题：" in content:
                    current = content.split("当前问题：", 1)[1].strip()
                    break
            route = "rag" if ("退货" in current or "政策" in current) else "final"
            kwargs.pop("agent", None)
            self.calls.append({"agent": "router", "messages": [dict(m) for m in messages], **kwargs})
            return {"route": route, "certainty": 0.95, "reason": "按问法分流"}
        return super().__call__(messages, **kwargs)


def set_state(text: str, *, user_id: int = 2, thread: str = "t-1", history: tuple[str, ...] = ()) -> dict[str, Any]:
    """造入口 state（第 9 步 API 会写进去的那几个键）。"""
    from app.agents.state import inject_identity

    msgs: list[dict[str, str]] = []
    for item in history:
        msgs.append({"role": "user", "content": item})
        msgs.append({"role": "assistant", "content": "（上一轮的回答）"})
    msgs.append({"role": "user", "content": text})
    return {
        "messages": msgs,
        "thread_id": thread,
        **inject_identity(user_id, "customer"),
    }


@pytest.fixture
def space() -> SemanticSpace:
    return SemanticSpace()


@pytest.fixture
def search() -> CountingSearch:
    return CountingSearch()


@pytest.fixture
def cache(space: SemanticSpace):
    """**全离线**的缓存（内存条目表 + 内存键值 + 内存版本表 + 可控 embedding）。"""
    space.place("怎么退货")
    instance = build_cache(
        config=CacheConfig(cache=CacheSettings(), backend=CacheBackendSettings(redis_url="")),
        embedder=space,
        store=MemoryEntryStore(),
        key_value=MemoryKeyValue(),
    )
    return instance


# ── 未命中 → 生成 → 写缓存 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_turn_misses_and_writes_the_answer(space, search, cache) -> None:
    """第一次问：检索发生、模型被调、答案被写进缓存。"""
    graph = build_graph(space=space, search=search, cache=cache, llm=rag_llm())
    state = set_state("怎么退货")
    out = await graph.ainvoke(state, {"configurable": {"thread_id": "t-1"}})

    assert out["answer"] == ANSWER
    assert len(search.calls) == 1
    assert cache.semantic.stats["miss"] >= 1
    assert cache.semantic.stats["written"] == 1
    assert cache.store.count() == 1


@pytest.mark.asyncio
async def test_second_turn_hits_and_skips_retrieval_and_model(space, search, cache) -> None:
    """**本步最核心的一条**：同一个问题问第二次，检索 0 次、`synthesis` 0 次。

    三个断言缺一不可：
      * `search.calls` 长度不变 → 缓存闸在检索之前；
      * 假模型里没有 `synthesis` → 「命中就不打模型」（E1 的落点）；
      * `answer` 与第一次一致 → 缓存真的被用上了（而不是「碰巧又生成了一遍」）。
    """
    graph = build_graph(space=space, search=search, cache=cache, llm=rag_llm())
    first = await graph.ainvoke(set_state("怎么退货"), {"configurable": {"thread_id": "t-1"}})
    assert first["answer"] == ANSWER
    assert len(search.calls) == 1

    # ⚠️ 第二轮**换一个检索后端对象**：`search` 会进版本指纹
    # （`embedder_key=str(search)`），换个对象就等于「检索配置变了」——
    # 于是缓存正确地不命中。要断言「命中时不检索」，就必须让两轮的检索配置一致
    # （这里用 `search` 的重置版：对象身份不同、内容相同，所以换个写法：
    # 直接看**第二轮有没有新增检索调用**。为了那件事成立，两轮用同一个 searcher）。
    search.calls.clear()
    llm = rag_llm()
    graph2 = compile_graph(memory_saver(), GraphRuntime(llm=llm, search=search, cache=cache))
    second = await graph2.ainvoke(
        set_state("怎么退货", thread="t-2"), {"configurable": {"thread_id": "t-2"}}
    )

    assert second["answer"] == ANSWER
    assert search.calls == [], "命中时不该再检索"
    assert "synthesis" not in llm.agents, "命中时不该打模型"
    assert second["cache_info"]["outcome"] == "hit"
    assert second["cache_info"]["cached_answer_used"] is True


@pytest.mark.asyncio
async def test_similar_question_hits_through_the_graph(space, search, cache) -> None:
    """语义命中（不是完全同问）在**图里**也成立。"""
    graph = build_graph(space=space, search=search, cache=cache, llm=rag_llm())
    await graph.ainvoke(set_state("怎么退货"), {"configurable": {"thread_id": "t-1"}})
    searches = len(search.calls)

    space.place("退货流程是什么", base="怎么退货", similarity=0.97)
    llm = rag_llm()
    graph2 = compile_graph(memory_saver(), GraphRuntime(llm=llm, search=search, cache=cache))
    out = await graph2.ainvoke(
        set_state("退货流程是什么", thread="t-2"), {"configurable": {"thread_id": "t-2"}}
    )
    assert out["answer"] == ANSWER
    assert len(search.calls) == searches
    assert "synthesis" not in llm.agents


# ── 隔离与强时效（在图上再验一遍）──────────────────────────────────────────


@pytest.mark.asyncio
async def test_another_user_does_not_hit(space, search, cache) -> None:
    """换个 user_id 就不命中 —— 缓存**必须**按身份隔离（A2 越权）。"""
    graph = build_graph(space=space, search=search, cache=cache, llm=rag_llm())
    await graph.ainvoke(set_state("怎么退货", user_id=2), {"configurable": {"thread_id": "t-1"}})

    llm = rag_llm()
    graph2 = compile_graph(memory_saver(), GraphRuntime(llm=llm, search=search, cache=cache))
    await graph2.ainvoke(
        set_state("怎么退货", user_id=3, thread="t-2"), {"configurable": {"thread_id": "t-2"}}
    )
    assert "synthesis" in llm.agents, "换了用户就该重新生成"


@pytest.mark.asyncio
async def test_multi_turn_does_not_cross_hit(space, search, cache) -> None:
    """同一句问题在不同上下文里**不命中**（缓存键含整串用户消息）。

    ## 为什么这条用例不复用 `rag_llm()`

    `AgentFakeLLM(router={...})` 是**一个固定回执**：它无论问题是什么都回答 `rag`。
    这条用例的判定放在**缓存层**（同一句话、不同上下文 → 不同缓存键），
    所以最干净的写法是绕开 LLM 的路由不确定性，直接问两次缓存：
      * 上下文 A：[「怎么退货」]
      * 上下文 B：[「你们几点上班」,「怎么退货」]
    两者的**最后一句完全一样**，但拼出来的问题不同 —— 这正是要钉住的性质。

    多轮上下文本身的语义（为什么必须拼上下文）由
    `tests/cache/test_cache_core.py::test_follow_up_question_is_a_different_key_than_the_standalone_one`
    在纯逻辑层逐字钉住；这条用例只负责「图上真的把整串消息给了缓存」。
    """
    graph = build_graph(space=space, search=search, cache=cache, llm=rag_llm())
    first = await graph.ainvoke(set_state("怎么退货"), {"configurable": {"thread_id": "t-1"}})
    assert first["answer"] == ANSWER
    assert first["cache_info"]["outcome"] == "miss"

    # 同一句话、多了一轮历史：图上这次应当**未命中**（问题串变了）
    llm = rag_llm()
    graph2 = compile_graph(memory_saver(), GraphRuntime(llm=llm, search=search, cache=cache))
    second = await graph2.ainvoke(
        set_state("怎么退货", thread="t-2", history=("你们几点上班",)),
        {"configurable": {"thread_id": "t-2"}},
    )
    assert second["cache_info"]["outcome"] == "miss", "带上了下文就不该命中上一轮的缓存"
    assert "synthesis" in llm.agents, "未命中就该正常走生成"

    # 反过来：上下文完全相同的第二次问**要**命中（同一串消息 → 同一个键）
    llm2 = rag_llm()
    graph3 = compile_graph(memory_saver(), GraphRuntime(llm=llm2, search=search, cache=cache))
    third = await graph3.ainvoke(
        set_state("怎么退货", thread="t-3", history=("你们几点上班",)),
        {"configurable": {"thread_id": "t-3"}},
    )
    assert third["cache_info"]["outcome"] == "hit"
    assert "synthesis" not in llm2.agents
    assert third["answer"] == ANSWER


# ── 强时效（F1）在图上 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_time_sensitive_question_is_never_served_from_cache(space, cache) -> None:
    """订单/库存/物流类问题**每次都重新查库**（`no_cache`，F1）。

    这条走 `data` 分支（强时效里最常见的那一类），所以这里用「假 SQL 执行器」计数。
    """
    from app.agents import data as data_mod

    calls: list[str] = []

    def fake_query(sql, params=None, **kwargs):  # noqa: ANN001, ANN202
        calls.append(sql)
        return [{"order_id": 1024, "status": "shipped"}], {"row_count": 1, "truncated": False, "audit_id": "a1"}

    original = data_mod.execute_readonly_query
    data_mod.execute_readonly_query = fake_query
    try:
        llm = AgentFakeLLM(
            router={"route": "data", "certainty": 0.95, "reason": "问订单"},
            data={"sql": "SELECT order_id, status FROM orders WHERE user_id = :uid", "params": {"uid": 2}, "reason": "查"},
            synthesis={"answer": "您的订单已发货。[1]", "cited": []},
            guardrail={"allowed": True, "reason": "ok"},
        )
        graph = compile_graph(
            memory_saver(), GraphRuntime(llm=llm, search=lambda **_: [], cache=cache)
        )
        for thread in ("t-1", "t-2", "t-3"):
            await graph.ainvoke(
                set_state("我的订单 1024 到哪了", thread=thread),
                {"configurable": {"thread_id": thread}},
            )
    finally:
        data_mod.execute_readonly_query = original

    assert len(calls) == 3, "强时效问题每次都必须真查库（不能被缓存拦住）"
    assert cache.store.count() == 0, "强时效问题的答案不该进缓存"


@pytest.mark.asyncio
async def test_bypass_is_reported_in_cache_info(space, search, cache) -> None:
    """跳过缓存这件事要在 state 里留痕（否则「为什么它没被缓存」查不出来）。"""
    llm = rag_llm()
    llm.replies["router"] = {"route": "rav", "certainty": 0.9}  # 未知路线 → 保守兜底到 rag
    graph = compile_graph(memory_saver(), GraphRuntime(llm=llm, search=search, cache=cache))
    # 用一句强时效的话走 rag（图不会真的去 data，但缓存闸先看问法）
    out = await graph.ainvoke(
        set_state("还有货吗", thread="t-9"), {"configurable": {"thread_id": "t-9"}}
    )
    assert out["cache_info"]["outcome"] == "bypassed"
    assert cache.store.count() == 0


# ── 缓存为 None 时行为不变（第 10 步的回归面）──────────────────────────────


@pytest.mark.asyncio
async def test_without_cache_the_graph_behaves_as_before(space, search) -> None:
    """`cache=None`（默认）时：照常检索、照常生成、没有任何缓存副作用。"""
    llm = rag_llm()
    graph = compile_graph(memory_saver(), GraphRuntime(llm=llm, search=search))
    out = await graph.ainvoke(
        set_state("怎么退货", thread="t-1"), {"configurable": {"thread_id": "t-1"}}
    )
    assert out["answer"] == ANSWER
    assert len(search.calls) == 1
    assert "synthesis" in llm.agents
    assert "cache_info" not in out or not out.get("cache_info")


# ── API 接线：`create_app(cache=...)` ──────────────────────────────────────


def test_create_app_wires_the_injected_cache_into_the_runtime(space, cache) -> None:
    """`create_app(cache=...)` 把它装进 `GraphRuntime.cache`（**唯一一处**换缓存的地方）。"""
    from app.api.main import create_app
    from tests.api.conftest import tiny_limiter

    app = create_app(
        runtime=GraphRuntime(llm=rag_llm(), search=lambda **_: []),
        cache=cache,
        rate_limiter=tiny_limiter(),
        probe_timeout_value=0.05,
    )
    with __import__("fastapi").testclient.TestClient(app):
        assert app.state.cache is cache
        assert app.state.graph_runtime.cache is cache


def test_create_app_reports_the_cache_in_health(space, cache) -> None:
    """`/health` 里有 cache 组件，且注入的缓存状态是 `ok`（内存 + 已装配）。"""
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from tests.api.conftest import tiny_limiter

    app = create_app(
        runtime=GraphRuntime(llm=rag_llm(), search=lambda **_: []),
        cache=cache,
        rate_limiter=tiny_limiter(),
        probe_timeout_value=0.05,
    )
    with TestClient(app) as client:
        body = client.get("/health").json()
    names = {component["name"] for component in body["components"]}
    assert "cache" in names
    entry = next(c for c in body["components"] if c["name"] == "cache")
    assert entry["status"] in ("ok", "degraded")
    assert "条目" in entry["detail"]


def test_health_reports_cache_skipped_when_not_wired() -> None:
    """没装配缓存时是 `skipped`（**不是 `down`**）—— 「没启用」与「坏了」是两件事。"""
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from tests.api.conftest import tiny_limiter

    app = create_app(
        runtime=GraphRuntime(llm=rag_llm(), search=lambda **_: []),
        rate_limiter=tiny_limiter(),
        probe_timeout_value=0.05,
    )
    with TestClient(app) as client:
        app.state.cache = None  # 模拟「没走 lifespan 装配」这条路径
        body = client.get("/health").json()
    entry = next(c for c in body["components"] if c["name"] == "cache")
    assert entry["status"] == "skipped"


def test_health_reports_cache_down_for_bad_config() -> None:
    """配置写反（阈值 < 兜底线）算 `down` 而不是 `degraded`。

    它会让缓存**要么永不命中**（白花成本）、要么答错问题 —— 两者都比「慢一点」严重，
    所以它必须比 `degraded` 更醒目（见 `app/api/health.cache_component` 的说明）。
    """
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.cache.api import build_cache
    from app.cache.settings import CacheBackendSettings, CacheConfig, CacheSettings
    from tests.api.conftest import tiny_limiter

    broken = build_cache(
        config=CacheConfig(
            cache=CacheSettings(semantic_threshold=0.5, semantic_floor=0.9),
            backend=CacheBackendSettings(redis_url=""),
        )
    )
    app = create_app(
        runtime=GraphRuntime(llm=rag_llm(), search=lambda **_: []),
        cache=broken,
        rate_limiter=tiny_limiter(),
        probe_timeout_value=0.05,
    )
    with TestClient(app) as client:
        body = client.get("/health").json()
    entry = next(c for c in body["components"] if c["name"] == "cache")
    assert entry["status"] == "down"
    assert "阈值" in entry["detail"]
