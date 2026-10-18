# 模块 11 · observability（指标 + 链路 + LLM 记录）

## 目的

回答三个问题：**慢在哪一步**（OTEL span）、**系统当前多健康**（Prometheus 指标）、**每次模型调用花了多少 / 质量如何**（Langfuse）。

## 关键结论

1. **一处装配**：`app/observability/__init__.py:93 setup_observability(settings, metrics=...)` 在 `create_app()` 的 lifespan 里**只调一次**（`app/api/main.py:242`）。放在 lifespan 而不是 import 期，因为 `create_app()` 在模块 import 期就执行了 —— 那时建 `TracerProvider` 会让"import"变成"起监控"。
2. **span 命名规约只有一处**：`node.*` / `tool.*` / `llm.*` / `retrieval.*` / `sql.*`（`app/observability/tracing.py:78-89`）。埋点入口是 `instrument.node_span(node)`（`app/observability/instrument.py:377`），由 `app/graph/nodes.py:215` 统一包住 7 个节点 —— **所以"每个节点都埋点"是结构性成立**，不靠自觉。
3. **属性白名单 + 基数三道闸**（`app/observability/tracing.py:104 ALLOWED_ATTRIBUTES`、`app/observability/metrics.py:107 LABEL_LIMITS`）：prompt 原文这类文本**进不了 span 属性**；标签值超长被截断。
4. **指标是自建 registry 再渲染 Prometheus 文本**：`app/observability/metrics.py`（582 行）定义 17+ 个指标名（`app/observability/metrics.py:487-554`），`/metrics` 由 `app/api/health.py:302` 输出 `text/plain; version=0.0.4`。分位数**只能从桶里算**，不在应用层算。
5. **Langfuse 4.x**：没配密钥 → **关闭且零开销**（`app/observability/langfuse_cb.py:22`）；配了才建 client。之所以不用 LangChain 的 `CallbackHandler`，是因为 4.x 把集成搬到了 `langfuse.langchain`（同文件 :7-12）。
6. **WebSocket 的根 span 要自己开**：`ApiMiddleware` 只管 HTTP（`scope["type"] != "http"` 直接放行），所以 `app/api/chat.py:457` 显式用 `trace_request_span("WS", "/api/chat/stream")` 包住**整条连接的消息循环**。

## 实际用法

```python
# 节点埋点（唯一入口，勿在业务模块里自己开 span）
with node_span("router") as handle:
    handle.set_attributes({"stage": state.get("route") or ""})
    return await fn(state)

# 应用启动时装配（一次）
obs = setup_observability(settings, metrics=app.state.metrics)   # app/api/main.py:242
```

| 指标（`shopsage_` 前缀） | 用途 |
|---|---|
| `requests_total` / `request_duration_seconds` | HTTP 黄金指标 |
| `agent_duration_seconds{node}` | **B1 的直接答案**：按 node 分组看 P99 |
| `tool_duration_seconds` / `sql_duration_seconds` / `retrieval_duration_seconds` | 分层定位 |
| `llm_duration_seconds` / `llm_tokens_total` / `llm_cost_usd_total` | 成本 |
| `queue_depth` / `queue_dead_letter_pending` | 队列 |
| `cache_results_total` / `cache_saved_calls_total` | 缓存收益 |

## 待办 / 已知边界

- `docs/runbook/11.observability-runbook.md`（1012 行）的示例 span 树基于第 9 步，**不含 `refund_request` 节点**；按代码 `node_specs()` 已含该节点（`app/graph/nodes.py:436`）。以代码为准（`../../02-异常清单.md` E-10）。
- Grafana 宿主端口两套形态不同：开发栈 3000、生产栈 3001（`../../02-异常清单.md` E-16）。
- ⚠️ `otel-collector` 在生产 compose 里是 `--profile tracing` 可选启动（`docs/runbook/17.docker-ops.md:48`），默认没有 trace 落盘。

## 来源

- `docs/modules/11.observability.md`（66 行）、`docs/runbook/11.observability-runbook.md`（1012 行）
- 代码：`app/observability/*.py`、`infra/prometheus/`、`infra/grafana/`、`infra/otel/`
