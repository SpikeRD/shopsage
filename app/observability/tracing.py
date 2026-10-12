"""链路追踪：**OTEL 的父子 span** + 一个能离线断言的内存 exporter（第 11 步）。

`11.observability.md` §4 第 1 条把埋点粒度钉死了：**每个 Agent 节点、每次工具调用、
每次 LLM 调用、每次向量检索都要埋点**。理由是 B1 —— 「P99 高但不知道慢在哪」的解药
就是「一次请求 = 一棵树，每步一个子 span」：看火焰图就知道时间花在哪个 span。

所以本模块提供的不是「要不要埋点」的开关，而是**三种埋点方式**，分别对应三类调用点：

| 方式 | 用法 | 适合 |
|---|---|---|
| 装饰器 | `@trace_node("router")` | 一个函数就是一步（六个 Agent 节点、每个工具） |
| 上下文管理器 | `with trace_llm("deepseek-chat") as span:` | 一步跨好几行代码、中途要补属性（LLM/检索） |
| 纯记录函数 | `record_span(...)` | 只有「事后拿到的数字」、没有可包住的代码块 |

## 为什么自己实现 `SpanExporter` 而不是装 `opentelemetry-sdk-test-utils`

`InMemorySpanExporter` 在**额外的包**里（`opentelemetry-sdk-test-utils`），
而本仓第 10 步已经为同一件事定过口径：**能被测试到的依赖才配当默认依赖**
（那次是 `pypdf` 进了 dev 组）。区别在于这里的结论相反 ——
「抓取 span 并断言」不是测试专属能力：**开发机上没起 collector 时，
它是唯一能看见链路的方式**（正是 `2.infra.md` 里「基础设施只起依赖、代码跑本机」
这条拓扑带来的必然需求）。所以本模块自带一个 30 行的 exporter，
装在生产依赖里，同时服务「离线断言」与「开发机自检」两个场景。

## 不给 OTEL 设全局 `TracerProvider`（一条纪律）

`opentelemetry.trace.set_tracer_provider()` **只生效一次**（第二次调用静默失败，不报错）。
一旦用了它，「测试里换一个 provider」就变成不可能 —— 而本仓已经为同一类问题
付过一次代价（第 9 步 `MetricsRegistry` 刻意不注册全局 `REGISTRY`，理由写在那个模块里）。
所以这里**始终显式持有自己的 `TracerProvider`**，span 也从它上面取 tracer；
全局 provider 保持「未设置（no-op）」状态。代价是：如果将来引入第三方
自动埋点（`opentelemetry-instrumentation-*`），它们会用全局 provider 从而与本模块脱节 ——
那是一笔将来要显式处理的账，而不是现在用全局状态换来的隐性依赖。
"""

from __future__ import annotations

import contextvars
import inspect
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, TypeVar

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, Status, StatusCode
from app.observability.metrics import (
    AGENT_DURATION,
    LLM_DURATION,
    RETRIEVAL_DURATION,
    SPAN_ERRORS,
    SQL_DURATION,
    TOOL_DURATION,
    TRACE_COUNT,
    Metrics,
    default_metrics,
)

logger = logging.getLogger(__name__)

#: span 名字的前缀（`node.router` / `tool.sql_query` / `llm.call`）。
#:
#: 为什么加前缀而不是直接用节点名：**同一棵树里会有重名的不同类步骤**
#: （`data` 是一个节点，也是一个工具名；`rag` 是节点，检索是它内部的一步）。
#: 前缀让「按类型过滤」（只看所有 LLM 调用的耗时）在 trace UI 里是一次点击，
#: 而不是一条正则。这是 trace 可读性最便宜的一笔投入。
NODE_PREFIX: Final[str] = "node."
TOOL_PREFIX: Final[str] = "tool."
LLM_PREFIX: Final[str] = "llm."
RETRIEVAL_PREFIX: Final[str] = "retrieval."
#: SQL 执行**单独一个前缀**（不用 `tool.`）。
#:
#: 为什么值得多一个前缀：`11.observability.md` §2 把 `SQL_DURATION` 与 `TOOL_DURATION`
#: 列成了**两个**指标，而两者都发生在「工具调用」里 —— 用同一个前缀会让
#: 「数据库慢」与「工具代码慢」混进同一条序列，而这两件事的处置完全不同
#: （前者要加索引/改 SQL，后者要改代码）。前缀是**分类**，分类做错了，
#: 再精确的分位数也回答不了「慢在哪」。
SQL_PREFIX: Final[str] = "sql."

#: 默认保留多少条 span 记录（内存 exporter 的上限）。
#:
#: **必须有上限**：一个跑一天的服务会产生几百万条 span，全留在内存里就是一次 OOM。
#: 1000 条足够回答「刚才那次请求慢在哪」，而按时间淘汰让它永远只占一份固定的内存。
DEFAULT_MAX_RECORDS: Final[int] = 1000

