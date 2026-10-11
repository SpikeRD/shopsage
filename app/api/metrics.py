"""API 层的指标写入口：`MetricsRegistry`（`9.api.md` §2 的 `/metrics`）。

## 本模块在第 11 步的变化（**收敛，不是重写**）

第 9 步为了让 `/metrics` 当时就能用，在这里手写了一套计数器/直方图 + 文本渲染，
当时的注释明确写着两件事：「不引入 `prometheus_client`」与「不做节点级/工具级埋点
（那是第 11 步的活）」。第 11 步两件都做了，所以这里改成**门面**：

| 项 | 第 9 步 | 现在 |
|---|---|---|
| 计数器/直方图的实现 | 本模块手写 | `app/observability/metrics.py`（`prometheus_client`） |
| 文本渲染 | 本模块手写（`render()`） | 同上（库的 `generate_latest()`） |
| 指标名 | `shopsage_api_*` | **不变**（下游看板与用例都按名字查） |
| `MetricsRegistry` 类型 | 真实实现 | **门面**：`counter()` / `observe()` 语义完全不变 |

为什么保留 `MetricsRegistry` 这个类型而不是让调用方直接用 `observability.metrics`：

* 中间件（`middleware.py`）与路由（`chat.py`）只依赖 `counter()` / `observe()` 这两个写入口，
  所以换实现不影响它们（**这是第 9 步就设计好的接缝**，本步正好用上）；
* `app.state.metrics` 是**按 app 注入**的（`create_app(metrics=...)`），
  测试用一条用例一个干净注册表。若改成进程级全局注册表，
  「第二条用例」会因为 `Duplicated timeseries` 直接红 ——
  `prometheus_client` 的全局 `REGISTRY` 正是这么设计的，所以本仓**从不注册它**。

## 为什么直方图必须仍然是 histogram（而不是「算好分位数再暴露」）

第 9 步的注释已经解释过：分位数要交给 Prometheus 的 `histogram_quantile()` 在**查询时**算。
本步补一条更实际的理由：**本模块现在同时被 `/metrics` 与告警规则使用**，
而告警规则写在 Prometheus 侧（`infra/prometheus/`）。若在这里算好 P99 再暴露成 gauge，
告警只能对着「已经算完的数字」写，且无法按 `node` / `tool` 做维度下钻
（下钻靠的是标签，而 gauge 一旦被聚合就丢了维度）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from app.observability.metrics import (
    AGENT_DURATION,
    DEFAULT_SETTINGS,
    Metrics,
)

#: 直方图的桶边界（秒）——**与第 9 步完全一致**，所以历史看板不会断档。
#:
#: 第 9 步的口径：10ms 以下是空转，5s 以上已经进入「重试/降级」的讨论。
#: 第 11 步的 telemetry 默认桶（`observability.metrics.MetricsSettings.fast_buckets`）
#: 是它的**超集**（多了 10s，用于慢节点）。API 层继续用这套更窄的桶：
#: HTTP 请求的耗时不值得为 5s~10s 那一段再多一条序列（那条序列要存 15 天）。
DEFAULT_BUCKETS: Final[tuple[float, ...]] = DEFAULT_SETTINGS.fast_buckets[:9]

#: 指标名前缀（Prometheus 约定：`<namespace>_<subsystem>_<name>`）
PREFIX: Final[str] = "shopsage_api"

#: 第 9 步定下的 **HTTP 层对外契约名**（看板与告警按它们配好了，不能改）。
#:
#: 与第 11 步的通用名（`observability.metrics.REQUEST_COUNT` = `shopsage_requests_total`）
#: **并存**，两者由 `instrument.record_request` 在同一次调用里写、用同一个耗时值。
#:
#: | 名字 | 谁在用 |
#: |---|---|
#: | `shopsage_api_requests_total` | 第 9 步已配好的看板/告警（改名 = 让它们静默失效） |
#: | `shopsage_requests_total` | 第 11 步的通用出口（记录规则、新看板） |
#:
#: 真正的纪律不是「只有一个名字」，而是「**一个名字只有一个写入方**」——
#: 两个名字两份数据必然漂移，而漂移的看板比没有看板更危险（它会让人**以为**看见了）。
API_REQUESTS_TOTAL: Final[str] = f"{PREFIX}_requests_total"
API_REQUEST_DURATION: Final[str] = f"{PREFIX}_request_duration_seconds"


class MetricsRegistry:
    """API 层指标的注册表门面（**转发给 `observability.metrics.Metrics`**）。

    ⚠️ 本类**不再自己存数据**。第 9 步的版本里 `render()` 读的是自己的字典，
    而第 11 步的 telemetry 埋点写在 prometheus_client 的 collector 里 ——
    两处各存一份必然漂移（典型症状：某个量在 `/metrics` 里有时有、有时没有，
    取决于哪条路径写的），所以数据只有一处：collector。
    """

    def __init__(self, *, buckets: Iterable[float] | None = None, metrics: Metrics | None = None) -> None:
        self._buckets: tuple[float, ...] = tuple(sorted(buckets)) if buckets else DEFAULT_BUCKETS
        self.metrics = metrics or Metrics()

    # ── 写（与第 9 步签名完全一致）────────────────────────────────────────────
    def counter(self, name: str, labels: Mapping[str, str] | None = None, *, value: float = 1.0) -> None:
        self.metrics.counter(name, labels, value=value)

    def observe(self, name: str, seconds: float, labels: Mapping[str, str] | None = None) -> None:
        self.metrics.observe(name, seconds, labels)

    # ── 读 ───────────────────────────────────────────────────────────────────
    def render(self) -> str:
        """渲染 Prometheus 文本格式（**唯一的渲染实现**在 `observability.metrics`）。"""
        return self.metrics.render()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """当前值的快照（测试断言用；结构不承诺对外稳定）。

        保持第 9 步的形状（`{"counters": {...}, "histograms": {...}}` 且每个直方图带
        `count` / `sum`）：那是 `tests/api/*` 与演示脚本已经在用的读口，
        换掉形状的收益是零，代价是所有调用方都要改一遍。
        """
        raw = self.metrics.snapshot()
        histograms = {
            key: {"count": entry["count"], "sum": entry["sum"]}
            for key, entry in raw["histograms"].items()
        }
        return {"counters": raw["counters"], "histograms": histograms}

    def reset(self) -> None:
        self.metrics.reset()


# ── 计数器/直方图之外的读口（B2 的量化指标；第 11 步接 Grafana 的输入）─────────


def status_class(status_code: int) -> str:
    """状态码 → 低基数标签（`2xx` / `4xx` / `5xx`）。

    折叠的**唯一**理由（第 9 步的注释，本步继续有效）：Prometheus 的每个唯一标签组合
    都是一条时间序列。把原始状态码（或更糟：`user_id`）当标签，一个爬虫刷 500 个
    不存在的路径就能造出 500 条序列 —— 「监控把监控搞垮」的最常见形态。
    """
    return f"{int(status_code) // 100}xx"


def escape_label(value: str) -> str:
    """URL 里有非法字符时用它做标签值（保留可读性的最小转义）。"""
    from urllib.parse import quote

    return quote(str(value), safe="/._-")


def agent_duration_series() -> str:
    """`AGENT_DURATION` 的系列名（给看板与用例引用，避免各处写字符串）。"""
    return AGENT_DURATION


__all__ = [
    "API_REQUEST_DURATION",
    "API_REQUESTS_TOTAL",
    "DEFAULT_BUCKETS",
    "PREFIX",
    "MetricsRegistry",
    "agent_duration_series",
    "escape_label",
    "status_class",
]
