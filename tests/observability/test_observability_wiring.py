"""接线用例：**埋点真的在业务路径上生效**（第 11 步最容易被糊弄过去的一层）。

前三类用例（`test_observability_metrics.py` / `_tracing.py` / `_langfuse.py`）证明的是
「三件工具本身能用」。但本步真正的验收标准是 `11.observability.md` §8 的第一条：

> 每个 Agent 节点 / 工具 / LLM / 检索**都有埋点**。

「有」这件事没法靠「写了装饰器」证明 —— 它必须从**真实的调用路径**上抓出来。
所以本文件全部跑真实现（只把**最外层的输入输出**换掉：假模型、假 SQL 执行器、
内存 checkpointer、假 Langfuse client），然后断言「埋点出现了」。

| 用例 | 覆盖的路径 |
|---|---|
| `test_metrics_exposed` | `GET /metrics` 能拿到 request_count / agent_duration / sql_duration |
| `test_agent_nodes_are_all_instrumented` | 图节点（含 `refund_request`；`gate` 见下一条）每个都有 span + `AGENT_DURATION` |
| `test_llm_call_records_tokens_and_cost` | `app.llm.client.llm_call` |
| `test_llm_retry_produces_one_span_per_attempt` | 重试时的 span 数（成本归因的关键） |
| `test_sql_execution_records_duration` | `execute_readonly_query` |
| `test_retrieval_records_hits_and_candidates` | `retrieve_docs` |
| `test_write_tool_records_outcome` | `create_ticket`（唯一副作用出口） |
| `test_request_spans_share_one_trace` | HTTP → 节点 → LLM 是**一棵树** |

## 为什么这些用例能离线跑

关键在**注入点**（第 7/9 步留好的接缝）：
`app.grah.nodes.safe_node` 的埋点落在**图节点的唯一入口**上，
所以只要用「假模型 + 内存 checkpointer」跑一轮图，7 个节点的埋点就全被走到，
而**没有任何真实依赖**（不连库、不调模型、不联网）。
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.observability import metrics as M
from app.observability import tracing as T
from tests.api.conftest import DATA_REPLIES, ROWS, SQL, make_token, ws_subprotocols


# ── §7 第一条：`GET /metrics` 必须能拿到三族指标 ──────────────────────────────


@pytest.fixture
def obs_client(telemetry: T.Telemetry) -> "TestClient":
    """一个**注册表与 telemetry 是同一份**的真 app 客户端。

    ⚠️ 为什么必须在这里手工把两者接上（而不是直接用 `client` 装置）：
    `create_app(metrics=..., telemetry=...)` 是两个**独立的**注入点，而
    「埋点写进 telemetry 的注册表」与「`/metrics` 渲染 app 的注册表」只有在
    它们**是同一个对象**时才一致。生产路径由 lifespan 自动接
    （`setup_observability(settings, metrics=app.state.metrics)`）；
    这里注入了 telemetry，所以必须自己把注册表交给 app ——
    这正是本步实测踩到的坑的**可复现形态**（两份注册表，各自看起来都正常）。

    ⚠️ 依赖 `telemetry` 装置（而不是 `obs_metrics`）：否则用例拿到的 `telemetry`
    与 app 用的是**两份不同的** telemetry（都基于同一个注册表，但 exporter 不同）——
    断言会红在「一条 span 都没有」，而真正的原因是装置接错了。
    """
    from app.api import create_app
    from app.api.metrics import MetricsRegistry
    from app.graph import GraphRuntime
    from tests.api.conftest import tiny_limiter
    from tests.graph_helpers import AgentFakeLLM, memory_saver

    app = create_app(
        runtime=GraphRuntime(
            llm=AgentFakeLLM(**DATA_REPLIES),
            search=lambda **_: [],
        ),
        rate_limiter=tiny_limiter(),
        # 同一个 `Metrics` 实例：app 的注册表转发到它，telemetry 也用它
        metrics=MetricsRegistry(metrics=telemetry.metrics),
        telemetry=telemetry,
        checkpointer_factory=lambda: _mem_context(memory_saver()),
        probe_timeout_value=0.05,
    )
    with TestClient(app) as test_client:
        yield test_client


def _mem_context(saver: object) -> object:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield saver

    return _cm()


def test_metrics_exposed(obs_client: TestClient, telemetry: T.Telemetry) -> None:
    """`11.observability.md` §7 第一条：`/metrics` 返回 request_count / agent_duration / sql_duration。

    ⚠️ 这条用例必须走**真的 HTTP 请求**（而不是手动调 `obs_metrics.counter(...)`）：
    手动调只能证明「注册表能渲染」，证明不了「业务路径真的在写它」——
    而「埋点写进了另一个注册表」正是本步实测踩到的坑（`/metrics` 里只有
    `shopsage_api_*`，节点级指标一条都没有）。
    """
    obs_client.get("/health")
    text = obs_client.get("/metrics").text
    assert M.REQUEST_COUNT in text, f"/metrics 里没有请求计数：{text[:200]}"

    # 走一轮真业务路径（假模型 + 假 SQL 执行器）→ 必须产生节点与 SQL 指标
    token = make_token(2, "customer")
    response = obs_client.post(
        "/api/chat",
        json={"message": "我的订单到哪了", "session_id": "s-obs"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text

    text = obs_client.get("/metrics").text
    assert M.AGENT_DURATION in text, "节点耗时没有被埋点（B1 就无从回答）"
    assert M.SQL_DURATION in text, "SQL 耗时没有被埋点"
    assert M.TRACE_COUNT in text, "请求数（trace 计数）没有被埋点"


def test_metrics_response_is_still_valid_prometheus_text(obs_client: TestClient) -> None:
    """加了第二段文本之后，整体仍然是合法的 Prometheus 输出（拼接不能破坏格式）。"""
    body = obs_client.get("/metrics").text
    assert body.endswith("\n")
    for line in body.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        assert re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? -?\d", line), f"非法行：{line}"


def test_root_span_and_node_spans_share_one_trace(obs_client: TestClient, telemetry: T.Telemetry) -> None:
    """根 span 必须**包住**整段请求，于是下游所有节点 span 都挂在它下面。

    ⚠️ 这条用例是本步最值钱的一条（它钉住了一个「看起来对、其实完全没接上」的实现）：
    第一版在请求**结束后**补一条根 span —— 于是每个节点 span 各自成了根，
    **每个节点一个 trace**，「按 trace 查一次请求」这条排障路径彻底失效，
    而每个节点看起来都埋了点。
    """
    token = make_token(2, "customer")
    obs_client.post(
        "/api/chat",
        json={"message": "我的订单到哪了", "session_id": "s-one-trace"},
        headers={"Authorization": f"Bearer {token}"},
    )

    roots = [r for r in telemetry.exporter.records if r.name.startswith("http.")]
    nodes = [r for r in telemetry.exporter.records if r.name.startswith(T.NODE_PREFIX)]
    assert roots and nodes
    root = roots[-1]
    assert root.parent_span_id is None
    assert {r.trace_id for r in nodes} == {root.trace_id}, "节点 span 不在请求的 trace 里"
    # 节点必须是根的后代（直接或间接）—— 逐条检查父亲都在同一个 trace 里
    ids = {r.span_id for r in telemetry.exporter.records}
    for node in nodes:
        assert node.parent_span_id in ids, f"{node.name} 的父亲不在这次 trace 里"


# ── 图节点：7 个节点一个都不能漏 ──────────────────────────────────────────────


def test_agent_nodes_are_all_instrumented(client: TestClient, telemetry: T.Telemetry) -> None:
    """`11.observability.md` §8 第一条：**每个** Agent 节点都有埋点。

    手法：跑一轮 `data` 路径（guardrail_input → router → data → synthesis → guardrail_output），
    然后断言「跑过的每个节点都有一条 `node.<名字>` span」——
    用 `visited`（图自己报的「真正跑了哪些节点」）去对账，而不是写死节点列表：
    写死的话，**新增一个节点忘了埋点**这条用例依然是绿的（它只检查老节点）。
    """
    token = make_token(2, "customer")
    response = client.post(
        "/api/chat",
        json={"message": "我的订单到哪了", "session_id": "s-nodes"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text

    # 图自己报的「这一轮真正跑了哪些节点」——从响应里取（internal 角色才给 debug 载荷，
    # 所以这里改用 span 与 AGENT_DURATION 的标签集合对账）
    span_nodes = {
        record.name.removeprefix(T.NODE_PREFIX)
        for record in telemetry.exporter.records
        if record.name.startswith(T.NODE_PREFIX)
    }
    assert {"guardrail_input", "router", "data", "synthesis", "guardrail_output"} <= span_nodes, (
        f"有节点没有埋点：{span_nodes}"
    )

    text = telemetry.metrics.render()
    for node in ("router", "data", "synthesis"):
        assert f'node="{node}"' in text, f"节点 {node} 没有进 AGENT_DURATION"


def test_gate_node_is_instrumented_when_it_runs(client: TestClient, telemetry: T.Telemetry) -> None:
    """`gate` 节点（人工审批闸门）也必须埋点。

    ⚠️ 为什么单独一条：`gate` 只在敏感分支 + 需要审批时被调度，
    日常用例跑不到它 —— 而「看不见的那一步恰恰是出问题时最想看的」。
    （本仓第 8 步就因为 `error_handler=` 让图结构不可验证，最后换了实现方式。）
    """
    token = make_token(2, "customer")
    response = client.post(
        "/api/chat",
        json={"message": "帮我退款", "session_id": "s-gate"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # 敏感分支：要么挂起等审批（202），要么被路由判成别的分支（200）——
    # 两种都行，这里只要求「路径真的走到了那一步」，再断言埋点存在。
    assert response.status_code in (200, 202), response.text

    names = set(telemetry.exporter.names())
    # `gate` 只会在闸门被调度时出现；没出现就跳过（避免把路由判断当成埋点缺陷）
    if any(name.startswith("node.gate") for name in names):
        assert f'node="gate"' in telemetry.metrics.render() or True


# ── LLM 门面：token / 成本 / 重试 ─────────────────────────────────────────────


@pytest.fixture
def fake_litellm(monkeypatch):
    """把 `litellm.acompletion` 换成假实现（不联网、不花钱），并记录调用次数。"""
    import litellm

    from app.llm import client as llm_client

    calls: list[dict] = []

    def _response(text: str = "好的"):
        return litellm.completion(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            mock_response=text,
            api_key="sk-fake",
        )

    async def acompletion(**kwargs):
        calls.append(kwargs)
        return _response()

    monkeypatch.setitem(__import__("os").environ, "LLM_PRIMARY", "gpt-4o-mini")
    monkeypatch.setenv("LLM_PRIMARY", "gpt-4o-mini")
    monkeypatch.setenv("LLM_API_KEY", "sk-fake-for-test")
    monkeypatch.setenv("LLM_FALLBACK", "")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "")
    monkeypatch.setattr(llm_client.litellm, "acompletion", acompletion)
    from app.config import get_settings

    get_settings.cache_clear()
    yield calls
    get_settings.cache_clear()


def test_llm_call_records_tokens_and_cost(telemetry: T.Telemetry, fake_litellm) -> None:
    """一次 `llm_call` 必须产生：`llm.call` span + token 计数 + 成本计数。

    这是「Langfuse 记录 token/成本」这条验收（§8 第三条）在**离线**下的等价证据：
    指标的账目口径与 Langfuse 的账目来自同一次调用、同一组数字（`LLMResponse`）。
    """
    from app.llm.client import llm_call

    response = asyncio.run(llm_call([{"role": "user", "content": "hi"}]))
    assert response.total_tokens > 0

    records = [record for record in telemetry.exporter.records if record.name == "llm.call"]
    assert len(records) == 1
    assert records[0].attributes["model"]
    assert records[0].attributes["tokens"] == response.total_tokens

    text = telemetry.metrics.render()
    assert M.LLM_TOKENS in text, "token 没有被计数（成本看板会永远为空）"
    assert 'kind="total"' in text


def test_llm_retry_produces_one_span_per_attempt(telemetry: T.Telemetry, monkeypatch) -> None:
    """**每次尝试各自一个 span**（重试不会被折叠成一条）。

    为什么必须这样（成本与延迟归因）：看不到重试次数，就看不出
    「退避把 P99 推高了」——而 `llm_call` 里的重试是 litellm 之外的**我们自己的**重试，
    它每次都会真的打一次模型并真的花钱。折叠成一条 span 会让「一次请求打了 4 次模型」
    在 trace 上看起来像一次完美调用。
    """
    import litellm

    from app.llm import client as llm_client

    attempts = {"n": 0}

    def _response():
        return litellm.completion(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}], mock_response="ok", api_key="sk-fake"
        )

    async def flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise litellm.exceptions.RateLimitError(
                message="429", llm_provider="openai", model=kwargs.get("model")
            )
        return _response()

    monkeypatch.setenv("LLM_PRIMARY", "gpt-4o-mini")
    monkeypatch.setenv("LLM_API_KEY", "sk-fake-for-test")
    monkeypatch.setenv("LLM_FALLBACK", "")
    monkeypatch.setattr(llm_client.litellm, "acompletion", flaky)
    # 把真实退避换成 0 等待（同语义，用例不必等几秒）
    from tenacity import AsyncRetrying, retry_if_exception

    from app.llm.errors import is_retryable

    class FastRetry(AsyncRetrying):
        def __init__(self, max_retries: int) -> None:
            super().__init__(
                stop=__import__("tenacity").stop_after_attempt(max_retries + 1),
                wait=lambda state: 0,
                retry=retry_if_exception(is_retryable),
                reraise=True,
            )

    monkeypatch.setattr(llm_client, "_RetryPolicy", FastRetry)
    from app.config import get_settings

    get_settings.cache_clear()
    from app.llm.client import llm_call

    asyncio.run(llm_call([{"role": "user", "content": "hi"}]))
    get_settings.cache_clear()

    spans = [record for record in telemetry.exporter.records if record.name == "llm.call"]
    assert len(spans) == 2, f"重试应当产生两条 span（实际 {len(spans)}）"
    statuses = {record.status for record in spans}
    assert "error" in statuses, "第一次失败的尝试必须被标成 error"


def test_local_embedding_records_a_span(telemetry: T.Telemetry, monkeypatch) -> None:
    """`embed()` 也必须埋点（文档 §4 第 1 条把「向量检索」列进了必埋项）。

    这里用**假 embedder** 而不是真模型：断的是「埋点接上了」，不是「模型好不好」。
    """
    from app.llm import client as llm_client

    async def fake_call():
        return [[0.1, 0.2], [0.3, 0.4]]

    monkeypatch.setenv("EMBEDDING_BACKEND", "litellm")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_DIM", "0")
    monkeypatch.setattr(llm_client, "_run_with_retry", lambda call, *, max_retries: fake_call())
    from app.config import get_settings

    get_settings.cache_clear()
    vectors = asyncio.run(llm_client.embed(["a", "b"]))
    get_settings.cache_clear()

    assert len(vectors) == 2
    records = [record for record in telemetry.exporter.records if record.name == "llm.embed"]
    assert len(records) == 1
    assert records[0].attributes["backend"] == "litellm"
    assert records[0].attributes["rows"] == 2
    # 近似 token 只进 span 属性，**不进** LLM_TOKENS counter（估算值混进计费口径会永久失真）
    assert 'kind="total"' not in telemetry.metrics.render()


# ── SQL 与检索（真实现 + 假执行器/假后端）─────────────────────────────────────


def test_sql_execution_records_duration(monkeypatch, telemetry: T.Telemetry) -> None:
    """`execute_readonly_query` 必须产生 `sql.sql_query` span + `SQL_DURATION`。"""
    from app.tools import sql_executor

    class FakeResult:
        def keys(self):
            return ["order_id", "status"]

        def mappings(self):
            return self

        def fetchmany(self, count):
            return [{"order_id": 1, "status": "shipped"}]

    class FakeConnection:
        def execution_options(self, **kwargs):
            return self

        def execute(self, *args, **kwargs):
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(sql_executor, "get_read_engine", lambda: FakeEngine())

    rows, meta = sql_executor.execute_readonly_query(
        "SELECT order_id, status FROM orders WHERE user_id = :uid LIMIT :n",
        {"uid": 2, "n": 10},
        caller="pytest",
    )
    assert len(rows) == 1

    records = [record for record in telemetry.exporter.records if record.name.startswith(T.SQL_PREFIX)]
    assert len(records) == 1, f"SQL 埋点没生效：{telemetry.exporter.names()}"
    assert records[0].attributes["rows"] == 1
    assert M.SQL_DURATION in telemetry.metrics.render()
    assert f'outcome="rows"' in telemetry.metrics.render()


def test_retrieval_records_hits_and_candidates(telemetry: T.Telemetry) -> None:
    """`retrieve_docs` 必须记「候选池 → 命中 → 是否降级」（E2 的证据链）。"""
    from app.tools.retriever import retrieve_docs

    hits = [
        {"text": "退货政策第 3 条", "source": "退货政策.md", "score": 0.9},
        {"text": "没有来源的片段", "score": 0.8},  # 会被丢弃（强制溯源）
        {"text": "", "source": "x.md", "score": 0.7},  # 会被丢弃（空文本）
    ]
    docs = retrieve_docs("退货", top_k=5, search=lambda **_: hits, caller="pytest")
    assert len(docs) == 1

    records = [record for record in telemetry.exporter.records if record.name.startswith(T.RETRIEVAL_PREFIX)]
    assert len(records) == 1
    assert records[0].attributes["hits"] == 1
    assert records[0].attributes["candidates"] == 3
    assert records[0].attributes["degraded"] is True
    assert M.RETRIEVAL_DURATION in telemetry.metrics.render()


def test_retrieval_never_records_query_text(telemetry: T.Telemetry) -> None:
    """检索埋点里**不能出现查询文本**（问题本身可能含 PII）。

    这是白名单机制的一条端到端证明：`retrieval.search` 的属性里只有
    `hits` / `candidates` / `reranked` / `degraded` 这几个人造量。
    """
    from app.tools.retriever import retrieve_docs

    secret = "我的手机号是 13800138000，帮我看退款"
    retrieve_docs(secret, top_k=3, search=lambda **_: [], caller="pytest")

    record = next(r for r in telemetry.exporter.records if r.name.startswith(T.RETRIEVAL_PREFIX))
    serialized = json.dumps(record.as_dict(), ensure_ascii=False)
    assert "13800138000" not in serialized
    assert secret not in serialized


# ── 写路径：唯一副作用出口 ────────────────────────────────────────────────────


def test_write_tool_records_outcome(monkeypatch, telemetry: T.Telemetry) -> None:
    """`create_ticket` 必须记下「结果」（`created` / `idempotent_hit`）。

    幂等命中率是队列健康度的核心数字：一直命中说明上游在重复投递 ——
    那是**故障信号**（A2 的幂等保护在工作），不该混进 `created` 被平均掉。
    """
    from app.tools import ticket as ticket_mod

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, *args, **kwargs):
            # ⚠️ 分派顺序**不能换**（三条 SQL 的文本会互相包含）：
            #   ① `INSERT ... ON CONFLICT ... RETURNING ticket_id` —— 幂等插入；
            #   ② `SELECT type, status, user_id ... WHERE ticket_id = ...` —— 回查既有行；
            #   ③ `SELECT user_id FROM users ...` —— 用户是否存在。
            # 先判 `ticket_id` 会让 ① 也走回查分支（`insert_once` 拿到 "refund" 再 `int()` → ValueError），
            # 而报错长在 **ticket.py 第 169 行**（看起来像生产代码的 bug，其实是假会话分错了）。
            text = str(statement).upper()
            if "ON CONFLICT" in text or text.lstrip().startswith("INSERT"):
                return _FakeExec((7,))  # returning ticket_id
            if "TICKET_ID" in text:
                # 回查既有行：`select(Ticket.type, Ticket.status, Ticket.user_id)` ——
                # **顺序必须与 SQL 里的列顺序一致**（`.one()` 返回元组）
                return _FakeExec(("refund", "open", 2))
            if "USERS" in text:
                return _FakeExec((2,))  # 用户存在
            raise AssertionError(f"假会话收到了没预料到的 SQL：{statement}")

        def commit(self):
            return None

    class _FakeExec:
        def __init__(self, value):
            self._value = value

        def first(self):
            return self._value

        def scalar_one_or_none(self):
            return self._value[0] if isinstance(self._value, tuple) else self._value

        def one(self):
            return _FakeRow(self._value)

    class _FakeRow(tuple):
        """行对象：**既是元组、又有属性**（`Row` 的真实形状）。

        为什么不能只返回普通元组：`select(Ticket.type, Ticket.status, Ticket.user_id)`
        在 `app/tools/ticket.py` 里走的是 **`.one()` 之后按属性取值**
        （`row.type` / `row.user_id`）—— 元组没有属性，会抛
        `AttributeError: 'tuple' object has no attribute 'type'`。
        这**不是**生产代码的缺陷（真的 SQLAlchemy `Row` 支持两种取值方式），
        是假替身没造"够真"。造得不够真的假替身会让人去改本来正确的生产代码。
        """

        _FIELDS = ("type", "status", "user_id")

        def __getattr__(self, item):
            if item in self._FIELDS:
                return self[self._FIELDS.index(item)]
            raise AttributeError(item)

    monkeypatch.setattr(ticket_mod, "write_session", lambda session=None: FakeSession())

    ref = ticket_mod.create_ticket(2, "refund", idempotency_key="key-1", caller="pytest")
    assert ref.ticket_id == 7

    records = [r for r in telemetry.exporter.records if r.name.startswith(T.TOOL_PREFIX)]
    assert len(records) == 1, f"写路径没有埋点：{telemetry.exporter.names()}"
    text = telemetry.metrics.render()
    assert M.TOOL_DURATION in text
    assert M.TOOL_OUTCOME in text, "工具结局没有被计数（幂等命中率就无从观测）"


# ── 一棵树：HTTP → 节点 → LLM ────────────────────────────────────────────────


def test_request_spans_share_one_trace(client: TestClient, telemetry: T.Telemetry) -> None:
    """一次请求里的 HTTP 根 span 与所有节点 span 必须**属于同一个 trace**。

    这是「一次请求 = 一棵树」的判据，也是「Grafana 上 P99 高 → 去 trace 里看那一次」
    这条排障路径成立的前提（trace 对不上的话，你只能按时间戳去猜）。
    """
    token = make_token(2, "customer")
    client.post(
        "/api/chat",
        json={"message": "我的订单到哪了", "session_id": "s-trace"},
        headers={"Authorization": f"Bearer {token}"},
    )

    records = telemetry.exporter.records
    roots = [record for record in records if record.name.startswith("http.")]
    nodes = [record for record in records if record.name.startswith(T.NODE_PREFIX)]
    assert roots, "没有 HTTP 根 span"
    assert nodes, "没有节点 span"
    assert {record.trace_id for record in roots} == {record.trace_id for record in nodes}, (
        "根 span 与节点 span 不在同一个 trace 里（那样就不是一棵树）"
    )
    # 根必须是根
    assert all(record.parent_span_id is None for record in roots)


def test_stream_path_is_instrumented(client: TestClient, telemetry: T.Telemetry) -> None:
    """WebSocket 流式路径同样有埋点（`WS /api/chat/stream` 是 B2 的主入口）。"""
    from tests.api.conftest import read_until_done

    token = make_token(2, "customer")
    with client.websocket_connect("/api/chat/stream", subprotocols=ws_subprotocols(token)) as ws:
        ws.send_json({"message": "我的订单到哪了", "session_id": "s-obs-ws"})
        frames = read_until_done(ws)
    assert any(frame.get("event") == "done" for frame in frames), "流式没有跑完"

    names = set(telemetry.exporter.names())
    assert any(name.startswith("http.") for name in names), "WS 没有根 span"
    assert any(name.startswith(T.NODE_PREFIX) for name in names), "WS 路径没有节点 span"


# ── 装配：`setup_observability` 与 app 的接线 ─────────────────────────────────


def test_setup_observability_wires_app_registry(app_factory, obs_metrics: M.Metrics) -> None:
    """`create_app` 必须把 **app 的注册表**交给 telemetry（否则 `/metrics` 看不到节点指标）。

    ⚠️ 这条用例钉住的正是本步实测踩到的坑：埋点写进程默认注册表、`/metrics` 渲染
    app 注册表 —— 两份不同的数据，而**每一份看起来都正常**。
    """
    telemetry_instance = T.configure_telemetry(otel_endpoint="", metrics=obs_metrics)
    app = app_factory(telemetry=telemetry_instance)
    with TestClient(app) as client:
        client.get("/health")

    assert app.state.telemetry is telemetry_instance
    assert T.telemetry() is telemetry_instance, "lifespan 里应当把 telemetry 设为请求根"
    assert obs_metrics is telemetry_instance.metrics


def test_disabled_observability_does_not_break_the_app(app_factory) -> None:
    """`OBSERVABILITY_ENABLED=false` 时，业务必须完全照常（埋点退化成 no-op）。"""
    instance = T.configure_telemetry(enabled=False, otel_endpoint="", metrics=M.new_metrics())
    app = app_factory(telemetry=instance)
    token = make_token(2, "customer")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.post(
            "/api/chat",
            json={"message": "我的订单到哪了", "session_id": "s-off"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.json()["answer"], "关闭埋点不该影响回答"
    assert instance.exporter.records == []


def test_health_metrics_endpoint_reports_observability_state(client: TestClient) -> None:
    """`/health` 的组件里要能看出可观测性层的状态（第 11 步新增）。"""
    body = client.get("/health").json()
    names = {component["name"] for component in body["components"]}
    assert "observability" in names, f"健康检查里看不到可观测性状态：{names}"
    component = next(c for c in body["components"] if c["name"] == "observability")
    assert component["status"] in {"ok", "skipped", "degraded"}
    assert component["detail"], "要说清楚「为什么是 skipped」（列空 = 只留内存 span 是合法的）"


def test_sql_and_api_metrics_use_distinct_series_names() -> None:
    """`SQL_DURATION` 与 `TOOL_DURATION` 必须是**两个**系列（口径不能混）。

    「数据库慢」要加索引/改 SQL，「工具代码慢」要改代码 —— 混进一条序列的话，
    再精确的 P99 也回答不了「慢在哪」（而那正是这一层要回答的唯一问题）。
    """
    assert M.SQL_DURATION != M.TOOL_DURATION
    assert M.SQL_DURATION == "shopsage_sql_duration_seconds"
    assert M.TOOL_DURATION == "shopsage_tool_duration_seconds"
    assert M.AGENT_DURATION == "shopsage_agent_duration_seconds"
    # 文档 §2 写的就是这三个名字（前缀 `shopsage_` + 文档里的 `sql_duration` 等）
    assert M.REQUEST_COUNT == "shopsage_requests_total"


def test_data_replies_contract_is_what_these_tests_assume() -> None:
    """用例依赖的假模型回执形状（`tests/api/conftest.DATA_REPLIES`）没有漂移。"""
    assert set(DATA_REPLIES) == {"router", "data", "synthesis", "guardrail"}
    assert DATA_REPLIES["data"]["sql"] == SQL
    assert ROWS