#: span 记录里**允许**出现的属性名白名单（其余一律丢弃）。
#:
#: ⚠️ 这是本模块唯一的安全边界，必须是**白名单**：span 属性会进 trace UI、
#: 进日志、进外部的 Langfuse/collector。若用黑名单（「排除 sql / params / prompt」），
#: 新增一个带 PII 的属性时**默认出网** —— 与第 9 步 `chat.FRAME_SAFE_KEYS`
#: 是同一条纪律（那次是 WebSocket 帧）。所以：值要么是白名单里的键，
#: 要么是**数字**（耗时/token/行数这类不可能含 PII 的量）。
ALLOWED_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "request_id",
        "thread_id",
        "route",
        "node",
        "tool",
        "model",
        "stage",
        "flow_state",
        "status_class",
        "caller",
        "cache",
        "degraded",
        "rows",
        "attempts",
        "hits",
        "candidates",
        "reranked",
        "tokens",
        "cost_usd",
        "error_kind",
        "rule",
        "outcome",
        "backend",
        "retrieval_legs",
        # ── 第 13 步（缓存）补的四个键 ──────────────────────────────────────
        # 为什么要新增而不是复用已有的 `cache` / `outcome`：
        # ① `cache` 的消费者是「检索腿是否来自缓存」，而这里要说的是**这一次查询的结论**
        #    （hit / miss / stale / bypassed）—— 两者同时出现在一条 `retrieval` span 上，
        #    合成一个键会让「缓存命中了但检索腿没用它」这类矛盾无法表达；
        # ② `outcome` 是工具结局（created / truncated…）的键，值的取值域完全不同。
        # 四个键都是**低基数**（outcome 5 个取值、policy 3 个、hit 布尔、fingerprint 16 位 hex
        # 每版本一个）—— 基数纪律没有被破坏。
        "cache_outcome",
        "cache_policy",
        "cache_hit",
        "cache_fingerprint",
    }
)

_T = TypeVar("_T")


class MemorySpanExporter(SpanExporter):
    """把 span 留在内存里（**离线断言**与**开发机自检**两用，见模块 docstring）。

    两个刻意的实现选择：

    * `export()` **永不抛**：OTEL 的 exporter 异常会被 provider 吞掉并打日志，
      但那会让「一条 span 的字段格式怪」升级成「不知道哪里在报错」。这里自己兜住。
    * 记录是 **`MemorySpan` 而不是 `ReadableSpan`**：`ReadableSpan` 身上挂着
      `resource` / `instrumentation_scope` / 原始 context 对象（其中 `Context` 持有
      「当前活跃的 span」，**强引用整棵 trace**）。直接存它等于把已结束的 trace 钉在内存里，
      这正是内存 exporter 泄漏的经典形态。所以这里在 export 时**立刻抽出需要的字段**。
    """

    def __init__(self, *, max_records: int = DEFAULT_MAX_RECORDS) -> None:
        self.max_records = max(1, int(max_records))
        self._records: list[MemorySpan] = []
        self._lock = threading.Lock()

    def export(self, spans: Any) -> SpanExportResult:
        try:
            with self._lock:
                for span in spans:
                    self._records.append(_to_record(span))
                if len(self._records) > self.max_records:
                    # 按时间淘汰最老的（不是清空）：清空会让「刚跑完的请求反而没有记录」
                    del self._records[: len(self._records) - self.max_records]
            return SpanExportResult.SUCCESS
        except Exception as exc:  # noqa: BLE001 - exporter 失败不能影响业务
            logger.warning("内存 span exporter 记录失败（已忽略）：%s", exc)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    # ── 读口（测试与开发自检用）──────────────────────────────────────────────
    @property
    def records(self) -> list[MemorySpan]:
        """当前保留的 span 记录（**副本**：调用方改它不影响内部状态）。"""
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def by_trace(self, trace_id: str) -> list[MemorySpan]:
        """一次请求（一个 trace）的全部 span，**按开始时间排序**。

        排序是必须的：exporter 的调用顺序受 `SimpleSpanProcessor` / `BatchSpanProcessor`
        与多线程影响，**不保证**等于时间顺序。而「慢在哪一步」依赖的正是顺序 ——
        不排序的话，火焰图会时对时错，那比没有火焰图更误导。

        ⚠️ **同一起始时刻的父子要靠第二排序键**（本步实测踩到，值得写下来）：
        OTEL 的 `start_time` 只有**微秒**精度，而 `TracerProvider` 会**向下取整**到它自己
        的时钟粒度 —— 于是「父 span + 它的第一个子 span」经常得到**完全相同**的
        `start_ns`（实测：`http.POST /api/chat` 与它的第一个子节点一模一样）。
        这时只按 `start_ns` 排序的结果由 exporter 的送出顺序决定 ——
        而**子 span 先结束、先被送出**，于是列表会变成 `[子, 父]`：
        火焰图里根节点跑到最下面，父子层级看起来是反的。
        第二排序键取 `span_id in {父 id 的集合}`：**父 span 排在自己的子之前**
        （父 id 出现在别的 span 的 `parent_span_id` 里 ⟺ 它有子节点）。
        """
        spans = [record for record in self.records if record.trace_id == trace_id]
        parent_ids = {record.parent_span_id for record in spans}
        return sorted(
            spans,
            key=lambda record: (record.start_ns, 0 if record.span_id in parent_ids else 1),
        )

    def names(self, trace_id: str | None = None) -> list[str]:
        source = self.by_trace(trace_id) if trace_id else self.records
        return [r.name for r in source]

    def tree(self, trace_id: str) -> dict[str, Any]:
        """把一次请求的 span 拼成父子树（`/metrics` 之外的排障出口，见 `configure_telemetry`）。"""
        spans = self.by_trace(trace_id)
        nodes: dict[str, dict[str, Any]] = {
            r.span_id: {**r.as_dict(), "children": []} for r in spans
        }
        root: dict[str, Any] | None = None
        for record in spans:
            node = nodes[record.span_id]
            parent = nodes.get(record.parent_span_id or "")
            if parent is None:
                if root is None:
                    root = node
                continue
            parent["children"].append(node)
        return root or {}


