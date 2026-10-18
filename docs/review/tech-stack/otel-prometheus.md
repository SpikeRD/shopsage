# OpenTelemetry + Prometheus + Grafana + Langfuse（可观测性）

## 本项目用它解决什么

- **Prometheus**：回答"现在多健康"（请求量、P99、按节点耗时、队列深度、缓存命中）。
- **OTEL**：回答"这一次慢在哪一步"（一棵 span 树）。
- **Langfuse**：回答"每次模型调用花了多少、质量如何"。

## 实际用法（代码片段 + 文件:行号）

### 1）一处装配（lifespan 里，只调用一次）

```python
# app/observability/__init__.py:93
def setup_observability(settings: Any, *, metrics: Any = None) -> Any:
    """指标 + tracer + Langfuse，全部就绪。"""

# app/api/main.py:242
obs = telemetry if telemetry is not None else setup_observability(settings, metrics=app.state.metrics)
app.state.telemetry = obs
telemetry_token = set_request_root(obs)
```

### 2）span 命名规约只在一处

```python
# app/observability/tracing.py:78-89
NODE_PREFIX: Final[str] = "node."
TOOL_PREFIX: Final[str] = "tool."
LLM_PREFIX: Final[str] = "llm."
RETRIEVAL_PREFIX: Final[str] = "retrieval."
SQL_PREFIX: Final[str] = "sql."
```

### 3）节点埋点：唯一入口在 `safe_node`

```python
# app/graph/nodes.py:215-219
with node_span(node_name) as handle:
    handle.set_attributes({"stage": str(state.get("route") or "")})
    return await fn(normalize_input(state))
```

### 4）属性白名单 + 标签基数闸

```python
# app/observability/tracing.py:104
ALLOWED_ATTRIBUTES: Final[frozenset[str]] = frozenset({...})   # prompt 原文进不去

# app/observability/metrics.py:107
LABEL_LIMITS: Final[dict[str, int]] = {"endpoint": 64, "model": 48, "tool": 32, "node": 24, "route": 24}
```

### 5）指标名（`shopsage_` 前缀）

```python
# app/observability/metrics.py:487-554（节选）
REQUEST_COUNT   = f"{NAMESPACE}_requests_total"
AGENT_DURATION  = f"{NAMESPACE}_agent_duration_seconds"     # ← B1 的直接答案
SQL_DURATION    = f"{NAMESPACE}_sql_duration_seconds"
QUEUE_DEPTH     = f"{NAMESPACE}_queue_depth"
CACHE_RESULTS   = f"{NAMESPACE}_cache_results_total"
```

### 6）WebSocket 的根 span 必须自己开

```python
# app/api/chat.py:457
with trace_request_span("WS", "/api/chat/stream"):
    while True: ...
```

### 7）Langfuse：没配密钥就完全不建

```python
# app/observability/langfuse_cb.py:22
handler = langfuse_handler()     # 没配密钥 = 关闭，零开销

# app/observability/langfuse_cb.py:529
def configure_langfuse(settings: Any = None, *, telemetry: Any = None) -> LangfuseHandler: ...
```

## 最小原理

1. **OTEL 的三件套**：`TracerProvider`（谁产 span）、`SpanProcessor/Exporter`（span 送去哪）、`Context`（父子关系靠 contextvars 传递，所以 `await` 期间"当前 span"不变）。
2. **Prometheus 的拉模型**：它来 `GET /metrics` 取文本，所以指标必须由服务自己累积成"当前值"（Counter 单调增、Histogram 是桶计数、Gauge 可上可下）。
3. **分位数不能由应用层算**：Prometheus 用 `histogram_quantile(0.99, rate(bucket[5m]))` 从桶里估，所以**桶边界**决定 P99 的精度（本仓两套桶：`app/observability/metrics.py`）。
4. **标签基数（cardinality）是成本**：每个不同标签组合是一条独立时间序列。把 `user_id` 或 prompt 放进标签 = 序列爆炸，所以有白名单与长度截断。
5. **Langfuse 4.x** 把 LangChain 集成搬到了 `langfuse.langchain`，本仓不用它 —— 自己记录 `LLMFinding`（`app/observability/langfuse_cb.py:91`）。

## 常见坑

| 坑 | 症状 | 依据 |
|---|---|---|
| 只给业务节点埋点，忘了 WS 根 span | 一次连接里每轮各自成一棵"没根的树"，按 trace 查不到 | `app/api/chat.py:446-457` |
| 在 import 期建 `TracerProvider` | 测试收集期的 import 就"起了一套监控" | `app/api/main.py:239-241` |
| 把 prompt 塞进 span 属性 | 基数爆炸 + 泄漏面扩大 | `app/observability/tracing.py:104` |
| 在应用层算 P99 | 与 Prometheus 口径不一致，越看越乱 | `docs/runbook/11.observability-runbook.md` §3.3 |
| 以为 `/health` 会因依赖故障返回 5xx | 它恒 200（有意设计） | `app/api/health.py:9-18` |
| Langfuse 没配密钥就以为"埋点没生效" | 没配 = 关闭且零开销，是设计 | `app/observability/langfuse_cb.py:22` |

## 深入关键词

`TracerProvider` / `SimpleSpanProcessor` / `BatchSpanProcessor` · `OTLPSpanExporter` · `contextvars` 与 span 父子 · `SpanKind` · `prometheus_client` Counter/Histogram/Gauge · `histogram_quantile` · label cardinality · `text/plain; version=0.0.4` · Langfuse `trace` / `generation` / `flush()`
