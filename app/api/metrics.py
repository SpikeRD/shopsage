"""指标：**零依赖的计数器 / 直方图**，出口是 Prometheus 文本格式（`9.api.md` §2 的 `/metrics`）。

`9.api.md` 把 `/metrics` 列进了对外接口，而埋点（第 11 步 observability）还没落地。
本模块的处理方式是：**先把「API 层能测的量」测起来，并把出口做成 Prometheus 文本格式**，
一步到位的部分是：

* 端点 `/metrics` 现在就可用（`curl` 得到标准新行格式，Grafana/Prometheus 抓取格式不变）；
* 「先出结论骨架」的延迟（B2 的量化指标）现在就有数据，第 11 步接 Grafana 时不用回填历史。

刻意**不做**的部分（留给第 11 步，避免两套埋点）：

* 不引入 `prometheus_client`，也不注册全局 `REGISTRY` —— 那样一个库的全局状态会渗进
  每条用例（测试之间互相污染），而本模块的实例挂在 `app.state` 上，建一个 app 就有一份；
* 不做节点级/工具级埋点（那需要 `graph/` 与 `tools/` 的注入点，属第 11 步的活）；
* 不做 p50/p95/p99 的分位计算 —— 直方图的**桶**已经足够，
  分位数交给 Prometheus 的 `histogram_quantile()` 在查询时算（这正是它设计的用法）。
  自己算分位会得到一个「随时间漂移、无法复现」的数字。

## 关于标签基数（为什么状态码要折叠成 `2xx` / `4xx` / `5xx`）

Prometheus 的每个唯一标签组合都是一条时间序列。把原始状态码（或 `user_id`）当标签，
一个爬虫刷 500 个不存在的路径就能造出 500 条序列 —— 这是最常见的一种「监控把监控搞垮」。
所以这里**只允许低基数标签**：`endpoint` / `status_class` / `role`（角色表是有限的）。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Mapping
from typing import Final, Protocol
from urllib.parse import quote

#: 直方图的桶边界（秒）。覆盖「本机毫秒级」到「模型超时级」：
#: 10ms 以下是空转，5s 以上已经进入 §5 的可重试/降级讨论，不需要更细。
DEFAULT_BUCKETS: Final[tuple[float, ...]] = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

#: 指标名前缀（Prometheus 约定：`<namespace>_<subsystem>_<name>`）
PREFIX: Final[str] = "shopsage_api"


def _label_key(labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """标签 → 可哈希且**顺序稳定**的键（渲染时标签顺序必须稳定，否则同一个指标会分裂成多条）。"""
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _render_labels(labels: Mapping[str, str]) -> str:
    """`{'a': 'b'}` → `{a="b"}`（Prometheus 文本格式；值里的引号/换行要转义）。"""
    if not labels:
        return ""
    parts = []
    for name, value in labels.items():
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'{name}="{escaped}"')
    return "{" + ",".join(parts) + "}"


class MetricsRegistry:
    """进程内指标registry：`counter` / `observe` 两个写入口 + `render()` 一个读出口。"""

    def __init__(self, *, buckets: Iterable[float] = DEFAULT_BUCKETS) -> None:
        self._buckets: tuple[float, ...] = tuple(sorted(buckets))
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}
        self._lock = threading.Lock()

    # ── 写 ────────────────────────────────────────────────────────────────────
    def counter(self, name: str, labels: Mapping[str, str] | None = None, *, value: float = 1.0) -> None:
        key = (name, _label_key(labels or {}))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, seconds: float, labels: Mapping[str, str] | None = None) -> None:
        key = (name, _label_key(labels or {}))
        with self._lock:
            buckets = self._histograms.setdefault(key, [0.0] * (len(self._buckets) + 2))
            for index, bound in enumerate(self._buckets):
                if seconds <= bound:
                    buckets[index] += 1
            buckets[-2] += 1  # +Inf 桶（累计计数）
            buckets[-1] += seconds  # 总和

    def snapshot(self) -> dict[str, dict[str, object]]:
        """当前值的快照（测试断言用；结构不承诺对外稳定）。"""
        with self._lock:
            counters = {f"{n}{_render_labels(dict(k))}": v for (n, k), v in self._counters.items()}
            histograms: dict[str, object] = {}
            for (name, key), buckets in self._histograms.items():
                histograms[f"{name}{_render_labels(dict(key))}"] = {
                    "count": buckets[-2],
                    "sum": buckets[-1],
                }
        return {"counters": counters, "histograms": histograms}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()

    # ── 读（Prometheus 文本格式 0.0.4）────────────────────────────────────────
    def render(self) -> str:
        """渲染成 Prometheus 文本格式。

        ⚠️ 直方图的 `_bucket` 系列必须是**累计**值（`le` 单调不减），这是 Prometheus
        的硬性约定；写反了不会报错，只会让 `histogram_quantile()` 算出毫无意义的数字。
        本实现在 `observe()` 里对每个命中桶都加一，天然满足累计性。
        """
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            histograms = {k: list(v) for k, v in self._histograms.items()}

        families: dict[str, list[str]] = {}
        for (name, key), value in counters.items():
            families.setdefault(name, []).append(f"{name}{_render_labels(dict(key))} {_fmt(value)}")
        for (name, key), buckets in histograms.items():
            labels = dict(key)
            body = [
                f'{name}_bucket{_render_labels({**labels, "le": _fmt(bound)})} {_fmt(buckets[i])}'
                for i, bound in enumerate(self._buckets)
            ]
            body.append(f'{name}_bucket{_render_labels({**labels, "le": "+Inf"})} {_fmt(buckets[-2])}')
            body.append(f"{name}_sum{_render_labels(labels)} {_fmt(buckets[-1])}")
            body.append(f"{name}_count{_render_labels(labels)} {_fmt(buckets[-2])}")
            families.setdefault(name, []).extend(body)

        for name in sorted(families):
            lines.append(f"# HELP {name} shopsage 自采指标（第 9 步 api 层）")
            lines.append(f"# TYPE {name} {'summary' if name.endswith('_seconds') else 'counter'}")
            lines.extend(sorted(families[name]))  # 排序：输出稳定，diff 才有意义
        return "\n".join(lines) + "\n"


def _fmt(value: float) -> str:
    """数字格式化：整数不带小数点，浮点保留 6 位（Prometheus 的常规写法）。"""
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}"


class Metrics(Protocol):
    """路由层只依赖这两个写入口（换实现不影响路由代码）。"""

    def counter(self, name: str, labels: Mapping[str, str] | None = None, *, value: float = 1.0) -> None:
        ...

    def observe(self, name: str, seconds: float, labels: Mapping[str, str] | None = None) -> None:
        ...


def status_class(status_code: int) -> str:
    """状态码 → 低基数标签（`2xx` / `4xx` / `5xx`）。"""
    return f"{int(status_code) // 100}xx"


def escape_label(value: str) -> str:
    """URL 里有非法字符时用它做标签值（保留可读性的最小转义）。"""
    return quote(str(value), safe="/._-")


__all__ = [
    "DEFAULT_BUCKETS",
    "PREFIX",
    "Metrics",
    "MetricsRegistry",
    "escape_label",
    "status_class",
]
