"""埋点接线：**每个被埋点的调用点该记什么**（第 11 步的「粒度过账表」）。

`11.observability.md` §4 第 1 条要求「每个 Agent 节点、每次工具调用、每次 LLM 调用、
每次向量检索都埋点」。本模块把这条要求落成一张**可以逐条核对**的表 ——
因为「埋点漏了一处」是一种不会报错的缺陷：系统照跑，只是那一步在白板上看不见，
而**看不见的那一步恰恰是出问题时最想看的**（第 8 步的 `gate` 节点就是这么被漏掉过一次）。

| 埋点位置 | 代码里的落点 | span 名 | 指标 |
|---|---|---|---|
| HTTP 请求 | `app/api/middleware.py::_record` | `http.<方法 路径>` | `REQUEST_COUNT` / `REQUEST_DURATION` / `TRACE_COUNT` |
| 图节点（8 个） | `app/graph/nodes.py::safe_node` | `node.<节点名>` | `AGENT_DURATION{node}` |
| LLM 调用 | `app/llm/client.py::_call` | `llm.call` | `LLM_DURATION` / `LLM_TOKENS` / `LLM_COST` |
| embedding | `app/llm/client.py::embed` | `llm.embed` | `LLM_DURATION`（token 只进 span 属性，见下） |
| SQL 执行 | `app/tools/sql_executor.py` | `sql.sql_query` | `SQL_DURATION{tool}` |
| 知识库检索 | `app/tools/retriever.py::retrieve_docs` | `retrieval.search` | `RETRIEVAL_DURATION` |
| 写路径工具 | `app/tools/write_path.py` | `tool.<tool>` | `TOOL_DURATION{tool}` |

## 为什么埋点要「在调用点内联」而不是包一层中间件

包一层中间件（比如包住所有工具函数）看起来更省事，但它拿不到**只有那个调用点才知道的量**：
SQL 的行数、检索的候选池大小、LLM 的 token 与成本。而这些量恰恰是分诊时最需要的
（「P99 高」的下一步永远是「是模型慢了、还是返回的行太多了」）。
所以本步的口径是：**框架能力集中在 `metrics`/`tracing`，每个调用点自己写那 3~6 行**。

## 四个「不要」

1. **不要把查询文本 / prompt 全文塞进 span 属性**：会进 trace UI、进日志、进 Langfuse。
   属性白名单在 `tracing.ALLOWED_ATTRIBUTES`（**白名单而不是黑名单**，理由写在那里）。
2. **不要在这里 import 业务模块**（`app.graph` / `app.tools` / `app.agents`）：
   本模块会被那些模块反过来 import，反向依赖会立刻造成循环导入。
   这里只用 `app.observability` 自己。
3. **不要让埋点抛异常**：`tracing` / `metrics` 内部已经各自兜了一层，
   本模块只负责「把事实拼好」，不替它们重复兜底（重复兜底会掩盖真正的 bug）。
4. **不要用 `metrics.default_metrics()`**：见 `_metrics()` 的说明 ——
   那会让埋点写进「进程默认注册表」，而 `/metrics` 渲染的是「app 的注册表」。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from app.observability.metrics import (
    REQUEST_COUNT,
    REQUEST_DURATION,
    RETRIEVAL_DURATION,
    SQL_DURATION,
    TOOL_DURATION,
    TOOL_OUTCOME,
    Metrics,
    record_cost,
    record_tokens,
)
from app.observability.tracing import (
    LLM_PREFIX,
    NODE_PREFIX,
    RETRIEVAL_PREFIX,
    SQL_PREFIX,
    TOOL_PREFIX,
    TRACE_COUNT,
    TraceHandle,
    telemetry,
    trace_llm,
    trace_span,
)

logger = logging.getLogger(__name__)


class _ToolCall:
    """一次工具/检索/SQL 调用的记录器（见 `record_tool_call` 的用法与理由）。

    **一次调用 = 一个对象**（而不是「几个函数各记一笔」）：因为耗时、span、
    结果标签必须来自**同一个时间段**与**同一次调用**。分开写的话，
    「记了一个 span、忘了记指标」这类遗漏不会有任何症状（本步的目标就是消除这种缺口）。
    """

    def __init__(
        self,
        *,
        tool: str,
        rows: int | None = None,
        truncated: bool = False,
        error: BaseException | None = None,
        series: str = TOOL_DURATION,
        prefix: str = TOOL_PREFIX,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        self.tool = str(tool)
        self.series = series
        self._attributes: dict[str, Any] = {"tool": self.tool, **(dict(extra or {}))}
        if rows is not None:
            self._attributes["rows"] = int(rows)
        if truncated:
            self._attributes["degraded"] = True
        self._error = error
        self._span_name = f"{prefix}{self.tool}"
        self._manager: Any = None
        self._handle: TraceHandle | None = None
        self._started: float = 0.0

    # ── 调用点可用的两个方法 ─────────────────────────────────────────────────
    def set_attributes(self, values: Mapping[str, Any]) -> None:
        self._attributes.update(values)
        if self._handle is not None:
            self._handle.set_attributes(values)

    def outcome(self, outcome: str, *, rows: int | None = None) -> None:
        """贴一个结果标签并计数（`shopsage_tool_outcome_total{tool,outcome}`）。

        ⚠️ `outcome` **必须是有限集合里的值**（本仓约定用 `created` / `idempotent_hit` /
        `updated` / `empty`）：它是标签，而标签的每个新取值都是一条新的时间序列。
        把自由文本（例如错误消息）当 outcome 就是基数爆炸 —— 这一条写在函数签名旁边，
        因为它是**调用点最容易犯**的错。
        """
        text = str(outcome)
        metrics = _metrics()
        metrics.counter(
            TOOL_OUTCOME,
            {"tool": self.tool, "outcome": text},
        )
        self.set_attributes({"outcome": text, **({"rows": int(rows)} if rows is not None else {})})

    # ── 上下文管理器协议 ─────────────────────────────────────────────────────
    def __enter__(self) -> _ToolCall:
        self._started = time.perf_counter()
        self._manager = telemetry().span(self._span_name, kind="tool", attributes=self._attributes)
        self._handle = self._manager.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        # ⚠️ 这里**不吞异常**（返回 False）：埋点是旁路，它无权改变被观测代码的控制流
        # （第 8 步的断点重放依赖「节点异常穿透到回合驱动」）。
        if exc is None and self._error is not None and self._handle is not None:
            # 「事后才知道失败」的场合（调用方提前拿到了异常对象）
            self._handle.record_exception(self._error)
        elapsed = time.perf_counter() - self._started
        if self._manager is None:
            return False
        # `telemetry().span()` 自己负责计时与写指标（`_after`），这里只负责收口：
        # 于是「耗时口径」在全项目只有一处（`with` 块内的一切），不可能三个调用点写三种。
        try:
            return bool(self._manager.__exit__(exc_type, exc, tb))
        finally:
            # 关掉 span 时（`enabled=False`）`_after` 不会跑 —— 那时用这里量的耗时补一条指标。
            # 不补的后果很具体：「临时关掉埋点排查性能」会**同时把性能数据也关掉**，
            # 而那正是你想看的东西（实测踩到：关掉之后 `/metrics` 里一条耗时都没有）。
            if not telemetry().enabled:
                _backfill_duration(self.series, elapsed, {"tool": self.tool})


def _backfill_duration(series: str, seconds: float, labels: Mapping[str, str]) -> None:
    """span 被关掉时，用调用方给的耗时补一条观测（见 `_ToolCall.__exit__`）。"""
    if seconds > 0:
        _metrics().observe(series, seconds, labels)


def _metrics() -> Metrics:
    """**当前生效的**指标注册表（跟着请求走，见 `tracing.telemetry()`）。

    ⚠️ 这里刻意**不**用 `metrics.default_metrics()`：那样埋点会写进「进程默认注册表」，
    而 `/metrics` 渲染的是「app 的注册表」—— 两份不同的数据。
    症状是**指标确实产生了，但抓取端永远看不到**（本步实测踩到：
    `/metrics` 里只有 `shopsage_api_*`，节点级指标一条都没有）。
    走 `telemetry()` 就让两者必然是同一个对象：`create_app` 把 app 的注册表
    交给了这个 app 的 telemetry（见 `app/api/main.py` 的 lifespan）。
    """
    return telemetry().metrics


# ── HTTP 请求（中间件调用）───────────────────────────────────────────────────


def record_request(
    *,
    endpoint: str,
    seconds: float,
    status: int,
    request_id: str = "",
) -> None:
    """记一次 HTTP 请求的**计数与耗时**（只写指标；根 span 由 `trace_request_span` 负责）。

    ## 为什么一开始就要说清「两个名字」

    * `shopsage_requests_total` / `shopsage_request_duration_seconds` —— 第 11 步的通用名；
    * `shopsage_api_requests_total` / `shopsage_api_request_duration_seconds` —— 第 9 步的
      **对外契约名**（已有的看板与告警按它配好了，改名字等于让它们静默失效）。

    两个名字在同一次调用里写、用**同一个耗时值**，所以不会漂移成两个数字。
    真正的纪律是「**一个名字只有一个写入方**」—— 这里就是那两个名字的写入方。

    ⚠️ `endpoint` 必须已经**折叠**过（`"GET /health"`）：原始路径带 id 时
    （`/api/orders/12345`）每个 id 都是一条新时间序列（基数爆炸）。
    """
    from app.api.metrics import API_REQUEST_DURATION, API_REQUESTS_TOTAL

    metrics = _metrics()
    labels = {"endpoint": endpoint, "status_class": f"{int(status) // 100}xx"}
    for name in (REQUEST_COUNT, TRACE_COUNT, API_REQUESTS_TOTAL):
        metrics.counter(name, labels)
    for name in (REQUEST_DURATION, API_REQUEST_DURATION):
        metrics.observe(name, seconds, labels)
    del request_id, status  # 都不进指标标签（请求 id 在根 span 的属性里，状态码已折叠）


# ── LLM（`app/llm/client.py` 调用）──────────────────────────────────────────


def record_llm_call(
    *,
    model: str,
    latency_ms: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cost_usd: float | None = None,
    finish_reason: str = "",
    backend: str = "litellm",
    error: BaseException | None = None,
) -> None:
    """记一次 LLM 调用的账（token / 成本 / 结束原因 / 是否报错）。

    为什么 token 与成本记在**指标**里，而延迟记在 **span** 里：
    延迟需要「按模型、按时间窗看分位数」（Prometheus 的 histogram + `histogram_quantile`），
    而 token/成本是**累加量**（counter 才能在任意时间窗上做 `increase()`）。
    两者都塞进一种工具会有一半用不出来 —— 这是「三件工具各管一摊」的最具体的一处。

    ⚠️ `total_tokens` 由调用方给（不在这里 `prompt + completion`）：
    provider 的 `total` 可能包含 reasoning / 缓存 token，自己加会与账单对不上。

    ⚠️ `finish_reason` / `backend` 只进日志与 span 属性，**不进指标标签**：
    它们是高基数的自由文本（`finish_reason` 的取值集合由厂商决定），
    当标签会随厂商升级悄悄长出新的时间序列。
    """
    metrics = _metrics()
    record_tokens(
        metrics,
        model=model or "unknown",
        prompt=int(prompt_tokens or 0),
        completion=int(completion_tokens or 0),
        total=int(total_tokens or 0),
    )
    record_cost(metrics, model=model or "unknown", cost_usd=cost_usd)
    if error is not None:
        # 失败也要留一行：只看成功调用的成本，会把「重试烧掉的钱」全部漏掉
        # （而重试恰恰是成本失控最常见的原因）
        logger.info(
            "LLM 调用失败：model=%s 耗时=%dms backend=%s error=%s（token 可能已计费）",
            model or "?",
            latency_ms,
            backend,
            type(error).__name__,
        )
    elif finish_reason == "length":
        # 与 `_to_response` 里那条 WARNING 互补：那条说「内容为空且被截断」，
        # 这条说「有内容但被截断了」—— 后者同样要调大 max_tokens，
        # 只是不表现为空回答（回答只讲到一半），所以更容易被忽略。
        logger.info("LLM 输出被截断：model=%s finish_reason=length", model or "?")


# ── 工具与检索（各自的调用点调用）─────────────────────────────────────────────


def record_tool_call(
    *,
    tool: str,
    rows: int | None = None,
    truncated: bool = False,
    error: BaseException | None = None,
) -> _ToolCall:
    """记一次工具调用（写路径工具、图表、工单等）；**用 `with` 包住被计时的代码块**。

    ```python
    with record_tool_call(tool="create_ticket") as call:
        ...                                  # 被计时的那段
        call.outcome("created", rows=1)      # 给这次调用贴一个结果标签
    ```

    `rows` / `truncated` 进 span 属性（白名单里都有）：它们是「这一步为什么慢」的
    第一手解释 —— 一次取回 200 行（被截断）与一次取回 1 行，
    在同一张火焰图上是两条不同的故事。

    ⚠️ **不要传耗时**：第一版让调用方传 `seconds=`，结果是「每个调用点都要自己
    perf_counter 一遍」—— 三个调用点写了三种口径（有的含校验、有的不含），
    于是 `TOOL_DURATION` 这个指标**内部不可比**。现在耗时由本函数自己量，
    口径只有一处：**`with` 块内的一切**。

    ⚠️ `error` 参数保留给「事后才知道失败」的场合；正常路径下**不要**用它 ——
    `with` 块里抛出的异常会自动被 span 记下来（`record_exception` + ERROR 状态），
    再传一次会让同一个错误记两遍（trace 里出现两条 exception event）。
    """
    return _ToolCall(tool=tool, rows=rows, truncated=truncated, error=error)


def record_sql_query(
    *,
    tool: str,
    rows: int | None = None,
    truncated: bool = False,
    error: BaseException | None = None,
) -> _ToolCall:
    """记一次 SQL 执行（`sql.` 前缀 → 耗时进 `SQL_DURATION`，**与 `TOOL_DURATION` 分开**）。

    为什么单独一个函数而不是复用 `record_tool_call`：`11.observability.md` §2 把
    `SQL_DURATION` 与 `TOOL_DURATION` 列成了两个指标，而它们的处置完全不同 ——
    「数据库慢」要加索引/改 SQL，「工具代码慢」要改代码。混进一条序列的话，
    再精确的 P99 也回答不了「慢在哪」（而那正是这一层要回答的唯一问题）。
    """
    return _ToolCall(tool=tool, rows=rows, truncated=truncated, error=error, series=SQL_DURATION, prefix=SQL_PREFIX)


def record_retrieval(
    *,
    hits: int,
    candidates: int = 0,
    reranked: bool = False,
    degraded: bool = False,
    error: BaseException | None = None,
) -> _ToolCall:
    """记一次向量/混合检索（**不记查询文本**：问题本身可能含 PII）。

    记「候选池多大 → 命中几条 → 是否精排」这一串：它是 E2（上下文成本）与
    「检索好不好用」的共同证据 —— 候选池 11907 token 压到 top-5 3246 token 这个
    结论（第 10 步）就是从这三个数里算出来的。
    """
    return _ToolCall(
        tool="hybrid_search",
        rows=hits,
        truncated=False,
        error=error,
        series=RETRIEVAL_DURATION,
        prefix=RETRIEVAL_PREFIX,
        extra={"candidates": int(candidates), "reranked": bool(reranked), "degraded": bool(degraded)},
    )


def trace_request_span(method: str, path: str) -> Any:
    """一次 HTTP 请求的**根 span**（`http.<方法 路径>`）；用 `with` 包住整个请求。

    ⚠️ **必须在中间件里 `with` 住整段下游调用**，不能「请求结束后补一条」。
    第一版就是「结束后补一条」——它看起来完全合理（访问日志也在那里写），但：
      ① 它开在请求**结束之后**，不可能是任何节点 span 的父亲；
      ② 每个节点 span 因此各自成为根 → **每个节点一个 trace**，
         「按 trace 查一次请求」这条排障路径彻底失效，
         而每个节点看起来都埋了点（这是最迷惑人的形态）。
    `with` 住整段之后，OTEL 的 contextvars 会让下游**一切**埋点自动挂到它下面，
    而下游（六个 Agent、工具、LLM 门面）**不需要知道中间件的存在**。

    单独一个函数（而不是让中间件直接调 `trace_span`）的原因：
    中间件住在 `app/api/`，而那里对 `app/observability` 的依赖越窄越好
    （第 9 步的契约用例钉着「API 层不许碰业务实现」）。这里是一处**有意的**窄接口。
    """
    return trace_span(
        f"http.{method} {path}",
        attributes={"status_class": "", "outcome": "in_flight"},
    )


def count_trace(*, endpoint: str) -> None:
    """给一次**跑完的**请求计数（`TRACE_COUNT`；看板上「有没有流量」的第一个信号）。

    与 `record_request` 的分工：本函数是**通用入口**（脚本、压测器、演示脚本
    都能在跑完一次调用后自己记一笔），而 `record_request` 是**中间件专用**的
    （它同时写黄金指标的计数与耗时）。两者都写 `TRACE_COUNT`，
    所以调用方**不要同时用**（会重复计数）—— 中间件走 `record_request` 就够了。
    """
    _metrics().counter(TRACE_COUNT, {"endpoint": endpoint})


def node_span_attributes(*, node: str, **extra: Any) -> dict[str, Any]:
    """节点埋点的标准属性（**一处定义**，避免八个节点各写一份标签名）。"""
    return {"node": node, **extra}


def node_span(node: str) -> Any:
    """一次图节点的 span（`node.<节点名>`）；`with` 包住这一跳的执行。

    为什么需要它（而不是让 `app/graph/nodes.py` 直接调 `tracing.trace_span`）：
    **span 名的构造规约必须只有一处**。`node.` / `tool.` / `sql.` / `llm.` / `retrieval.`
    这五个前缀决定了耗时进哪条指标（`tracing._duration_series` 是唯一的映射处），
    而调用点自己拼 `f"node.{name}"` 时，拼错一个字母不会报错 ——
    只会让那条 span 的耗时**静默地不进任何指标**（症状是「这个节点的 P99 怎么没有」）。
    所以：业务代码只调 `instrument` 的具名入口，`tracing` 是它的实现层。
    """
    return trace_span(f"{NODE_PREFIX}{node}", kind="node", attributes={"node": str(node)})


def embed_span(model: str, *, backend: str, input_tokens: int, rows: int) -> Any:
    """一次 embedding 的 span（`llm.embed`）；`with` 包住调用之后的那一段。

    ⚠️ token 用 `input_tokens`（近似口径：本地后端拿不到真实 usage），
    所以它**只进 span 属性，不进 `LLM_TOKENS` counter** ——
    把估算值混进计费口径的 counter 会让那个数字永久失真（而它看起来仍然精确）。
    """
    return trace_span(
        "llm.embed",
        kind="llm",
        attributes={"model": model, "backend": backend, "tokens": input_tokens, "rows": rows},
    )


def llm_span(model: str, *, attributes: Mapping[str, Any] | None = None) -> Any:
    """一次 LLM 调用的 span（`llm.call`，`kind="llm"` → 耗时进 `LLM_DURATION`）。

    与 `tracing.trace_llm` 是同一个东西，这里再包一层只为**让 `app/llm/client.py`
    只依赖 `instrument` 一个模块**（否则它要从两个模块各 import 一次，
    而「埋点归谁管」这件事就有了两个答案）。
    """
    return trace_llm(model, attributes=attributes)


def current_trace_id() -> str:
    """当前 trace id（没有活跃 span 时为空串）。

    给「要把日志与 trace 对上」的场景用（例如 `chat.py` 的完成日志）：
    排障时从日志里拿 trace id 去 trace UI，比按时间戳去猜要快一个数量级。
    """
    try:
        from opentelemetry import trace as otel_trace

        context = otel_trace.get_current_span().get_span_context()
        if not getattr(context, "is_valid", False):
            return ""
        return format(context.trace_id, "032x")
    except Exception:  # noqa: BLE001
        return ""


def describe() -> dict[str, Any]:
    """当前埋点配置的一句话摘要（启动日志与 `/health` 用它）。"""
    active = telemetry()
    return {
        "enabled": active.enabled,
        "service": active.service_name,
        "otel_endpoint": active.otel_endpoint or "",
        "spans_recorded": len(active.exporter.records),
        "prefixes": [NODE_PREFIX, TOOL_PREFIX, SQL_PREFIX, LLM_PREFIX, RETRIEVAL_PREFIX],
    }


def span_attributes(handle: TraceHandle, attributes: Mapping[str, Any]) -> None:
    """给一个 span 批量补属性（**唯一**的入口，便于把白名单校验收敛在一处）。"""
    handle.set_attributes(attributes)


__all__ = [
    "count_trace",
    "current_trace_id",
    "describe",
    "embed_span",
    "llm_span",
    "node_span",
    "node_span_attributes",
    "record_llm_call",
    "record_request",
    "record_retrieval",
    "record_sql_query",
    "record_tool_call",
    "span_attributes",
    "trace_request_span",
]