@dataclass(frozen=True)
class MemorySpan:
    """一条 span 的**纯数据**快照（见 `MemorySpanExporter` 的说明：不留 OTEL 对象）。"""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_ns: int
    duration_ms: float
    attributes: Mapping[str, Any] = field(default_factory=dict)
    status: str = "unset"
    error_kind: str = ""

    @property
    def depth(self) -> int:
        return 0 if not self.parent_span_id else 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status,
            "error_kind": self.error_kind,
            "attributes": dict(self.attributes),
        }


def _to_record(span: ReadableSpan) -> MemorySpan:
    """`ReadableSpan` → `MemorySpan`（在 export 期立刻抽字段，理由见 exporter docstring）。"""
    context = span.get_span_context()
    parent = getattr(span, "parent", None)
    status = getattr(span, "status", None)
    # ⚠️ `getattr(...)` 而不是 `span.status.status_code`：**未显式设过状态的 span**
    # 上这个字段存在但为 `UNSET`，而 `Status` 本身在极老的 SDK 上有过
    # 「属性名不同」的历史。更实际的理由是这里**不允许抛异常**：
    # `_to_record` 跑在 exporter 里，它抛了就等于「这条 span 悄悄消失」。
    code = getattr(status, "status_code", None)
    if code is StatusCode.OK:
        status_name = "ok"
    elif code is StatusCode.ERROR:
        status_name = "error"
    else:
        status_name = "unset"
    del status
    end = getattr(span, "end_time", None) or getattr(span, "start_time", 0)
    return MemorySpan(
        name=str(span.name),
        trace_id=format(context.trace_id, "032x"),
        span_id=format(context.span_id, "016x"),
        parent_span_id=format(parent.span_id, "016x") if parent is not None else None,
        start_ns=int(getattr(span, "start_time", 0) or 0),
        duration_ms=max(0.0, (int(end) - int(getattr(span, "start_time", 0) or 0)) / 1e6),
        attributes=sanitize_attributes(dict(getattr(span, "attributes", None) or {})),
        status=status_name,
        error_kind=_error_kind(getattr(span, "events", None) or ()),
    )


def _error_kind(events: Any) -> str:
    """从 span 的 events 里取异常类型（OTEL 把 `record_exception` 记成 event）。"""
    for event in events:
        if getattr(event, "name", "") == "exception":
            attrs = dict(getattr(event, "attributes", None) or {})
            value = attrs.get("exception.type")
            if value:
                return str(value)
    return ""


def sanitize_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """按**白名单 + 数字**过滤 span 属性（理由见 `ALLOWED_ATTRIBUTES`）。"""
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        name = str(key)
        if name not in ALLOWED_ATTRIBUTES:
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            out[name] = value
        elif isinstance(value, str):
            # 字符串标签也要截断：属性值会进 trace UI 与外部的 Langfuse
            out[name] = value[:200]
    return out


class TraceHandle(Protocol):
    """一次埋点的句柄（**调用方只依赖这个协议**，不依赖 OTEL 类型）。

    为什么不直接把 `opentelemetry.trace.Span` 给业务代码：那样六个 Agent 节点就
    都 import 了 OTEL —— 而「第 7 步的节点可独立测试」这条验收要求节点只依赖注入的零件。
    协议在这里的作用与 `metrics.Metrics` 相同：换实现（no-op / 内存 / OTLP / 录制回放）
    不影响调用点。
    """

    @property
    def trace_id(self) -> str: ...

    def set_attribute(self, key: str, value: Any) -> None: ...

    def set_attributes(self, attributes: Mapping[str, Any]) -> None: ...

    def record_exception(self, error: BaseException) -> None: ...

    def set_ok(self) -> None: ...


class _NoopHandle:
    """什么都不做的句柄（`enabled=False` 时用）。"""

    trace_id = ""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        return None

    def record_exception(self, error: BaseException) -> None:
        return None

    def set_ok(self) -> None:
        return None


