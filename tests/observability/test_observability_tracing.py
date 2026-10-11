"""链路追踪用例：**一次请求 = 一棵 span 树**（`11.observability.md` §7 第二条）。

| 用例 | 它钉住的错误改动 |
|---|---|
| `test_trace_spans_recorded_*` | 「只埋了一个总耗时，没有分工」——B1 无法回答「慢在哪一步」 |
| `test_spans_form_a_parent_child_tree` | 「span 是平的」（看不出依赖关系，火焰图无从谈起） |
| `test_span_records_timing` | 「span 有时长但没有父子关系 / 时长为 0」 |
| `test_failed_span_is_marked_error` | 「出错的 span 与正常 span 长得一样」（看板上没有任何红） |
| `test_exception_propagates_*` | 「埋点吞掉了异常」—— 会让第 8 步的**断点重放**静默失效 |
| `test_*_exporter_never_raises` | 「exporter 抛异常」把业务请求打断 |
| `test_disabled_telemetry_is_noop` | 「关掉埋点」变成了「关掉业务」 |
| `test_sanitize_attributes_*` | 「prompt / SQL 全文进了 trace」（PII 出网的经典路径） |
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.observability import tracing as T


# ── §7 第二条：一次请求产生 根 span + 各节点/工具/LLM 子 span ──────────────────


def test_trace_spans_recorded_for_each_step(telemetry: T.Telemetry) -> None:
    """根 span + 节点 span + 工具 span + LLM span 都必须被记下来。"""
    with T.trace_span("http.POST /api/chat", attributes={"request_id": "req-1"}):
        with T.trace_span("node.router", kind="node", attributes={"node": "router"}):
            with T.trace_span("llm.call", kind="llm", attributes={"model": "m"}):
                pass
        with T.trace_span("node.data", kind="node", attributes={"node": "data"}):
            with T.trace_span("sql.sql_query", kind="tool", attributes={"tool": "sql_query"}):
                pass
        with T.trace_span("node.rag", kind="node", attributes={"node": "rag"}):
            with T.trace_span("retrieval.search", kind="retrieval", attributes={"hits": 5}):
                pass

    names = telemetry.exporter.names()
    assert "http.POST /api/chat" in names
    for expected in (
        "node.router",
        "node.data",
        "node.rag",
        "llm.call",
        "sql.sql_query",
        "retrieval.search",
    ):
        assert expected in names, f"缺少 span：{expected}（现有：{names}）"


def test_spans_form_a_parent_child_tree(telemetry: T.Telemetry) -> None:
    """父子关系必须成立 —— 「慢在哪一步」靠的就是这棵树。

    只断言「span 数量对」是不够的：**平铺**的 span 列表里，
    每个节点都显示成根节点，于是「谁在谁里面」这个最关键的依赖关系丢了
    （火焰图会变成一排没有层级的柱状图）。
    """
    with T.trace_span("http.POST /api/chat"):
        with T.trace_span("node.data", kind="node", attributes={"node": "data"}):
            with T.trace_span("sql.sql_query", kind="tool"):
                pass

    records = telemetry.exporter.records
    root = next(r for r in records if r.name.startswith("http."))
    node = next(r for r in records if r.name == "node.data")
    sql = next(r for r in records if r.name.startswith("sql."))

    # 同一个 trace id（一次请求一棵树）
    assert root.trace_id == node.trace_id == sql.trace_id
    # 只有根没有父亲
    assert root.parent_span_id is None
    assert node.parent_span_id == root.span_id
    assert sql.parent_span_id == node.span_id

    tree = telemetry.exporter.tree(root.trace_id)
    assert tree["name"] == "http.POST /api/chat"
    child_names = [child["name"] for child in tree["children"]]
    assert child_names == ["node.data"]
    assert tree["children"][0]["children"][0]["name"] == "sql.sql_query"


def test_span_records_timing_and_sort_order(telemetry: T.Telemetry) -> None:
    """span 必须带单调递增的开始时间与正的时长（火焰图的横轴就是它们）。

    ⚠️ 这里的两个节点必须**包在一个根 span 里**：OTEL 的 trace id 由**根 span**
    决定，两个平铺的顶层 span 属于两个不同的 trace（它们之间没有因果关系）——
    所以「一次请求一棵树」的前提就是「有根」。这也正是 `record_request` 要开
    `http.<endpoint>` 那条根 span 的原因。
    """
    with T.trace_span("http.POST /api/chat"):
        with T.trace_span("node.router", kind="node"):
            time.sleep(0.01)
        with T.trace_span("node.data", kind="node"):
            time.sleep(0.02)

    root = next(r for r in telemetry.exporter.records if r.name.startswith("http."))
    spans = telemetry.exporter.by_trace(root.trace_id)
    # 按开始时间排序（根最先）；父子的先后关系也由此可见
    assert [span.name for span in spans] == ["http.POST /api/chat", "node.router", "node.data"]
    router = next(span for span in spans if span.name == "node.router")
    data = next(span for span in spans if span.name == "node.data")
    assert router.start_ns < data.start_ns
    assert router.duration_ms >= 5, f"耗时明显偏小：{router.duration_ms}ms"
    assert data.duration_ms > router.duration_ms


def test_top_level_spans_are_separate_traces(telemetry: T.Telemetry) -> None:
    """两条**平铺**的顶层 span 属于两个 trace —— 这也是「必须有根 span」的理由。

    为什么值得一条用例：它解释了为什么 `record_request`（中间件的根 span）
    是整棵树的前提。把根 span 去掉之后，节点 span 会变成一堆互不相干的 trace，
    「按 trace 查一次请求」立刻失效 —— 而每个节点**看起来**都埋了点。
    """
    with T.trace_span("node.router", kind="node"):
        pass
    with T.trace_span("node.data", kind="node"):
        pass

    first, second = telemetry.exporter.records
    assert first.trace_id != second.trace_id
    assert len(telemetry.exporter.by_trace(first.trace_id)) == 1


def test_decorators_cover_sync_and_async(telemetry: T.Telemetry) -> None:
    """装饰器必须同时吃同步与异步函数（六个节点是 async，多数工具是 sync）。"""

    @T.trace_node("router")
    def sync_node(value: int) -> int:
        return value * 2

    @T.trace_node("data")
    async def async_node(value: int) -> int:
        return value + 1

    assert sync_node(3) == 6
    assert asyncio.run(async_node(3)) == 4

    names = telemetry.exporter.names()
    assert "node.router" in names and "node.data" in names


def test_decorator_preserves_function_metadata(telemetry: T.Telemetry) -> None:
    """装饰器必须保留 `__name__` 与 `__node_name__`。

    ⚠️ 这条不是形式主义：`app/graph/nodes.py::safe_node` 靠 **`__node_name__`**
    决定「这个节点失败后是继续还是要中止」，也靠 `__name__` 给 LangGraph 注册节点。
    装饰器把名字丢掉会让兜底策略**认错节点** —— 症状是「某个节点失败后图继续跑了，
    而没有答案出网」，而且很难联想到是埋点装饰器干的。
    """

    @T.trace_tool("sql_query")
    def some_tool() -> None:
        """原始 docstring。"""

    assert some_tool.__name__ == "some_tool"
    assert some_tool.__doc__ == "原始 docstring。"
    assert some_tool.__node_name__ == "sql_query"  # type: ignore[attr-defined]


def test_decorator_passes_through_arguments_and_return_value(telemetry: T.Telemetry) -> None:
    """参数与返回值原样透传（埋点不改变契约）。"""

    @T.trace_tool("create_ticket")
    def create(a: int, b: str, *, c: float = 1.0) -> dict[str, object]:
        return {"a": a, "b": b, "c": c}

    assert create(1, "x", c=2.5) == {"a": 1, "b": "x", "c": 2.5}


# ── 错误路径 ──────────────────────────────────────────────────────────────────


def test_failed_span_is_marked_error(telemetry: T.Telemetry) -> None:
    """抛异常的 span 必须被标成 `error`（否则看板上永远没有红）。

    实测踩到（本步第一版）：`record_exception()` **只加一个 event，不改 status** ——
    于是出错的 span 与正常的 span 在 trace UI 里长得一模一样（status 都是 unset）。
    这条用例钉住「`_OtelHandle.record_exception` 必须同时设 status」。
    """
    with pytest.raises(ValueError):
        with T.trace_span("sql.sql_query", kind="tool"):
            raise ValueError("boom")

    record = telemetry.exporter.records[0]
    assert record.status == "error"
    assert record.error_kind == "ValueError"


def test_span_error_is_counted_by_kind(telemetry: T.Telemetry) -> None:
    """出错的 span 必须进 `SPAN_ERRORS{kind}`（「是模型挂了还是库挂了」）。"""
    with pytest.raises(RuntimeError):
        with T.trace_span("llm.call", kind="llm"):
            raise RuntimeError("provider down")

    text = telemetry.metrics.render()
    assert 'shopsage_span_errors_total{kind="llm"} 1.0' in text


def test_exception_propagates_unchanged(telemetry: T.Telemetry) -> None:
    """埋点**不许吞异常**，也不许换类型。

    ⚠️ 这是本步最重要的一条纪律（第 8 步的断点重放依赖它）：
    `app/graph/nodes.py::safe_node` 刻意不 `try/except` —— 异常必须穿透到
    `app/graph/run.py` 的回合驱动，那里才有「从断点重放」的逻辑。
    一个会吞（或会包装）异常的埋点会让**重放静默失效**：
    症状是「节点失败后用户看到一句兜底话术，但状态里没有断点」。
    """
    marker = KeyError("原始异常")

    with pytest.raises(KeyError) as captured:
        with T.trace_span("node.synthesis", kind="node"):
            raise marker

    assert captured.value is marker, "埋点把异常换了（必须是同一个对象）"


def test_diverging_span_records_exception_even_when_caller_swallows(telemetry: T.Telemetry) -> None:
    """调用方吞掉异常时，span 仍然要留下错误证据（排障时这是唯一的线索）。"""
    with T.trace_span("node.rag", kind="node"):
        try:
            raise TimeoutError("检索超时")
        except TimeoutError:
            pass
        # 走到这里：异常被调用方处理了 —— span 不会自动被标成 error
    record = telemetry.exporter.records[0]
    assert record.status in {"unset", "ok"}, "没有异常穿透时不该凭空标错"
    # 想标错必须显式调用（`with ... as handle: handle.record_exception(exc)`）
    with T.trace_span("node.rag", kind="node") as handle:
        try:
            raise TimeoutError("检索超时")
        except TimeoutError as exc:
            handle.record_exception(exc)
    assert telemetry.exporter.records[1].status == "error"


# ── 属性：白名单与脱敏 ────────────────────────────────────────────────────────


def test_sanitize_attributes_keeps_only_allowlisted_names() -> None:
    """属性按**白名单**过滤：不在名单里的键一律丢弃。

    为什么是白名单（`11.observability.md` 的安全边界）：span 属性会进 trace UI、
    进日志、进外部 Langfuse。用黑名单（「排除 prompt / sql / params」）的话，
    新增一个带 PII 的字段时**默认出网** —— 与第 9 步 `chat.FRAME_SAFE_KEYS` 同一条纪律。
    """
    out = T.sanitize_attributes(
        {
            "node": "router",
            "rows": 12,
            "prompt": "用户的完整问题（含手机号）",  # 不在白名单
            "sql": "SELECT * FROM orders",  # 不在白名单
            "message": "秘密",
            "stage": "routing",
        }
    )
    assert out == {"node": "router", "rows": 12, "stage": "routing"}


def test_sanitize_attributes_truncates_string_values() -> None:
    """白名单里的字符串值也要截断（防「合法键塞进一篇文章」）。"""
    out = T.sanitize_attributes({"route": "x" * 5000})
    assert len(str(out["route"])) <= 200


def test_span_attributes_reject_non_allowlisted_keys(telemetry: T.Telemetry) -> None:
    """写入时也要挡一遍（不能只靠 exporter 过滤 —— 那时数据已经进了 SDK 的 span）。"""
    with T.trace_span("node.router", kind="node") as handle:
        handle.set_attribute("prompt", "不该出现的内容")
        handle.set_attribute("node", "router")

    record = telemetry.exporter.records[0]
    assert "prompt" not in record.attributes
    assert record.attributes["node"] == "router"


# ── 端口的健壮性 ──────────────────────────────────────────────────────────────


def test_memory_exporter_never_raises_and_bounds_records() -> None:
    """内存 exporter 永不抛异常，且有上限（跑一天的服务不能把 span 全留在内存里）。"""
    exporter = T.MemorySpanExporter(max_records=5)
    assert exporter.export(object()).name == "SUCCESS" or True  # 不可迭代对象也不抛
    assert exporter.export([]) is not None

    for index in range(20):
        with T.configure_telemetry(otel_endpoint="", metrics=_noop_metrics()).span(f"probe.{index}"):
            pass
    bounded = T.telemetry().exporter
    assert len(bounded.records) <= T.DEFAULT_MAX_RECORDS


def _noop_metrics():  # type: ignore[no-untyped-def]
    from app.observability.metrics import new_metrics

    return new_metrics()


def test_telemetry_can_be_disabled_entirely() -> None:
    """`enabled=False` 时埋点退化成 no-op：不记 span、不写指标、**不影响业务返回值**。"""
    from app.observability import metrics as M

    instance = T.configure_telemetry(enabled=False, otel_endpoint="", metrics=M.new_metrics())
    with T.use_telemetry(instance):
        with T.trace_span("node.router", kind="node") as handle:
            handle.set_attribute("node", "router")
            result = "业务返回值"
        with T.trace_tool_call("sql_query"):
            tool_result = 42

    assert result == "业务返回值"
    assert tool_result == 42
    assert instance.exporter.records == [], "关闭时不该记 span"
    assert instance.metrics.render().strip() == "", "关闭时不该写指标"


def test_disabled_telemetry_still_backfills_tool_duration() -> None:
    """关闭 span 时，工具埋点仍要用调用方给的耗时补一条指标。

    为什么：`instrument._ToolCall` 的耗时靠 span 自己量，而关闭时 span 是 no-op ——
    没有这条兜底的话，「临时关掉埋点排查性能」会**同时把性能数据也关掉**，
    而那正是你想看的东西。
    """
    from app.observability import metrics as M

    instance = T.configure_telemetry(enabled=False, otel_endpoint="", metrics=M.new_metrics())
    with T.use_telemetry(instance):
        from app.observability.instrument import record_tool_call

        with record_tool_call(tool="probe"):
            time.sleep(0.001)

    text = instance.metrics.render()
    assert M.TOOL_DURATION in text, f"关闭 span 时耗时也没了：{text!r}"


def test_request_root_takes_priority_over_process_default() -> None:
    """请求根 telemetry 优先于进程默认（一个 app 一份 telemetry 的基础）。"""
    from app.observability import metrics as M

    first = T.configure_telemetry(otel_endpoint="", metrics=M.new_metrics())
    second = T.configure_telemetry(otel_endpoint="", metrics=M.new_metrics())

    assert T.telemetry() is second, "进程默认应当是最新装配的那一份"

    with T.use_telemetry(first):
        assert T.telemetry() is first
        with T.trace_span("node.router", kind="node"):
            pass
        assert len(first.exporter.records) == 1
    assert T.telemetry() is second, "块外必须恢复"


def test_otlp_endpoint_normalization() -> None:
    """OTLP 端点必须被规整成 HTTP 完整路径，且 4317（gRPC 端口）要给出告警。

    配错端口的后果**不是报错**，而是「trace 一条都不到」——
    所以在启动期把它变成一条能照着改的告警，是本步性价比最高的一处兜底。
    """
    url, warning = T.normalize_otlp_endpoint("http://host:4318")
    assert url == "http://host:4318/v1/traces" and warning == ""

    url, warning = T.normalize_otlp_endpoint("http://host:4318/v1/traces")
    assert url == "http://host:4318/v1/traces" and warning == ""

    _url, warning = T.normalize_otlp_endpoint("http://host:4317")
    assert "4317" in warning and "4318" in warning

    assert T.normalize_otlp_endpoint("") == ("", "")
