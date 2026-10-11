"""可观测性三件套（第 11 步）：**链路追踪（OTEL）+ 指标（Prometheus）+ LLM 专用（Langfuse）**。

`11.observability.md` §1 把这一层的职责收成三个问题：

| 问题 | 谁回答 | 本包里的模块 |
|---|---|---|
| **慢在哪**（一次请求的调用链） | OTEL 的父子 span | `tracing.py` |
| **健康吗**（长期看是多少、要不要告警） | Prometheus 的聚合指标 | `metrics.py` |
| **花多少钱 / 质量如何** | Langfuse 的 token/成本/评分 | `langfuse_cb.py` |

## 三条纪律（每一条都对应一次「不做就会出事」）

1. **三件工具各管一摊，不互相替代**（文档 §4 第 2 条）。
   本包**不允许**出现「把 QPS 也塞进 Langfuse」或「把 prompt 全文塞进指标标签」这类串味：
   前者的症状是账单里混进了一堆与模型无关的记录，后者是基数爆炸 + PII 出网。
   串味在代码上的表现是**同一个量有两个出口**，而两个出口必然漂移。

2. **埋点是旁路，永不改变控制流**（本包所有写入路径都不抛异常）。
   本仓第 8 步的核心机制是「节点异常穿透到回合驱动才能重放断点」——
   一个会抛的埋点会**静默吃掉断点**。同理，埋点也不能吞掉别人的异常
   （`tracing.span()` 里 `except: raise`），更不能把用户的请求变成 500。

3. **出口只有一个**：指标文本由 `metrics.Metrics.render()` 渲染，
   埋点原语由 `tracing.trace_*` 提供，LLM 记录由 `langfuse_cb` 落地。
   多一个出口就多一处「忘了同步」，而漂移的监控比没有监控更危险
   （它会让人**以为**看见了）。

## 对外接口（与 `11.observability.md` §2 一致）

```python
from app.observability.metrics import AGENT_DURATION, SQL_DURATION, REQUEST_COUNT
from app.observability.tracing import trace_node, trace_tool, trace_llm
from app.observability.langfuse_cb import langfuse_handler
```

## 装配顺序（`setup_observability()` 一次做完）

```python
from app.observability import setup_observability

telemetry = setup_observability(settings)   # 指标 + tracer + Langfuse，全部就绪
...                                          # 业务跑起来，埋点自动生效
telemetry.flush()                            # 关服前把还没发出去的 span 推出去
```

⚠️ **`setup_observability()` 是「一次」，不是「每请求一次」**：它建 `TracerProvider`、
开 OTLP 导出线程、初始化 Langfuse client。放在请求路径上会每个请求建一套。
"""

from __future__ import annotations

import logging

from app.observability import langfuse_cb as _langfuse_cb
from app.observability.metrics import (
    AGENT_DURATION,
    CONTENT_TYPE,
    LLM_COST,
    LLM_DURATION,
    LLM_TOKENS,
    REQUEST_COUNT,
    REQUEST_DURATION,
    RETRIEVAL_DURATION,
    SPAN_ERRORS,
    SQL_DURATION,
    TOOL_DURATION,
    TRACE_COUNT,
    Metrics,
    MetricsSettings,
    default_metrics,
    new_metrics,
    record_cost,
    record_tokens,
    set_default_metrics,
)
from app.observability.tracing import (
    Telemetry,
    configure_telemetry,
    reset_telemetry,
    telemetry,
    trace_llm,
    trace_node,
    trace_retrieval,
    trace_span,
    trace_tool,
    trace_tool_call,
)
from app.observability.langfuse_cb import langfuse_handler

logger = logging.getLogger(__name__)


def setup_observability(
    settings: object = None,
    *,
    metrics: Metrics | None = None,
    metrics_settings: MetricsSettings | None = None,
) -> Telemetry:
    """按配置装配可观测性（**幂等**：重复调用会用新配置重建一套）。

    配置项（都来自 `app.config.Settings`，缺省值都是「不打扰业务」）：

    | 配置 | 默认 | 作用 |
    |---|---|---|
    | `otel_endpoint` | `""` | 空 = **只记在内存里**（本机开发）；填了才外发 OTLP/HTTP |
    | `langfuse_public_key` / `langfuse_secret_key` | 空 | 空 = LLM 记录关闭（不发任何请求） |
    | `observability_enabled` | `True` | 关掉后所有埋点退化成 no-op（压测对照用） |

    ⚠️ `metrics` 参数是**注入点**（不是测试后门）：`app/api/main.py` 会注入它挂在
    `app.state` 上的那一份 —— 于是「API 层指标」与「图/LLM 层指标」进同一个 registry，
    `/metrics` 一次抓取就能拿到全部（否则要抓两个端点，而运维会只配一个）。
    """
    resolved = metrics or default_metrics()
    if metrics is not None:
        set_default_metrics(metrics)

    enabled = True
    endpoint = ""
    service = "shopsage"
    if settings is not None:
        # 配置是外部输入：**读不到就用默认值**，绝不让「配置项拼错」变成启动失败
        enabled = bool(getattr(settings, "observability_enabled", True))
        endpoint = str(getattr(settings, "otel_endpoint", "") or "")
        service = str(getattr(settings, "service_name", "") or "shopsage")

    telemetry_instance = configure_telemetry(
        enabled=enabled,
        service_name=service,
        otel_endpoint=endpoint,
        metrics=resolved,
    )
    # Langfuse 与 OTEL 共用同一个 provider（Langfuse 3+/4 本身就是 OTEL 的实现），
    # 于是「一次 LLM 调用」在 trace 里是一条完整链路，而不是两个互不相干的系统。
    _langfuse_cb.configure_langfuse(settings, telemetry=telemetry_instance)
    if metrics_settings is not None:
        logger.warning("metrics_settings 只对 new_metrics() 生效，这里的 registry 已定型")
    return telemetry_instance


__all__ = [
    "AGENT_DURATION",
    "CONTENT_TYPE",
    "LLM_COST",
    "LLM_DURATION",
    "LLM_TOKENS",
    "REQUEST_COUNT",
    "REQUEST_DURATION",
    "RETRIEVAL_DURATION",
    "SPAN_ERRORS",
    "SQL_DURATION",
    "TOOL_DURATION",
    "TRACE_COUNT",
    "Metrics",
    "MetricsSettings",
    "Telemetry",
    "configure_telemetry",
    "default_metrics",
    "langfuse_handler",
    "new_metrics",
    "record_cost",
    "record_tokens",
    "reset_telemetry",
    "set_default_metrics",
    "setup_observability",
    "telemetry",
    "trace_llm",
    "trace_node",
    "trace_retrieval",
    "trace_span",
    "trace_tool",
    "trace_tool_call",
]