class _OtelHandle:
    """把 OTEL 的 span 包成 `TraceHandle`（并顺手把白名单外的属性挡掉）。"""

    def __init__(self, span: Any) -> None:
        self._span = span
        self.failed = False

    @property
    def trace_id(self) -> str:
        return format(self._span.get_span_context().trace_id, "032x")

    def set_attribute(self, key: str, value: Any) -> None:
        if str(key) not in ALLOWED_ATTRIBUTES:
            return
        try:
            self._span.set_attribute(str(key), value)
        except Exception as exc:  # noqa: BLE001 - 埋点永不打断业务
            logger.debug("set_attribute 失败（已忽略）：%s", exc)

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def record_exception(self, error: BaseException) -> None:
        """记下异常**并把 span 标成 ERROR**。

        ⚠️ 两件事必须一起做（第一版只做了前一半，实测踩到）：
        `record_exception()` 只往 span 上加一个 `exception` **event**，**不改 status**。
        而 trace UI 的分组、告警、以及本模块 `MemorySpan.status` 读的都是 status ——
        于是「出错的 span」看起来与正常 span 一模一样（status 都是 unset），
        症状是「看板上没有任何红」。所以这里显式补 `set_status(ERROR)`。
        """
        try:
            self._span.record_exception(error)
            self._span.set_attribute("error_kind", type(error).__name__)
            self._span.set_status(Status(StatusCode.ERROR, f"{type(error).__name__}: {error}"[:200]))
            self.failed = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("record_exception 失败（已忽略）：%s", exc)

    def set_ok(self) -> None:
        try:
            self._span.set_status(Status(StatusCode.OK))
        except Exception:  # noqa: BLE001
            logger.debug("set_status 失败（已忽略）")


