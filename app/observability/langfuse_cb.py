"""Langfuse 接入：**LLM 专用**的 token / 成本 / 质量记录（`11.observability.md` §2）。

三件工具的边界（文档 §4 第 2 条）在这里体现得最直接：本模块**只记 LLM 相关的东西**。
它记录的不是「服务有多快」（那是 Prometheus），也不是「调用链长什么样」（那是 OTEL）——
而是「这一次模型调用花了多少 token、多少钱、答案质量几分」。

## 为什么不是 LangChain 的 `CallbackHandler`

`11.observability.md` §2 写的是 `from app.observability.langfuse_cb import langfuse_handler`。
名称保留（它是契约），但**接口形态不同**，原因是一个必须写清楚的版本事实：

> **Langfuse 4.x 把 LangChain 集成搬到了 `langfuse.langchain`**（旧的 `langfuse.callback`
> 路径已不存在），而且它**只在装了顶层 `langchain` 包时才可导入** ——
> 本仓只装了 `langchain-core`（六个 Agent 是手写函数，没有 `Runnable`），
> 所以在模块顶层 import 它就会**直接让 `import app.observability` 失败**。

本仓没有 LangChain 的链（LLM 走自建门面 `app.llm.llm_call`），`CallbackHandler` 那套
**本来也用不上** —— 它要挂在 `Runnable.invoke(callbacks=[...])` 上，而本项目一个
`Runnable` 都没有。于是这里落成一个**与调用点无关的门面**：

```python
handler = langfuse_handler()                      # 没配密钥 = 关闭，零开销
with handler.record(model="deepseek-chat") as rec:  # 也可以不写 with（见下）
    rec.tokens(prompt=1200, completion=380, total=1580)
    rec.cost(0.00042)
    rec.quality(name="answer_relevance", value=0.86)   # 配合 14.eval.md
# 退出 with 时：一次上报 + 一条 score（**后台线程**，不阻塞请求）
```

### 四个必须成立的性质（每一条都是实测踩出来的）

1. **没配密钥就完全关闭**。`Langfuse(public_key="", ...)` 会打一条 WARNING 并把自己禁用
   （好），但 **`LANGFUSE_PUBLIC_KEY=""` 走环境变量时会绕过 `is None` 判断、建出一个
   真的会连网重试的 client**（坏）。所以本模块的做法是：**密钥不全就根本不构造 client**，
   连环境变量都不看 —— 这条边界只有一处，写在这里。

2. **永不阻塞请求路径**。实测（离线、端点不可达）：`flush()` 最短 2.0s、最长 23.6s，
   记过 score 之后 6~13s —— **不是**「几十毫秒」。所以本模块的 `record()` 只登记一次，
   网络导出交给 SDK 的后台线程 + `atexit`，且**超时显式调到 2s**（默认 5s 更久）。

3. **永不抛异常**。LLM 记录失败不能影响这次调用 —— 它是旁路（与 `tracing`/`metrics` 同一条纪律）。

4. **trace id 必须真的传下去，而且要校验形状**。
   Langfuse 4.x 的 `trace_context` 在**非十六进制**的 id 上会 `int(x, 16)` 并抛
   `ValueError`（实测：`"abc123defg"`、带横杠的 UUID 都直接崩）；
   而**短十六进制**（`"abc123"`）会被静默左补零，得到一条**对不上账**的 trace。
   所以 `_trace_context()` 用 `^[0-9a-f]{32}$` 严格校验，不合格就**不传**
   （宁可少一次关联，也不能让埋点把请求打崩）。

## 「质量」记在哪

`quality()` 写的是 Langfuse 的 **score**（不是 metadata）：
score 是 Langfuse 里**能聚合、能画趋势、能按数据集筛选**的一等公民，
而 metadata 只是挂在单条 trace 上的一段 JSON。把「答案质量 0.86」写进 metadata，
等于把它变成一条只能人肉翻看的记录 —— 与 `14.eval.md` 要对齐「成本 vs 质量」的目标直接冲突。
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 保留多少条离线记录（**必须有上限**，理由同 `tracing.MemorySpanExporter`）。
DEFAULT_MAX_FINDINGS: Final[int] = 500

#: 质量分的合法区间（Langfuse 的 NUMERIC score 约定是 [0,1]）。
QUALITY_RANGE: Final[tuple[float, float]] = (0.0, 1.0)

#: Langfuse 只接受**32 位小写十六进制**的 trace id（见模块 docstring 第 4 条：
#: 不合格的 id 会让 SDK 抛 `ValueError` 或静默补零，两者都比「不关联」更糟）。
#:
#: ⚠️ OTEL 的 trace id 是按 32 位十六进制格式化的（`format(trace_id, "032x")`），
#: 所以**我们自己产生的** id 永远合格；这条正则是为了挡住外部传进来的 id
#: （例如某个调用方拿自己的 request_id 来当 trace id）。
TRACE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")

#: 上报超时（秒）。**显式设成 2s**：SDK 默认 5s，而实测 `flush()` 的阻塞时间随它线性放大
#: （timeout=1 → 2.0s，timeout=5 → 4.1s，timeout=30 → 23.6s）。
#: 这个值只影响**可达但很慢**的端点；不可达时是连接拒绝（快失败）。
LANGFUSE_TIMEOUT_SECONDS: Final[int] = 2


@dataclass
class LLMFinding:
    """一次 LLM 调用的完整账目（**本模块的对外数据契约**）。

    为什么要有这个结构而不是「直接往 Langfuse 发」：
    ① 它让「记录了什么」**离线可断言**（测试不需要一个真的 Langfuse 服务）；
    ② 它是「成本/质量」这条链路的**唯一形状**，`13.cache.md` / `14.eval.md` 对比时
       不用各自去猜字段名；
    ③ 它也是 `log` 出口（没配 Langfuse 时，这条记录仍然值得进日志/进 trace 属性）。
    """

    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0
    #: trace id（与 OTEL 的 32 位十六进制 id 是**同一个** —— 这样两个系统能对上账）
    trace_id: str = ""
    observation_id: str = ""
    name: str = "llm.call"
    user_id: str = ""
    session_id: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: 是否真的发到了 Langfuse（没配密钥 / 发送失败时是 False）
    exported: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "trace_id": self.trace_id,
            "observation_id": self.observation_id,
            "name": self.name,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "scores": dict(self.scores),
            "metadata": dict(self.metadata),
            "exported": self.exported,
        }


class LLMRecorder:
    """一次 LLM 调用的记录器（`with` 块内填账目，退出时落地）。

    **不写 `with` 也能用**：直接调 `rec.tokens(...)` 等方法即可，
    落地由 `flush()` / 进程退出时统一完成。这一点是刻意的 ——
    生产路径（`app.llm.client`）在 `finally` 里记录，包成 `with` 会把
    「记录」与「异常处理」的代码搅在一起，而后者才是那个函数的主体。
    """

    def __init__(self, handler: LangfuseHandler, finding: LLMFinding) -> None:
        self._handler = handler
        self.finding = finding
        #: 已经落地过（`done()` **幂等**的理由见下）。
        self._submitted = False

    # ── 填账目 ───────────────────────────────────────────────────────────────
    def tokens(self, *, prompt: int = 0, completion: int = 0, total: int = 0) -> LLMRecorder:
        """记 token。`total` 单独传（某些 provider 的 total ≠ prompt + completion）。"""
        self.finding.prompt_tokens = int(prompt or 0)
        self.finding.completion_tokens = int(completion or 0)
        self.finding.total_tokens = int(total or 0) or (
            self.finding.prompt_tokens + self.finding.completion_tokens
        )
        return self

    def cost(self, cost_usd: float | None) -> LLMRecorder:
        """记成本。`None` = **未知**（litellm 的定价表里没有这个模型）。

        ⚠️ 未知**不等于 0**：把未知记成 0 会让成本看板「看起来完整、实际偏低」，
        而偏低的成本不会触发任何告警（见 `metrics.record_cost` 的同一条理由）。
        """
        self.finding.cost_usd = None if cost_usd is None else float(cost_usd)
        return self

    def quality(self, *, name: str, value: float) -> LLMRecorder:
        """记一次质量分（`14.eval.md` 的评测分、或人工评分）。

        超出 `[0,1]` 的值被**夹住**而不是抛错：Langfuse 的 NUMERIC score 有这个约定，
        而一个 1.2 分的质量分在趋势图里会让整条线失真（它会被当成异常值）。
        需要记「原始分」时用 `metadata`（例如 `{"raw_score": 12}`）。
        """
        low, high = QUALITY_RANGE
        self.finding.scores[str(name)] = min(high, max(low, float(value)))
        return self

    def meta(self, **values: Any) -> LLMRecorder:
        """记若干 metadata（**只放不会膨胀的东西**：路由、缓存命中、重试次数）。"""
        for key, value in values.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                self.finding.metadata[str(key)] = value
            else:
                self.finding.metadata[str(key)] = str(value)[:200]
        return self

    # ── 落地 ─────────────────────────────────────────────────────────────────
    def done(self, *, error: BaseException | None = None) -> LLMFinding:
        """结束这次记录并落地（**幂等**：重复调用不会重复上报）。

        为什么必须是幂等的（不是「顺手加的防御」）：生产路径在 `finally` 里记录，
        而 `record()` 的上下文管理器**退出时也会调一次** `done()` ——
        两条路径叠在一起时，一次 LLM 调用会变成 Langfuse 里的**两条** generation，
        于是「成本」与「调用次数」都翻倍（而看板看起来完全正常，只是数字偏大）。
        真实系统里这种「翻倍」极难发现，所以幂等放在这里，而不是靠调用方小心。
        """
        if self._submitted:
            return self.finding
        self._submitted = True
        if error is not None:
            self.finding.metadata.setdefault("error_kind", type(error).__name__)
        return self._handler.submit(self.finding)


class LangfuseHandler:
    """Langfuse 的门面：**没配密钥时是一个安全的空实现**。

    ## 为什么「关闭」是一个显式状态而不是「什么都不做」

    因为「什么都不做」无法被观测：看板上没有数据时，你分不清是
    「没人用」还是「密钥配错了」。所以关闭状态会：
    ① 在 `state` 里如实写出原因（`disabled_reason`）；
    ② 仍然把账目留在 `findings()` 里（离线可断言、可进日志）；
    ③ 在 `setup` 时打**一条** INFO 日志（不是每请求一条 —— 那会刷屏）。
    """

    def __init__(
        self,
        *,
        client: Any = None,
        enabled: bool = False,
        disabled_reason: str = "",
        telemetry: Any = None,
        max_findings: int = DEFAULT_MAX_FINDINGS,
    ) -> None:
        self._client = client
        self.enabled = bool(enabled) and client is not None
        self.disabled_reason = disabled_reason or ("" if self.enabled else "未配置")
        self.telemetry = telemetry
        self._findings: list[LLMFinding] = []
        self._lock = threading.Lock()
        self._max = max(1, int(max_findings))

    # ── 装配 ─────────────────────────────────────────────────────────────────
    @classmethod
    def from_settings(cls, settings: Any, *, telemetry: Any = None) -> LangfuseHandler:
        """按配置造一个 handler。**密钥不全就完全不构造 client**（见模块 docstring 第 1 条）。"""
        public = str(_secret(settings, "langfuse_public_key") or "")
        secret = str(_secret(settings, "langfuse_secret_key") or "")
        host = str(getattr(settings, "langfuse_host", "") or "")

        # 显式关掉（密钥配好了也不想上报）：**先判它**，因为它是「我要 A」的表达，
        # 而「缺密钥」是「我没给 B」的事实 —— 两者同时成立时该听前者。
        if settings is not None and not bool(getattr(settings, "langfuse_enabled", True)):
            logger.info(
                "Langfuse 被 LANGFUSE_ENABLED=false 显式关闭："
                "LLM 的 token/成本/质量仍留在进程内（可查日志），但不外发。"
                "（这条开关的存在理由：SDK 会在进程退出时 flush，而实测最坏要 20s+）"
            )
            return cls(telemetry=telemetry, disabled_reason="LANGFUSE_ENABLED=false")

        if not public or not secret:
            logger.info(
                "Langfuse 未启用（缺 %s）：LLM 的 token/成本/质量记录仍会留在进程内（可查日志），"
                "但不外发。设置 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 即可启用。",
                "public_key" if not public else "secret_key",
            )
            return cls(telemetry=telemetry, disabled_reason="缺 public_key 或 secret_key")

        try:
            from langfuse import Langfuse

            # ⚠️ `tracer_provider=telemetry.provider` 是「两个系统对得上账」的关键：
            # Langfuse 4.x 本身就是 OTEL 的实现，共用 provider 之后，
            # 一次 LLM 调用在 Langfuse 里是 trace 的一个**子节点**（能顺着调用链看上下文），
            # 而不是一条孤立的记录。不共用时两边的 trace_id 不同，永远对不上账。
            #
            # ⚠️ 代价必须写清楚：共用 provider 意味着**两个 exporter 挂在同一个 provider 上**
            # （我们的内存/OTLP + Langfuse 自己的）。这是刻意的（要的就是同一棵 trace），
            # 但也意味着「Langfuse 端不可达」会让我们这一侧也多一条失败重试路径 ——
            # 所以它必须与 `otel_endpoint` 一样被显式配置，而不是默认打开。
            client = Langfuse(
                public_key=public,
                secret_key=secret,
                host=host or None,
                tracing_enabled=True,
                timeout=LANGFUSE_TIMEOUT_SECONDS,
                tracer_provider=getattr(telemetry, "provider", None),
            )
        except Exception as exc:  # noqa: BLE001 - 监控坏了不能让服务起不来
            logger.warning(
                "Langfuse client 构造失败（LLM 记录只在进程内保留）：%s: %s", type(exc).__name__, exc
            )
            return cls(telemetry=telemetry, disabled_reason=f"client 构造失败：{type(exc).__name__}")

        logger.info("Langfuse 已启用：host=%s（token/成本/质量会上报）", host or "默认")
        return cls(client=client, enabled=True, telemetry=telemetry)

    # ── 记录 ─────────────────────────────────────────────────────────────────
    @contextmanager
    def record(
        self,
        *,
        model: str = "",
        name: str = "llm.call",
        user_id: str = "",
        session_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> Iterator[LLMRecorder]:
        """开一条 LLM 记录（`with` 形式；退出时落地，异常也会落地）。"""
        finding = LLMFinding(
            model=str(model),
            name=str(name),
            user_id=str(user_id),
            session_id=str(session_id),
            trace_id=self._current_trace_id(),
        )
        for key, value in (metadata or {}).items():
            finding.metadata[str(key)] = value if isinstance(value, (str, int, float, bool)) else str(value)[:200]
        recorder = LLMRecorder(self, finding)
        try:
            yield recorder
        finally:
            recorder.done()

    def open(
        self, *, model: str = "", name: str = "llm.call", **kwargs: Any
    ) -> LLMRecorder:
        """不开 `with` 的记录器（生产路径用这个，见 `LLMRecorder` 的说明）。"""
        del kwargs
        return LLMRecorder(self, LLMFinding(model=str(model), name=str(name), trace_id=self._current_trace_id()))

    def submit(self, finding: LLMFinding) -> LLMFinding:
        """把一条账目落地（**永不抛、永不阻塞**，理由见模块 docstring）。"""
        with self._lock:
            self._findings.append(finding)
            if len(self._findings) > self._max:
                del self._findings[: len(self._findings) - self._max]

        if not self.enabled:
            return finding

        try:
            self._export(finding)
            finding.exported = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse 上报失败（已忽略，账目仍在进程内）：%s: %s", type(exc).__name__, exc)
        return finding

    def _export(self, finding: LLMFinding) -> None:
        """真正发给 Langfuse（一次 observation + 若干 score）。

        ⚠️ 用 `start_as_current_observation(as_type="generation")` 而不是
        「自己拼一段 HTTP 请求」：generation 是 Langfuse 里**唯一**会去做
        「成本/延迟/质量」聚合的节点类型，用 span 发过去在 UI 上只是一条通用节点
        （数据在，但看板的「Cost」「Latency」面板是空的）。

        ⚠️ 必须用**同步** `with`（`async with` 在 4.17.0 上会抛
        `TypeError: '_AgnosticContextManager' object does not support the
        asynchronous context manager protocol` —— 实测）。本函数在 async 代码里被调用，
        但这里的上下文管理器是同步的，这是**有意的**：报告本身不需要 await。
        """
        with self._client.start_as_current_observation(
            name=finding.name,
            as_type="generation",
            model=finding.model or None,
            # `trace_context` 带上 trace id，两个系统才能对上账（见 `_trace_context`）
            trace_context=_trace_context(finding.trace_id),
            metadata=_metadata(finding),
            usage_details=_usage_details(finding),
            cost_details={"total": finding.cost_usd} if finding.cost_usd is not None else None,
        ) as observation:
            finding.observation_id = str(getattr(observation, "id", "") or "")
            # ⚠️ **打分必须在 `with` 块内**：`score_current_trace()` 依附「当前活跃的
            # observation」。在块外它是**静默无效**的 —— 实测它会打一条
            # `Context error: No active span…` 然后直接返回（不抛异常），
            # 于是「质量分」这条链看起来配好了、实际一条都不会到看板上。
            # 这正是本仓最讨厌的一类故障：没有报错，只是数据永远缺失。
            for name, value in finding.scores.items():
                self._client.score_current_trace(name=name, value=value)

    def score(
        self, *, name: str, value: float, trace_id: str = "", comment: str = "", data_type: str = "NUMERIC"
    ) -> bool:
        """给**指定的** trace 打分（`14.eval.md` 的评测回填入口；无 trace 时退回当前 trace）。"""
        if not self.enabled:
            return False
        try:
            kwargs: dict[str, Any] = {"name": str(name), "value": value, "data_type": data_type}
            if comment:
                kwargs["comment"] = str(comment)
            if trace_id:
                kwargs["trace_id"] = str(trace_id)
                self._client.create_score(**kwargs)
            else:
                self._client.score_current_trace(name=str(name), value=value, comment=comment or None)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse 打分失败（已忽略）：%s: %s", type(exc).__name__, exc)
            return False

    # ── 读口（测试与排障用）──────────────────────────────────────────────────
    @property
    def findings(self) -> list[LLMFinding]:
        """当前保留的账目（副本）。"""
        with self._lock:
            return list(self._findings)

    def clear(self) -> None:
        with self._lock:
            self._findings.clear()

    @property
    def state(self) -> dict[str, Any]:
        """对外可观测的状态（`/metrics` 之外的自检出口）。"""
        return {
            "enabled": self.enabled,
            "reason": self.disabled_reason,
            "findings": len(self.findings),
            "host": str(getattr(self._client, "_base_url", "") or "") if self.enabled else "",
        }

    def flush(self, *, timeout_ms: int = 5000) -> None:
        """把还没发出去的记录推出去（**只在关服前调**；请求路径上绝对不要调）。

        ⚠️ **这是个会阻塞的调用**，而且比直觉久得多。实测（端点不可达、离线）：
        最短 2.0s、最长 23.6s；记过 score 之后 6~13s（score 走的是 SDK 内部
        **另一条无上限的队列**，不受 `timeout` 参数约束）。所以：

        * 它**不允许**出现在任何请求路径上（把 2s 加到一个请求上就是一次可用性事故）；
        * `timeout_ms` **只是给调用方的语义提示**，SDK 没有对应参数可以直接传 ——
          真正的上限由构造时的 `timeout=`（本模块设为 2s）与 exporter 自己的重试决定。
          这里如实说明，而不是假装这个参数生效（第一版就是假装了）。
        """
        if not self.enabled:
            return
        if timeout_ms <= 0:  # 显式的「不想等」：跳过（调用方自己知道丢一点数据）
            logger.info("Langfuse flush 被跳过（timeout_ms<=0）：可能丢掉未发送的记录")
            return
        try:
            self._client.flush()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Langfuse flush 失败（已忽略）：%s", exc)

    def shutdown(self) -> None:
        if not self.enabled:
            return
        try:
            self._client.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Langfuse shutdown 失败（已忽略）：%s", exc)

    def _current_trace_id(self) -> str:
        """当前 OTEL trace id（没有活跃 span 时为空）。

        为什么要带它：Langfuse 里的记录与 OTEL 的 trace 是**同一次请求**，
        排障时「Grafana 上 P99 高 → 拿 trace id 去 Langfuse 看是哪次调用贵」
        这条路径全靠这个字段。
        """
        try:
            from opentelemetry import trace as otel_trace

            context = otel_trace.get_current_span().get_span_context()
            if not getattr(context, "is_valid", False):
                return ""
            return format(context.trace_id, "032x")
        except Exception:  # noqa: BLE001
            return ""


def _metadata(finding: LLMFinding) -> dict[str, Any] | None:
    """observation 的 metadata（含延迟、用户、会话）。

    ⚠️ **延迟放 metadata 而不是某个「latency 字段」**：Langfuse 的延迟面板读的是
    **span 的时长**，而不是我们传进去的数字。本模块的 span 只包住「上报」这一段
    （因此它自己的时长接近 0）—— 那是**有意的**：真正被计时的是
    `app/llm/client.py` 里的 `llm.call` span（那里量的是模型调用本身）。
    把 `latency_ms` 放进 metadata 是为了让这条记录**自解释**（不用对着一棵 trace 反推）。
    """
    meta: dict[str, Any] = {**finding.metadata, "latency_ms": finding.latency_ms}
    if finding.user_id:
        meta["user_id"] = finding.user_id
    if finding.session_id:
        meta["session_id"] = finding.session_id
    return meta or None


def _usage_details(finding: LLMFinding) -> dict[str, int] | None:
    """token 的三种口径（Langfuse 认 `input`/`output`/`total` 这组别名）。

    `total` 缺省时才用两者相加兜底 —— 与 `metrics.record_tokens` 同一条纪律：
    provider 的 `total` 可能含 reasoning / 缓存 token，自己加会与账单对不上。
    """
    if not (finding.prompt_tokens or finding.completion_tokens or finding.total_tokens):
        return None
    return {
        "input": finding.prompt_tokens,
        "output": finding.completion_tokens,
        "total": finding.total_tokens or (finding.prompt_tokens + finding.completion_tokens),
    }


def _trace_context(trace_id: str) -> dict[str, str] | None:
    """trace id → Langfuse 的 `trace_context`；**不合格就返回 None**（见模块 docstring 第 4 条）。

    三条实测出来的规矩：

    * `^[0-9a-f]{32}$` 之外的任何东西（含 UUID、含非十六进制字符、大写）都**不传**：
      非十六进制的会 `ValueError`（把一次正常的 LLM 调用变成 500），
      短十六进制的会被静默左补零（得到一条对不上账的 trace —— 比不关联更坏，
      因为看板上会有两条都「看起来对」的 trace）；
    * 空字符串 = 当前没有活跃 span（没有可关联的 trace）→ 不传；
    * 传的是**十六进制字符串**，不是 int（`int` 会丢前导零，32 位 id 变 31 位就非法了）。
    """
    text = str(trace_id or "")
    if not TRACE_ID_RE.match(text):
        return None
    return {"trace_id": text}


def _secret(settings: Any, name: str) -> str:
    """从 settings 里取一个可能是 `SecretStr` 的值（**不要打印它**）。"""
    value = getattr(settings, name, "") if settings is not None else ""
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter() or "")
    return str(value or "")


#: 进程级默认 handler（`None` = 还没配，第一次访问时懒建一个**关闭**的）。
_DEFAULT: LangfuseHandler | None = None
_DEFAULT_LOCK = threading.Lock()


def configure_langfuse(settings: Any = None, *, telemetry: Any = None) -> LangfuseHandler:
    """按配置装配进程默认 handler（`setup_observability()` 会调它）。"""
    global _DEFAULT
    handler = LangfuseHandler.from_settings(settings, telemetry=telemetry)
    with _DEFAULT_LOCK:
        _DEFAULT = handler
    return handler


def set_langfuse_handler(handler: LangfuseHandler | None) -> None:
    """直接替换进程默认 handler（测试注入用）。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = handler


def langfuse_handler() -> LangfuseHandler:
    """取进程默认 handler（**从未配置过**时返回一个关闭的空实现）。

    ⚠️ 与 `telemetry()` 的区别：这里**不主动读 settings 建 client**。
    理由：本函数会在**每次 LLM 调用**里被调到，而 `from_settings` 可能真的建一个
    连外网的 client。让「谁调用」决定「什么时候建」是错的 ——
    装配是 `setup_observability()` 的事，本函数只负责取。
    """
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            return LangfuseHandler(disabled_reason="尚未 configure_langfuse（未走 setup_observability）")
        return _DEFAULT


__all__ = [
    "DEFAULT_MAX_FINDINGS",
    "LANGFUSE_TIMEOUT_SECONDS",
    "QUALITY_RANGE",
    "TRACE_ID_RE",
    "LLMFinding",
    "LLMRecorder",
    "LangfuseHandler",
    "configure_langfuse",
    "langfuse_handler",
    "set_langfuse_handler",
]
