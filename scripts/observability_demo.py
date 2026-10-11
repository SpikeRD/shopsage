r"""可观测性验收工具（第 11 步）：**七个场景，每个都打印可核对的数字**。

用法（**必须在仓库根执行**）：

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uv run python -m scripts.observability_demo --scenario all
uv run python -m scripts.observability_demo --scenario spans     # 一次请求的 span 树
uv run python -m scripts.observability_demo --scenario p99       # 分位数（B1 的核心）
uv run python -m scripts.observability_demo --scenario cardinality
uv run python -m scripts.observability_demo --scenario langfuse
uv run python -m scripts.observability_demo --scenario sql
uv run python -m scripts.observability_demo --scenario config
```

## 这个脚本存在的理由

第 11 步的三条验收（`11.observability.md` §8）都需要**数字**，而数字要能在一台新机器上复跑出来：

| 验收 | 谁来回答 | 本脚本的哪个场景 |
|---|---|---|
| 每个节点/工具/LLM/检索都有埋点 | span 树 + 指标系列清单 | `spans` |
| Grafana 能看到分 Agent 耗时 / P99 | 从桶里算出的 P50/P90/P95/P99 | `p99` |
| Langfuse 记录 token/成本/质量 | 一次调用的完整账目 | `langfuse` |

⚠️ **不做的事**：不连 Redis/Postgres、不调真模型、不起 Prometheus/Grafana。
那三样是 `2.infra-runbook.md` 与 Grafana 面板的活。本脚本只回答
「埋点在代码里真的生效了吗、口径对不对」——**离线可复跑**是它的全部价值。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from app.observability import langfuse_cb
from app.observability import metrics as M
from app.observability import tracing as T
from app.observability.langfuse_cb import LangfuseHandler
from app.observability.metrics import Metrics, MetricsSettings

#: 演示用的细桶（真机是 10ms 起；这里被观测的代码是内存操作，微秒级）
DEMO_BUCKETS: MetricsSettings = MetricsSettings(
    fast_buckets=(0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0),
    slow_buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0),
)


class FakeLangfuseClient:
    """假 Langfuse client（演示里**绝不**连外网：真 client 的 flush 会阻塞 2~24s）。"""

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self._base_url = "http://langfuse.demo（假的）"

    def start_as_current_observation(self, **kwargs: Any) -> Any:
        self.observations.append(dict(kwargs))
        import contextlib

        class _Observation:
            id = f"obs-demo-{len(self.observations)}"

        @contextlib.contextmanager
        def _cm() -> Any:
            yield _Observation()

        return _cm()

    def score_current_trace(self, **kwargs: Any) -> None:
        self.scores.append(dict(kwargs))

    def create_score(self, **kwargs: Any) -> None:
        self.scores.append(dict(kwargs))

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


def _banner(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _telemetry() -> tuple[T.Telemetry, Metrics]:
    """装一套**完全离线**的 telemetry（内存 span exporter + 独立注册表）。"""
    metrics = M.new_metrics(settings=DEMO_BUCKETS)
    instance = T.configure_telemetry(
        enabled=True,
        service_name="shopsage-demo",
        otel_endpoint="",  # 空 = 只留内存（不建 OTLP exporter、不重试）
        metrics=metrics,
    )
    return instance, metrics


# ── 场景 ①：一次请求的 span 树 ────────────────────────────────────────────────


def scenario_spans() -> dict[str, Any]:
    """模拟一次 `data` 路径的请求，打印**完整的 span 树**。

    结构照着真实图节点写（`app/graph/nodes.py::safe_node` + `llm_call` + `execute_readonly_query`），
    但**不跑真图**：本场景要证明的是「埋点粒度 = 架构第 7 节」（每个节点/工具/LLM/检索各一条），
    不是「图跑得通」（那是第 8 步 78 条用例的活）。
    """
    _banner("① 一次请求的 span 树（每个节点 / 工具 / LLM / 检索都埋点）")
    telemetry, _metrics = _telemetry()

    with T.use_telemetry(telemetry):
        with T.trace_span("http.POST /api/chat", attributes={"request_id": "demo-req-1"}):
            # guardrail_input（零 token，只做规则校验）
            with T.trace_span("node.guardrail_input", kind="node", attributes={"node": "guardrail_input"}):
                time.sleep(0.0005)
            # router：一次 LLM 调用
            with T.trace_span("node.router", kind="node", attributes={"node": "router", "stage": "routing"}):
                with T.trace_llm("deepseek-chat") as span:
                    time.sleep(0.003)
                    span.set_attributes({"tokens": 320, "cost_usd": 0.00008})
            # data：一次 LLM（NL→SQL）+ 一次 SQL 执行
            with T.trace_span("node.data", kind="node", attributes={"node": "data", "stage": "data"}):
                with T.trace_llm("deepseek-chat") as span:
                    time.sleep(0.004)
                    span.set_attributes({"tokens": 540, "cost_usd": 0.00012})
                with T.trace_span("sql.sql_query", kind="tool", attributes={"tool": "sql_query"}) as span:
                    time.sleep(0.002)
                    span.set_attributes({"rows": 2})
        # synthesis：一次 LLM（**最贵的一步**：上下文最长，token 最多）
            with T.trace_span("node.synthesis", kind="node", attributes={"node": "synthesis"}):
                with T.trace_llm("deepseek-chat") as span:
                    time.sleep(0.006)
                    span.set_attributes({"tokens": 1580, "cost_usd": 0.00042})
            with T.trace_span("node.guardrail_output", kind="node", attributes={"node": "guardrail_output"}):
                time.sleep(0.0004)
            # 模拟一次「不该有」的检索：证明两条腿的埋点也能挂进同一棵树
            with T.trace_span("node.rag", kind="node", attributes={"node": "rag"}):
                with T.trace_span("retrieval.search", kind="retrieval", attributes={"hits": 5, "candidates": 30}):
                    time.sleep(0.0025)

    records = telemetry.exporter.records
    trace_id = next(r.trace_id for r in records if r.name.startswith("http."))
    tree = telemetry.exporter.tree(trace_id)

    def render(node: dict[str, Any], depth: int = 0) -> None:
        mark = "└─" if depth else "●"
        attrs = node["attributes"]
        extra = " ".join(f"{k}={v}" for k, v in sorted(attrs.items()) if k not in ("node",))
        print(f"  {'  ' * depth}{mark} {node['name']:<28} {node['duration_ms']:>8.3f}ms  {extra}")
        for child in node["children"]:
            render(child, depth + 1)

    render(tree)
    print()
    print(f"  共 {len(records)} 条 span，全部属于 trace {trace_id}")
    print("  断言：每条 span 都有自己的 trace/span id，父子关系由 parent_span_id 表达")
    return {"spans": len(records), "trace_id": trace_id}


# ── 场景 ②：分位数（B1 的核心）───────────────────────────────────────────────


def scenario_p99() -> dict[str, Any]:
    """给每个节点灌一批**形状不同**的耗时，打印 P50/P90/P95/P99，并演示「均值骗人」。

    数据的形状是刻意设计的：
      * `router` 快而稳（10~12ms）→ P50 与 P99 接近，说明「它没问题」；
      * `data` 有长尾（多数 30ms，少数 400ms）→ P50 正常、P99 高 12 倍；
      * `synthesis` 整体慢（600~900ms）→ P50 就高，说明「普遍慢」。
    这三种形状对应三种完全不同的处置，而**只看均值会把前两种看成同一件事**。
    """
    _banner("② 分位数：P99 才是 B1 的答案（均值会把长尾藏起来）")
    _telemetry_instance, metrics = _telemetry()

    for _ in range(200):
        metrics.observe(M.AGENT_DURATION, 0.010 + (_ % 5) * 0.0005, {"node": "router"})
    for index in range(200):
        # 195 个 30ms + 5 个 ~400ms：这就是「长尾」
        metrics.observe(M.AGENT_DURATION, 0.4 if index % 40 == 0 else 0.030, {"node": "data"})
    for index in range(200):
        metrics.observe(M.AGENT_DURATION, 0.60 + (index % 7) * 0.05, {"node": "synthesis"})

    print(f"  {'节点':<12}{'P50':>10}{'P90':>10}{'P95':>10}{'P99':>10}{'均值':>10}{'P99/均值':>10}")
    print("  " + "-" * 72)
    worst: tuple[str, float] = ("", 0.0)
    for node in ("router", "data", "synthesis"):
        series = f'{M.AGENT_DURATION}{{node="{node}"}}'
        values = M.quantiles(metrics, series)
        snap = metrics.snapshot()["histograms"][series]
        mean = snap["sum"] / snap["count"] if snap["count"] else 0.0
        # `quantiles()` 在系列不存在时返回 None（而不是 0）—— 这里显式 `or 0.0`
        # 只用于**显示**（看板那边保留 None，因为「没有数据」与「0ms」是两件事）
        p50 = float(values["p50"] or 0.0)
        p90 = float(values["p90"] or 0.0)
        p95 = float(values["p95"] or 0.0)
        p99 = float(values["p99"] or 0.0)
        ratio = p99 / mean if mean else 0.0
        print(
            f"  {node:<12}{p50:>10.4f}{p90:>10.4f}{p95:>10.4f}{p99:>10.4f}{mean:>10.4f}{ratio:>9.2f}x"
        )
        if p99 > worst[1]:
            worst = (node, p99)

    print()
    print(f"  瓶颈节点（按 P99）：**{worst[0]}**（{worst[1]:.4f}s）")
    data = metrics.snapshot()["histograms"][f'{M.AGENT_DURATION}{{node="data"}}']
    data_mean = data["sum"] / data["count"] if data["count"] else 0.0
    data_p50 = float(metrics.histogram_quantile(f'{M.AGENT_DURATION}{{node="data"}}', 0.5) or 0.0)
    data_p99 = float(metrics.histogram_quantile(f'{M.AGENT_DURATION}{{node="data"}}', 0.99) or 0.0)
    print(
        f"  ⚠️ 反例：data 节点均值={data_mean:.4f}s、P50={data_p50:.4f}s —— "
        "看这两个数字会以为它很健康，"
        f"而它的 P99={data_p99:.4f}s（最差的那 1% 用户已经走了）"
    )
    return {"worst_node": worst[0], "worst_p99": worst[1]}


# ── 场景 ③：基数 ──────────────────────────────────────────────────────────────


def scenario_cardinality() -> dict[str, Any]:
    """演示「标签选错 → 序列爆炸」，以及门面的兜底（截断 + 白名单）。"""
    _banner("③ 标签基数：为什么状态码要折叠、路径不能原样进标签")
    _telemetry_instance, metrics = _telemetry()

    # 正确做法：折叠后的 endpoint + 状态类
    for _ in range(50):
        metrics.counter(M.REQUEST_COUNT, {"endpoint": "GET /health", "status_class": "2xx"})
    good = len(metrics.snapshot()["counters"])

    # 错误做法（**这段演示的是「如果原样写会怎样」，不写进真实代码**）：
    # 每个 order id 一条序列
    for order_id in range(50):
        metrics.counter(M.REQUEST_COUNT, {"endpoint": f"GET /api/orders/{order_id}", "status_class": "2xx"})
    bad = len(metrics.snapshot()["counters"])

    print(f"  50 笔同类请求，折叠标签 → {good} 条时间序列")
    print(f"  50 笔同类请求，原始路径 → {bad} 条时间序列（{bad // good}x）")
    print()
    print("  另两道兜底（`metrics.Metrics` 的 `LABEL_LIMITS` 与 `max_label_chars`）：")
    long_value = "/api/orders/" + "9" * 500
    metrics.counter("shopsage_cardinality_probe_total", {"endpoint": long_value})
    rendered = next(
        line for line in metrics.render().splitlines() if line.startswith("shopsage_cardinality_probe_total")
    )
    value = rendered.split('endpoint="', 1)[1].split('"', 1)[0]
    print(f"    超长标签值：{len(long_value)} 字符 → 落库 {len(value)} 字符（截断）")
    outside = T.sanitize_attributes({"prompt": "用户的完整问题（含手机号）", "node": "router"})
    print(f"    span 属性白名单：{{'prompt':…,'node':…}} → {outside}（prompt 被丢弃）")
    return {"folded_series": good, "raw_series": bad}


# ── 场景 ④：Langfuse 账目 ────────────────────────────────────────────────────


def scenario_langfuse() -> dict[str, Any]:
    """演示一次 LLM 调用在 Langfuse 里的完整账目（token / 成本 / 质量 / trace 关联）。

    用**假 client**：真 client 在没配密钥时会禁用、配了假密钥会真的建 TLS 连接并在
    后台重试（实测 `flush()` 阻塞 2~24s）—— 演示脚本不该有这种副作用。
    """
    _banner("④ Langfuse：token / 成本 / 质量（+ 与 trace 对得上账）")
    telemetry, _metrics = _telemetry()
    client = FakeLangfuseClient()
    handler = LangfuseHandler(client=client, enabled=True, telemetry=telemetry)
    langfuse_cb.set_langfuse_handler(handler)

    with T.use_telemetry(telemetry):
        with T.trace_span("node.synthesis", kind="node", attributes={"node": "synthesis"}):
            with handler.record(model="deepseek-chat", user_id="2", session_id="s-demo") as rec:
                rec.tokens(prompt=1200, completion=380, total=1580)
                rec.cost(0.00042)
                rec.quality(name="answer_relevance", value=0.86)
                rec.quality(name="faithfulness", value=0.91)
                rec.meta(route="data", cache="miss")

    finding = handler.findings[-1]
    payload = client.observations[-1]
    print(f"  model           : {finding.model}")
    print(f"  tokens          : prompt={finding.prompt_tokens} completion={finding.completion_tokens} total={finding.total_tokens}")
    print(f"  cost_usd        : {finding.cost_usd}")
    print(f"  质量分（score）  : {json.dumps(finding.scores, ensure_ascii=False)}")
    print(f"  observation id  : {finding.observation_id}")
    print(f"  trace_context   : {payload.get('trace_context')}")
    print(f"  上报类型        : {payload.get('as_type')}（generation 才会进成本/延迟聚合）")
    print(f"  usage_details   : {payload.get('usage_details')}")
    print()
    print("  未配置密钥时（默认）：")
    off = LangfuseHandler(disabled_reason="缺 public_key 或 secret_key")
    started = time.perf_counter()
    with off.record(model="deepseek-chat") as rec:
        rec.tokens(prompt=1200, completion=380, total=1580).cost(0.00042)
    elapsed = time.perf_counter() - started
    print(f"    enabled={off.enabled} 原因={off.state['reason']!r}")
    print(f"    账目仍留在进程内：findings={len(off.findings)}，开销 {elapsed * 1000:.3f}ms（零网络）")
    # 恢复进程默认（避免影响后续场景）
    langfuse_cb.set_langfuse_handler(None)
    return {"scores": finding.scores, "trace_linked": bool(payload.get("trace_context"))}


# ── 场景 ⑤：SQL 埋点 ─────────────────────────────────────────────────────────


def scenario_sql() -> dict[str, Any]:
    """演示 `execute_readonly_query` 的埋点（含**失败**的那条路径）。

    失败路径也要埋点，理由很具体：只埋成功的查询会让「库挂了」在看板上表现为
    「这段时间没有数据」——而「没有数据」与「一切正常」在图上长得一样。
    """
    _banner("⑤ SQL 埋点：成功与失败都要留痕")
    telemetry, metrics = _telemetry()
    from app.tools import sql_executor

    class _Result:
        def keys(self):
            return ["order_id", "status"]

        def mappings(self):
            return self

        def fetchmany(self, count):
            return [{"order_id": 1024, "status": "shipped"}, {"order_id": 1025, "status": "pending"}]

    class _Conn:
        def execution_options(self, **kwargs):
            return self

        def execute(self, *args, **kwargs):
            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Engine:
        def connect(self):
            return _Conn()

    class _BrokenEngine:
        def connect(self):
            from sqlalchemy.exc import OperationalError

            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    original = sql_executor.get_read_engine
    with T.use_telemetry(telemetry):
        sql_executor.get_read_engine = lambda: _Engine()
        rows, meta = sql_executor.execute_readonly_query(
            "SELECT order_id, status FROM orders WHERE user_id = :uid LIMIT :n",
            {"uid": 2, "n": 10},
            caller="demo",
        )
        print(f"  成功：rows={len(rows)} truncated={meta['truncated']} 耗时={meta['duration_ms']}ms")

        sql_executor.get_read_engine = lambda: _BrokenEngine()
        try:
            sql_executor.execute_readonly_query(
                "SELECT order_id FROM orders WHERE user_id = :uid LIMIT 5",
                {"uid": 2},
                caller="demo",
            )
        except Exception as exc:  # noqa: BLE001 - 演示失败路径
            print(f"  失败：{type(exc).__name__}（**也被埋点了**）")
        sql_executor.get_read_engine = original

    series = [line for line in metrics.render().splitlines() if line.startswith(M.SQL_DURATION)]
    print()
    print("  `SQL_DURATION` 系列：")
    for line in series[:6]:
        print(f"    {line}")
    errors = [line for line in metrics.render().splitlines() if line.startswith(M.SPAN_ERRORS)]
    print("  出错 span：" + (errors[0] if errors else "（无）"))
    print()
    print("  ⚠️ SQL 文本与绑定参数**都不进指标/span**（只进审计）：")
    attributes = {
        record.name: dict(record.attributes)
        for record in telemetry.exporter.records
        if record.name.startswith(T.SQL_PREFIX)
    }
    print(f"    span 属性：{json.dumps(attributes, ensure_ascii=False)}")
    return {"rows": len(rows), "span_errors": bool(errors)}


# ── 场景 ⑥：装配与配置 ───────────────────────────────────────────────────────


def scenario_config() -> dict[str, Any]:
    """打印从 `.env` 读到的可观测性配置与**装好之后的实际状态**。"""
    _banner("⑥ 配置与装配状态（从 .env 读，改完要重启）")
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    print(f"  OBSERVABILITY_ENABLED : {settings.observability_enabled}")
    print(f"  SERVICE_NAME          : {settings.service_name}")
    print(f"  OTEL_ENDPOINT         : {settings.otel_endpoint!r}"
          f"{'（空 = span 只留内存）' if not settings.otel_endpoint else ''}")
    print(f"  LANGFUSE_PUBLIC_KEY   : {'已配置' if settings.langfuse_public_key else '（空 = LLM 记录关闭）'}")
    print(f"  LANGFUSE_SECRET_KEY   : {'已配置' if settings.langfuse_secret_key.get_secret_value() else '（空）'}")
    print(f"  LANGFUSE_HOST         : {settings.langfuse_host or '（空 = SDK 默认）'}")

    url, warning = T.normalize_otlp_endpoint(settings.otel_endpoint)
    print()
    print(f"  归一后的 OTLP URL     : {url or '（不外发）'}")
    if warning:
        print(f"  ⚠️ {warning}")

    from app.observability import setup_observability

    instance = setup_observability(settings, metrics=M.new_metrics(settings=DEMO_BUCKETS))
    print()
    print("  装好之后：")
    print(f"    telemetry.enabled   : {instance.enabled}")
    print(f"    telemetry.service   : {instance.service_name}")
    print(f"    langfuse            : {langfuse_cb.langfuse_handler().state}")
    print()
    print("  三件工具的分工（`11.observability.md` §4 第 2 条）：")
    print("    OTEL       → 单次请求的调用链（父子 span、火焰图）")
    print("    Prometheus → 长期聚合（分位数、告警）：/metrics")
    print("    Langfuse   → LLM 专用（token / 成本 / 质量）")
    langfuse_cb.set_langfuse_handler(None)
    return {"enabled": instance.enabled, "endpoint": instance.otel_endpoint}


# ── 场景 ⑦：metric 系列清单（「每个节点都埋了吗」的机械核对）─────────────────


def scenario_series() -> dict[str, Any]:
    """跑一轮完整的假请求，然后**列出所有产生的指标系列**并与文档要求逐条核对。"""
    _banner("⑦ 指标系列清单：与 `11.observability.md` §2 的对账")
    telemetry, metrics = _telemetry()
    from app.observability.instrument import (
        record_llm_call,
        record_request,
        record_retrieval,
        record_sql_query,
        record_tool_call,
    )

    with T.use_telemetry(telemetry):
        record_request(endpoint="POST /api/chat", seconds=0.42, status=200, request_id="req-1")
        for node in ("guardrail_input", "router", "data", "synthesis", "guardrail_output"):
            with T.trace_span(f"node.{node}", kind="node", attributes={"node": node}):
                time.sleep(0.001)
        # ⚠️ `record_llm_call` **只写指标**（token/成本是累加量，与 span 是两回事）——
        # 而 `LLM_DURATION` 那条直方图由 **span** 产生（`tracing._duration_series`）。
        # 生产路径里这两件事同时发生（`llm/client.py` 的 `_call` 先开 `trace_llm` 再记指标），
        # 所以演示也必须同时做，否则会对不上账 —— 而那个「对不上」是**演示脚本的**错，
        # 不是实现的错（这正是本场景存在的意义：把对账做成可执行的检查）。
        with T.trace_llm("deepseek-chat"):
            record_llm_call(
                model="deepseek-chat",
                latency_ms=380,
                prompt_tokens=1200,
                completion_tokens=380,
                total_tokens=1580,
                cost_usd=0.00042,
            )
        with record_sql_query(tool="sql_query") as call:
            call.set_attributes({"rows": 2})
        with record_tool_call(tool="create_ticket") as call:
            call.outcome("created", rows=1)
        with record_retrieval(hits=5, candidates=30) as call:
            call.set_attributes({"reranked": True})
            call.outcome("hits", rows=5)

    # `metrics.registry.collect()` 给的是**指标族**，而族的名字对 counter 来说
    # **不含 `_total`**（`prometheus_client` 把 `_total` 只放在样本名上）——
    # 所以要拿样本名来对账。这个细节很容易让人以为「counter 没被注册」。
    names = sorted({sample.name for metric in metrics.registry.collect() for sample in metric.samples})
    families = sorted({metric.name for metric in metrics.registry.collect()})
    print("  产生的指标系列（样本名，含 `_total` / `_bucket` / `_count` / `_sum`）：")
    for name in names:
        print(f"    {name}")

    required = {
        "REQUEST_COUNT": M.REQUEST_COUNT,
        "REQUEST_DURATION": M.REQUEST_DURATION,
        "AGENT_DURATION": M.AGENT_DURATION,
        "TOOL_DURATION": M.TOOL_DURATION,
        "LLM_DURATION": M.LLM_DURATION,
        "LLM_TOKENS": M.LLM_TOKENS,
        "LLM_COST": M.LLM_COST,
        "SQL_DURATION": M.SQL_DURATION,
        "RETRIEVAL_DURATION": M.RETRIEVAL_DURATION,
        "SPAN_ERRORS": None,  # 只有出错时才有
        "TOOL_OUTCOME": M.TOOL_OUTCOME,
    }
    print()
    print("  与文档 §2 的对账（`SQL_DURATION` 是文档点名的那个）：")
    # 用**样本名前缀**匹配：直方图的族名没有后缀，而样本名带 `_bucket` / `_count` / `_sum`
    sample_names = set(names)
    missing: list[str] = []
    for label, name in required.items():
        if name is None:
            print(f"    {'—':<4} {label}（只在出错时产生）")
            continue
        ok = name in sample_names or any(sample.startswith(f"{name}_") for sample in sample_names)
        print(f"    {'✅' if ok else '❌'} {label:<20} {name}")
        if not ok:
            missing.append(label)

    spans = sorted({record.name for record in telemetry.exporter.records})
    print()
    print(f"  span 名（{len(spans)} 个）：")
    for name in spans:
        print(f"    {name}")
    return {"families": families, "missing": missing, "spans": spans}


SCENARIOS = {
    "spans": scenario_spans,
    "p99": scenario_p99,
    "cardinality": scenario_cardinality,
    "langfuse": scenario_langfuse,
    "sql": scenario_sql,
    "config": scenario_config,
    "series": scenario_series,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="第 11 步可观测性验收工具")
    parser.add_argument(
        "--scenario",
        choices=[*SCENARIOS, "all"],
        default="all",
        help="跑哪个场景（默认 all）",
    )
    args = parser.parse_args()

    print("ShopSage · 可观测性验收（第 11 步 observability）")
    print(f"场景：{args.scenario}    span 上限：{T.DEFAULT_MAX_RECORDS} 条（内存 exporter）")

    chosen = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    started = time.perf_counter()
    results: dict[str, Any] = {}
    for name in chosen:
        results[name] = SCENARIOS[name]()

    _banner("汇总")
    for name in chosen:
        print(f"  {name:<12} {json.dumps(results[name], ensure_ascii=False, default=str)}")
    print()
    print(f"  总耗时 {time.perf_counter() - started:.2f}s（全离线：不连库、不调模型、不联网）")

    missing = results.get("series", {}).get("missing") or []
    if missing:
        print(f"  ❌ 缺少文档要求的指标：{missing}")
        return 1
    print("  ✅ 全部场景完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