@dataclass
class Telemetry:
    """一个进程内的可观测性上下文：**一个 OTEL provider + 一组指标 + 一个 span 记录器**。

    为什么把三者绑在一个对象上：它们是**同一次配置**的产物。若分别做成模块级单例，
    「测试里换成内存 exporter」就要求同时改三处，而漏改一处（比如忘了换 provider）
    的症状是「span 记录了，但记进了生产的那一份」—— 极难发现。
    """

    provider: TracerProvider
    metrics: Metrics
    exporter: MemorySpanExporter
    enabled: bool = True
    service_name: str = "shopsage"
    otel_endpoint: str = ""
    #: 是否把 span 打到控制台（本机排障用；默认关，否则日志会被 span 淹掉）
    console: bool = False

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def tracer(self, name: str = "shopsage") -> Any:
        return self.provider.get_tracer(name)

    # ── 埋点原语 ─────────────────────────────────────────────────────────────
    @contextmanager
    def span(self, name: str, *, kind: str = "internal", attributes: Mapping[str, Any] | None = None) -> Iterator[TraceHandle]:
        """开一个 span（`with` 块内是**被计时的那段代码**）。

        ⚠️ 异常**必须原样抛出**：本模块是旁路，它不能改变被观测代码的控制流 ——
        否则「加了监控之后断点重放失效」会成为一次真实事故（第 8 步的核心机制依赖
        节点异常穿透到回合驱动）。
        """
        if not self.enabled:
            yield _NoopHandle()
            return
        start = time.perf_counter()
        with self.tracer().start_as_current_span(
            name, kind=_span_kind(kind), record_exception=True, set_status_on_exception=False
        ) as otel_span:
            handle = _OtelHandle(otel_span)
            handle.set_attributes(sanitize_attributes(attributes or {}))
            try:
                yield handle
            except BaseException as exc:
                handle.record_exception(exc)
                raise
            finally:
                self._after(name, kind, handle, (time.perf_counter() - start))

    def _after(self, name: str, kind: str, handle: TraceHandle, seconds: float) -> None:
        """一次 span 结束后记指标（**指标与 trace 在这里对齐**，只有一处实现）。"""
        series = _duration_series(name, kind)
        if series is not None:
            self.metrics.observe(series, seconds, _duration_labels(name))
        if isinstance(handle, _OtelHandle) and handle.failed:
            self.metrics.counter(SPAN_ERRORS, {"kind": _kind_label(name, kind)})

    def record_span(
        self,
        name: str,
        *,
        seconds: float,
        kind: str = "internal",
        attributes: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        """只有「事后拿到的数字」时用这个（包不住代码块的场合）。"""
        with self.span(name, kind=kind, attributes=attributes) as handle:
            if error is not None:
                handle.record_exception(error)
            else:
                handle.set_ok()

    # ── 生命周期 ─────────────────────────────────────────────────────────────
    def flush(self, *, timeout_ms: int = 5000) -> None:
        try:
            self.provider.force_flush(timeout_millis=int(timeout_ms))
        except Exception as exc:  # noqa: BLE001
            logger.debug("span flush 失败（已忽略）：%s", exc)

    def shutdown(self, *, timeout_ms: int = 5000) -> None:
        try:
            self.provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.debug("span shutdown 失败（已忽略）：%s", exc)

    def reset(self) -> None:
        """清空 span 记录与指标序列（测试用；**不动 provider**）。"""
        self.exporter.clear()
        self.metrics.reset()


def _span_kind(kind: str) -> SpanKind:
    """本项目的 `kind` 字符串 → OTEL 的 `SpanKind`。

    只有三种（刻意不暴露 OTEL 的全集）：`internal` 是默认（节点、工具），
    `client` 表示「我们发出去的调用」（LLM/检索/SQL —— 它们的耗时主要是**别人的**），
    这个区分在 trace UI 里决定「这一步该不该算进自己的服务耗时」。
    """
    return SpanKind.CLIENT if kind in ("client", "llm", "tool", "retrieval") else SpanKind.INTERNAL


def _duration_series(name: str, kind: str) -> str | None:
    """span 名 → 该记哪个耗时直方图（**只有一处映射**）。

    `11.observability.md` §2 的四个耗时指标在这里被一一对应上：
    `AGENT_DURATION` / `TOOL_DURATION` / `LLM_DURATION` / `SQL_DURATION`，
    外加第 10 步的检索（`RETRIEVAL_DURATION`）。**没有前缀的 span 不记耗时指标** ——
    否则「什么都不是」的 span（比如 `http.*`，它的耗时由中间的直方图记）会凭空多一条序列。
    """
    if name.startswith(LLM_PREFIX):
        return LLM_DURATION
    if name.startswith(RETRIEVAL_PREFIX):
        return RETRIEVAL_DURATION
    if name.startswith(SQL_PREFIX):
        return SQL_DURATION
    if name.startswith(TOOL_PREFIX):
        return TOOL_DURATION
    if name.startswith(NODE_PREFIX):
        return AGENT_DURATION
    del kind
    return None


def _duration_labels(name: str) -> dict[str, str]:
    """span 名 → 耗时直方图的标签（**只有一处映射**，与 `_duration_series` 同源）。

    一个 span 只会落到**一个**系列上：`node.*` 全是 Agent 节点，
    `tool.*` 全是工具，`sql.*` 全是数据库查询，`retrieval.*` 全是检索，
    `llm.*` 无标签（模型在 `LLM_TOKENS` 上，理由见 `trace_llm`）。
    """
    if name.startswith(NODE_PREFIX):
        return {"node": name[len(NODE_PREFIX) :]}
    if name.startswith(SQL_PREFIX):
        return {"tool": name[len(SQL_PREFIX) :]}
    if name.startswith(TOOL_PREFIX):
        return {"tool": name[len(TOOL_PREFIX) :]}
    if name.startswith(RETRIEVAL_PREFIX):
        return {"tool": name[len(RETRIEVAL_PREFIX) :]}
    return {}


def _kind_label(name: str, kind: str) -> str:
    """span 名 → `SPAN_ERRORS` 的 `kind` 标签取值（**只有一处映射**）。"""
    for prefix, label in (
        (NODE_PREFIX, "node"),
        (SQL_PREFIX, "sql"),
        (TOOL_PREFIX, "tool"),
        (LLM_PREFIX, "llm"),
        (RETRIEVAL_PREFIX, "retrieval"),
    ):
        if name.startswith(prefix):
            return label
    return kind or "internal"


# ── 三个埋点入口（`11.observability.md` §2 的对外接口）─────────────────────────

#: 当前活跃的 telemetry（**进程一份**；`None` = 还没配，第一次访问时懒建）。
_DEFAULT: Telemetry | None = None
_DEFAULT_LOCK = threading.Lock()

#: 「本次请求的 telemetry 根」。
#:
#: ⚠️ 为什么需要它（本步实测踩出来的设计）：`_DEFAULT` 是**进程级**的，
#: 而指标注册表是**按 app 注入**的（`create_app(metrics=...)`，第 9 步的接缝）。
#: 若埋点永远写进程级那一份，`/metrics`（渲染 app 那一份）就**永远看不到节点级指标** ——
#: 这个 bug 的形态很隐蔽：指标确实产生了，只是进了另一个没人抓取的 registry。
#: `ContextVar` 让「这次请求用哪一份 telemetry」跟着请求走：
#: host 侧（`app/api/main.py` 的 lifespan）设一次，请求内的所有埋点（含
#: `asyncio.create_task` 出来的子任务）都能解析到同一份。
_REQUEST_ROOT: contextvars.ContextVar["Telemetry | None"] = contextvars.ContextVar(
    "shopsage_telemetry_root", default=None
)


def configure_telemetry(
    *,
    enabled: bool = True,
    service_name: str = "shopsage",
    otel_endpoint: str = "",
    console: bool = False,
    max_records: int = DEFAULT_MAX_RECORDS,
    metrics: Metrics | None = None,
) -> Telemetry:
    """装配（或替换）进程的 telemetry，返回它。

    三条**必须在装配期决定**的事（而不是每请求、更不是 import 期）：

    * `enabled`：关掉时全部退化成 no-op（零开销、零记录）—— 评测/压测对照需要它；
    * `otel_endpoint`：**空 = 只留内存 exporter**。这一点很重要：开发机上没有 collector，
      若空端点也去建 OTLP exporter，每个请求都会在后台重试连接并超时
      （症状是「服务越跑越慢」，而原因在监控上）；
    * `console`：span 打到控制台只在本机排障时开（否则日志会被 span 淹掉）。
    """
    global _DEFAULT
    provider = TracerProvider(
        resource=Resource.create({"service.name": str(service_name), "service.namespace": "shopsage"})
    )
    exporter = MemorySpanExporter(max_records=max_records)
    # ⚠️ 内存 exporter 用 **SimpleSpanProcessor**（同步导出）：
    #    BatchSpanProcessor 是异步的，测试里得 `force_flush()` 才能看见 span
    #    —— 「忘了 flush 就断言不到」是一类纯粹由测试装置引入的假失败。
    #    代价是每条 span 结束时同步跑一次 export（这里是内存 append，微秒级），
    #    对**生产**的影响也极小；真正要出网时由 OTLP 那一路负责（它用 Batch）。
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    if otel_endpoint:
        _add_otlp(provider, otel_endpoint)

    if console:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    telemetry = Telemetry(
        provider=provider,
        metrics=metrics or default_metrics(),
        exporter=exporter,
        enabled=bool(enabled),
        service_name=str(service_name),
        otel_endpoint=str(otel_endpoint),
        console=bool(console),
    )
    # ⚠️ **这里刻意不加锁**（本步实测踩到过死锁，值得写下来）：
    #     `telemetry()` 会在**持有 `_DEFAULT_LOCK` 的情况下**调用本函数来懒建，
    #     而 `threading.Lock` **不可重入** —— 本函数再取一次锁就是自己等自己，
    #     表现为「第一次埋点调用永久挂住」（不是报错，是**没有输出地卡死**，
    #     实测用例直接超时 150s 才发现）。修法有两种：换成 `RLock`（掩盖问题），
    #     或者把锁的范围收窄到「只保护赋值」（本实现）。
    #     单次属性赋值在 CPython 里是原子的，读方也不做「读-改-写」，
    #     所以「不加锁」不会丢更新；而 `_DEFAULT_LOCK` 仍然保护下面两个
    #     真正做「读-改-写」的函数（`set_telemetry` / `use_telemetry`）。
    global _DEFAULT
    _DEFAULT = telemetry
    logger.info(
        "telemetry 就绪：service=%s enabled=%s endpoint=%r（空 = 只留内存 exporter）",
        telemetry.service_name,
        telemetry.enabled,
        telemetry.otel_endpoint,
    )
    return telemetry


def _add_otlp(provider: TracerProvider, endpoint: str) -> None:
    """挂 OTLP/HTTP exporter（**失败只告警**：监控坏了不能让服务起不来）。

    用 `BatchSpanProcessor` 而不是 Simple：出网是慢的（几十~几百毫秒），
    同步导出会让**每个请求**都多等一个网络往返 —— 那是「监控拖慢业务」的经典形态。

    ⚠️ **端点要写全路径**（`http://host:4318/v1/traces`），而 `.env` 里常见的
    `http://host:4317` 是 **gRPC** 的默认端口、且没有路径。两者混起来不会报错，
    只会**静默不收数据**（exporter 把 `/` 当路径发过去，collector 返回 404，
    而 SDK 只在 DEBUG 级别记这件事）。所以这里做两件兜底：
    ① 缺路径时补 `/v1/traces`；② 端口是 4317（gRPC 端口）时告警提示应该用 4318。
    """
    url, warning = normalize_otlp_endpoint(endpoint)
    if warning:
        logger.warning("%s", warning)
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=url)))
        logger.info("OTLP/HTTP span 导出已启用：%s", url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OTLP exporter 装配失败（链路仍会记在内存里，只是不外发）：endpoint=%r error=%s: %s",
            endpoint,
            type(exc).__name__,
            exc,
        )


def normalize_otlp_endpoint(endpoint: str) -> tuple[str, str]:
    """把配置里的 OTEL 端点规整成 OTLP/**HTTP** 的完整 URL，返回 `(url, 告警文本)`。

    两种输入都接受（配置是外部输入，不该要求用户记住协议细节）：
    * `http://host:4318` → `http://host:4318/v1/traces`
    * `http://host:4318/v1/traces` → 原样

    ⚠️ 4317 是 OTLP/**gRPC** 的端口。用它配 HTTP exporter 的后果不是报错，
    而是「trace 一条都不到」—— 本仓把它变成一条**启动期 WARNING**（能照着改）。
    """
    text = str(endpoint or "").strip()
    if not text:
        return "", ""
    warning = ""
    if ":4317" in text and "/v1/traces" not in text:
        warning = (
            f"otel_endpoint={text!r} 用的是 4317（OTLP/gRPC 的默认端口），"
            "而本项目用的是 OTLP/**HTTP** exporter —— 请改成 4318（HTTP 端口），"
            "否则 trace 会一条都不到（且不会报错）。"
        )
    if not text.rstrip("/").endswith("/v1/traces"):
        text = text.rstrip("/") + "/v1/traces"
    return text, warning


def telemetry() -> Telemetry:
    """取当前生效的 telemetry：**请求根优先**，否则用进程默认；都没有就懒建一个。

    懒建（而不是 import 期建）的理由与 `metrics.default_metrics()` 相同：
    `import app.observability` 不该产生任何全局副作用 ——
    否则「跑一条离线用例」会顺带建一个 TracerProvider 并常驻后台线程。

    ⚠️ **本函数在 `_DEFAULT_LOCK` 之外调用 `configure_telemetry()`**（本步实测踩到死锁）：
    两个函数都要写 `_DEFAULT`，若都在锁里写就是「持有不可重入锁再取同一把锁」，
    表现为**第一次埋点调用永久挂住**（没有异常、没有输出）。
    所以锁的范围是「只保护读-改-写」，而 `configure_telemetry()` 里的赋值
    在 CPython 下是原子的（它是唯一的写方）。
    """
    root = _REQUEST_ROOT.get()
    if root is not None:
        return root
    existing = _DEFAULT
    if existing is not None:
        return existing
    return configure_telemetry()


def set_request_root(value: Telemetry) -> contextvars.Token:
    """把 `value` 设成**当前上下文**的 telemetry 根（返回 token，供 `reset_request_root`）。

    典型调用点是 `app/api/main.py` 的 lifespan：一个 app 一份 telemetry，
    于是「这个 app 的 `/metrics`」与「这个 app 的埋点」必然是同一份数据。
    """
    return _REQUEST_ROOT.set(value)


def reset_request_root(token: contextvars.Token) -> None:
    """撤销 `set_request_root`（`finally` 里调；token 不匹配时**不抛**）。"""
    try:
        _REQUEST_ROOT.reset(token)
    except (ValueError, RuntimeError) as exc:
        # ContextVar 的 token 只能在同一上下文里 reset。跨上下文 reset 会抛
        # `ValueError: <Token ...> was created in a different Context` ——
        # 而 shutdown 路径上抛异常会让「关不掉的 app」变成一次真实故障，
        # 所以这里只记 debug 日志（埋点的清理不该有权打断关闭流程）。
        logger.debug("telemetry 根复位失败（已忽略）：%s", exc)


def reset_telemetry() -> None:
    """丢弃进程 telemetry（测试用；下一个 `telemetry()` 会重建）。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None


def set_telemetry(value: Telemetry | None) -> None:
    """直接替换进程 telemetry（测试与演示脚本注入自己的实例）。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = value


@contextmanager
def use_telemetry(value: Telemetry) -> Iterator[Telemetry]:
    """`with` 块内所有埋点都进 `value`（块结束后恢复原值）。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        previous = _DEFAULT
        _DEFAULT = value
    try:
        yield value
    finally:
        with _DEFAULT_LOCK:
            _DEFAULT = previous


@contextmanager
def trace_span(
    name: str, *, kind: str = "internal", attributes: Mapping[str, Any] | None = None
) -> Iterator[TraceHandle]:
    """开一个 span（进程 telemetry 版；等价于 `telemetry().span(...)`）。"""
    with telemetry().span(name, kind=kind, attributes=attributes) as handle:
        yield handle


@contextmanager
def trace_llm(model: str, *, attributes: Mapping[str, Any] | None = None) -> Iterator[TraceHandle]:
    """LLM 调用的 span（名字固定 `llm.call`，模型放属性里）。

    为什么模型不放进 span 名：**模型是标签，不是名字**。放进名字会让
    `llm.call.deepseek-chat` 与 `llm.call.gpt-4o` 变成两个不同的操作名 ——
    于是「所有 LLM 调用的 P99」在 trace UI 里再也聚合不起来
    （那正是 `11.observability.md` §5「分位数比均值重要」要回答的问题之一）。
    """
    with telemetry().span(
        f"{LLM_PREFIX}call", kind="llm", attributes={"model": model, **(attributes or {})}
    ) as handle:
        yield handle


@contextmanager
def trace_retrieval(query_len: int, *, attributes: Mapping[str, Any] | None = None) -> Iterator[TraceHandle]:
    """向量/混合检索的 span（`query_len` 而不是 query 本身：**问题文本不进 trace**）。"""
    with telemetry().span(
        f"{RETRIEVAL_PREFIX}search",
        kind="retrieval",
        attributes={"hits": None, "candidates": query_len, **(attributes or {})},
    ) as handle:
        yield handle


@contextmanager
def trace_tool_call(tool: str, *, attributes: Mapping[str, Any] | None = None) -> Iterator[TraceHandle]:
    """工具调用的 span（`with` 形式，见模块 docstring 的三种方式）。"""
    with telemetry().span(
        f"{TOOL_PREFIX}{tool}", kind="tool", attributes={"tool": tool, **(attributes or {})}
    ) as handle:
        yield handle


@contextmanager
def trace_sql(tool: str = "sql_query", *, attributes: Mapping[str, Any] | None = None) -> Iterator[TraceHandle]:
    """SQL 执行的 span（`sql.` 前缀 → 耗时进 `SQL_DURATION`，与 `TOOL_DURATION` 分开）。"""
    with telemetry().span(
        f"{SQL_PREFIX}{tool}", kind="tool", attributes={"tool": tool, **(attributes or {})}
    ) as handle:
        yield handle


# ── 装饰器形式 ───────────────────────────────────────────────────────────────
#
# 三个装饰器（`trace_node` / `trace_tool` / `trace_llm`）**必须同时吃同步与异步函数**：
# 被包的对象里既有 `async def`（六个 Agent 节点、LLM 门面）也有 `def`（多数工具、SQL 执行器），
# 而「装饰器只支持一种」的代价是调用方要么改写签名要么漏掉埋点。
# 所以这里按 `inspect.iscoroutinefunction` 分派，且**保留原函数的签名与元数据**（functools.wraps）
# —— `safe_node` 靠 `fn.__name__` / `fn.__node_name__` 认节点，包一层把名字丢掉会让兜底策略认错节点。


def _decorate(prefix: str, label: str) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """造一个 `trace_xxx(label)` 装饰器（三个入口共用一份实现，避免三处漂移）。"""

    def factory(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = f"{prefix}{label}"
        attributes = {prefix.rstrip("."): label}

        if inspect.iscoroutinefunction(fn):

            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with telemetry().span(name, kind=prefix.rstrip("."), attributes=attributes):
                    return await fn(*args, **kwargs)

            async_wrapper.__name__ = getattr(fn, "__name__", name)
            async_wrapper.__doc__ = fn.__doc__
            async_wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
            async_wrapper.__node_name__ = getattr(fn, "__node_name__", label)  # type: ignore[attr-defined]
            return async_wrapper

        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with telemetry().span(name, kind=prefix.rstrip("."), attributes=attributes):
                return fn(*args, **kwargs)

        sync_wrapper.__name__ = getattr(fn, "__name__", name)
        sync_wrapper.__doc__ = fn.__doc__
        sync_wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        sync_wrapper.__node_name__ = getattr(fn, "__node_name__", label)  # type: ignore[attr-defined]
        return sync_wrapper

    return factory


def trace_node(label: str) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """把一次 Agent 节点执行记成一个 span（+ `AGENT_DURATION` 观测）。"""
    return _decorate(NODE_PREFIX, label)


def trace_tool(label: str) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """把一次工具调用记成一个 span（+ `TOOL_DURATION` 观测）。"""
    return _decorate(TOOL_PREFIX, label)


def trace_llm_decorator() -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """装饰器版的 LLM 埋点（名字里带 `_decorator` 是为了**不与 `trace_llm` 上下文管理器撞名**）。

    ⚠️ 与 `trace_node` / `trace_tool` 不同，它**不吃 label**：模型名要从**调用时**的参数里取
    （`llm_call` 的模型缺省值来自 settings），装饰期根本不知道会打哪个模型 ——
    在装饰期写死一个模型名，正是「看板上多了个叫 default 的模型」这类脏数据的来源。

    生产路径用的是 `llm_call` 内部的 `trace_llm(model)` 上下文管理器（那里才知道最终模型），
    本装饰器留给「已经知道模型」的场合（评测脚本、包一个写死了模型的函数）。

    ⚠️ 为什么不把 `trace_llm` 本身做成「既能当装饰器又能当上下文管理器」的双面对象：
    那种对象（常见写法是只实现 `__enter__`/`__call__`）在**异步**里不能用 `async with`，
    而 LLM 调用恰好全是异步的 —— 一个「看起来更省事、实际用不了」的接口比两个明确的接口差。
    """
    return _decorate(LLM_PREFIX, "call")


def record_request_span(endpoint: str, *, attributes: Mapping[str, Any] | None = None) -> None:
    """开一条**独立的** `http.<endpoint>` span（给「只有一个事后数字」的调用方）。

    ⚠️ **不要用它来做请求的根 span**：它一条即开即关，所以不可能成为别的 span 的父节点
    （用它做根的结果是「每个节点各自一个 trace」）。请求的根 span 用
    `instrument.trace_request_span(method, path)` 的 `with` 形式 —— 那一版会**包住**
    整段下游调用，于是下游所有埋点自动成为它的子 span。
    本函数留给脚本、演示与「事后补一条记录」的场合。
    """
    with telemetry().span(f"http.{endpoint}", attributes={"request_id": "", **(attributes or {})}) as handle:
        handle.set_ok()


def count_trace(*, endpoint: str) -> None:
    """给一次完整的请求计数（`TRACE_COUNT`；看板上「有没有流量」的第一个信号）。"""
    default_metrics().counter(TRACE_COUNT, {"endpoint": endpoint})


def count_span_error(*, kind: str) -> None:
    """给一次出错的 span 计数（`SPAN_ERRORS`；按类型分组，便于「是模型挂了还是库挂了」）。"""
    default_metrics().counter(SPAN_ERRORS, {"kind": kind})


__all__ = [
    "ALLOWED_ATTRIBUTES",
    "DEFAULT_MAX_RECORDS",
    "LLM_PREFIX",
    "MemorySpan",
    "MemorySpanExporter",
    "NODE_PREFIX",
    "RETRIEVAL_PREFIX",
    "SQL_PREFIX",
    "TOOL_PREFIX",
    "Telemetry",
    "TraceHandle",
    "configure_telemetry",
    "count_span_error",
    "count_trace",
    "normalize_otlp_endpoint",
    "record_request_span",
    "reset_request_root",
    "reset_telemetry",
    "sanitize_attributes",
    "set_request_root",
    "set_telemetry",
    "telemetry",
    "trace_llm",
    "trace_llm_decorator",
    "trace_node",
    "trace_retrieval",
    "trace_span",
    "trace_sql",
    "trace_tool",
    "trace_tool_call",
    "use_telemetry",
]
