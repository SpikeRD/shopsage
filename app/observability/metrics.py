"""指标：**`prometheus_client` 的 collector 全家桶 + `/metrics` 的唯一渲染出口**（第 11 步）。

`11.observability.md` §4 第 2 条把三件工具的边界划得很清楚：

| 工具 | 管什么 | 不管什么 |
|---|---|---|
| OTEL（`tracing.py`） | 「这次请求的调用链长什么样」——父子 span、依赖关系 | 不做聚合、不做告警 |
| **Prometheus（本模块）** | **「长期看是多少」——聚合指标、分位数、告警** | 不管单次请求 |
| Langfuse（`langfuse_cb.py`） | 「LLM 专用：token / 成本 / 答案质量」 | 不管系统级 QPS |

所以本模块**只定义与聚合有关的量**，且刻意只做两件事：定义 instrument、渲染文本。

## 与第 9 步 `app/api/metrics.py` 的关系（本步的一次**收敛**，不是重写）

第 9 步为了让 `/metrics` 当时就能用，在 `app/api/metrics.py` 里手写了一套计数器/直方图
（当时没有 `prometheus_client`，且那一版的注释明确写了「不做节点级埋点，留给第 11 步」）。
本步把它**收敛到一处**：

* 指标的定义、注册、文本渲染全在本模块（`prometheus_client` 是**唯一**的实现）；
* `app/api/metrics.py` 保留 `MetricsRegistry` 这个**门面类型**（它的 `counter()` / `observe()`
  是中间件与路由依赖的写入口），内部改成本模块的 collector —— 于是
  「注册表」与「collector」不再各存一份数据（两份必然漂移：某处 `observe` 忘了转发）；
* `/metrics` 的响应体裁也因此**只有一处**，且是库自带的 `generate_latest()`
  —— 手写文本格式最容易出的错是「直方图的 `_bucket` 忘了写累计性」，
  而那一错不会报错，只会让 `histogram_quantile()` 算出毫无意义的数字（第 9 步的注释里已经点过）。

> ⚠️ **`PROMETHEUS_DISABLE_CREATED_SERIES` 必须在 import `prometheus_client` 之前设**。
> 见本模块 `_disable_created_series()` 的说明：它关掉的是 `*_created` 那一族时间戳 gauge，
> 而那一族在**进程重启后值会变小**——对 Prometheus 的 counter 语义来说这是「计数器回绕」，
> 会让每个副本重启时凭空产生一次 `rate()` 尖峰。第 9 步定下的 `/metrics` 输出契约里也没有它。

## 为什么桶边界要按**用途**分开，而不是全局一套

`11.observability.md` §4 第 3 条说「用 histogram 而非平均值」。这句话只对了一半 ——
光有 histogram 还不够，**桶的边界必须落在你会去问的那些量级上**：

* 一个节点耗时 30ms 与 60ms 的区别是有意义的（本地 SQL vs 本地检索），
  但用「0.1 / 0.5 / 1 / 5」的粗桶去量它们，两者落进同一个桶，分位数被钉死在桶边界上；
* 一次 LLM 调用 800ms 与 3s 的区别同样有意义，但用「0.01」起步的细桶去量，
  前 5 个桶永远是 0，白占 5 条序列（每条序列都是一个长期的存储成本）。

所以本模块用两套桶：**快**（节点/SQL/检索，10ms~10s）与**慢**（LLM 调用，100ms~60s）。
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

logger = logging.getLogger(__name__)

# ── 必须在 import prometheus_client 之前执行（见模块 docstring 的 ⚠️）─────────────
#: 关掉 `*_created` 系列：它是**时间戳**（不是计数器），值在进程重启后会变小，
#: 而 Prometheus 看到 counter 变小会认为「回绕了」并产生一次假的 rate 尖峰。
#: 用 `setdefault` 而不是赋值：给运维留一个「就是要那族指标」的覆盖口。
os.environ.setdefault("PROMETHEUS_DISABLE_CREATED_SERIES", "true")

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest  # noqa: E402

#: 指标名前缀。与第 9 步的 `shopsage_api_*` 兼容：业务侧指标都在一起，
#: 前缀只区分「哪一层产生的」（`shopsage_api_*` = HTTP 层，本模块 = 图/LLM/工具层）。
NAMESPACE: Final[str] = "shopsage"

#: `/metrics` 的 Content-Type。
#:
#: ⚠️ 刻意钉在 `version=0.0.4` 而不是用库的 `CONTENT_TYPE_LATEST`（它现在是 `1.0.0`）：
#: Prometheus 两种都能抓，但**第 9 步已经把 0.0.4 写进了对外契约**（`health.py` 的
#: `PROMETHEUS_CONTENT_TYPE` 与用例 `test_metrics_exposition_format_is_parseable`），
#: 而本步要输出的是**同一族文本格式**（本模块的 `_esc` 也照 0.0.4 的转义规则写）。
#: 换版本号是一件「要带证据一起改」的事，不该顺手做。
CONTENT_TYPE: Final[str] = "text/plain; version=0.0.4; charset=utf-8"


@dataclass(frozen=True)
class MetricsSettings:
    """指标层的可调项（**只有桶边界**，且默认值就是生产口径）。

    为什么把它做成参数而不是常量：桶边界的正确性依赖「被测系统的真实耗时」，
    而在**离线测试**里系统耗时会缩到微秒级（假模型、内存 saver）——
    那时若沿用生产桶（最小 10ms），所有观测值都落进第一个桶，
    「能算出 P99」这条用例就变成了一句空话（它会在任何实现下都通过）。
    所以测试可以注入细桶，从而真的验证「分桶 → 分位数」这条链路。
    """

    #: 快路径（Agent 节点 / 工具 / SQL / 向量检索）：10ms ~ 10s
    fast_buckets: tuple[float, ...] = (
        0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
    )
    #: 慢路径（LLM 调用）：100ms ~ 60s（一次模型调用很少低于 100ms，很少高于 60s）
    slow_buckets: tuple[float, ...] = (
        0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 30.0, 60.0,
    )
    #: 单条标签值长度上限（防止某个调用方把整条 SQL 当标签值塞进来）
    max_label_chars: int = 80


#: 标签值的**按名上限**。`endpoint` 尤其危险：它是「方法 + 完整路径」拼出来的，
#: 而路径里带 id 时（`/api/orders/12345`）**每个 id 都是一条新时间序列** ——
#: 这是把监控自己拖垮的最常见方式（第 9 步的 `status_class` 折叠防的是同一类事）。
#: 这里把 `endpoint` 卡得比默认更短，让它即使漏了折叠也只多出**有限**条序列。
LABEL_LIMITS: Final[dict[str, int]] = {"endpoint": 64, "model": 48, "tool": 32, "node": 24, "route": 24}


#: 默认设置（生产口径）
DEFAULT_SETTINGS: Final[MetricsSettings] = MetricsSettings()

#: 直方图桶探针的半开区间上界，见 `Metrics.histogram_quantile()`。
_QUANTILE_GRID: Final[tuple[float, ...]] = (0.5, 0.9, 0.95, 0.99)


class Metrics:
    """一整套指标（`prometheus_client` 的 collector + 一个**自己的** registry）。

    ## 为什么每个实例有自己的 `CollectorRegistry`

    `prometheus_client` 默认往**进程全局** `REGISTRY` 里注册，而全局注册表的
    `register()` 遇到同名 collector 会抛 `ValueError: Duplicated timeseries`。
    在测试里这会让「第二条用例」直接红，而且报错信息指向注册表而不是被测代码 ——
    最难查的一类噪音。所以这里**一律显式传 registry**，全局注册表完全不用。

    ## 为什么写入口不是 `Counter` / `Histogram` 对象本身

    直接用 `Counter.labels(...).inc()` 的话，调用方要自己记住「哪个指标有哪些标签」，
    而写错标签名的后果是 `ValueError` —— 发生在**运行期**（也就是请求路径上）。
    本门面的做法是 `counter("<系列名>", {...})`：名字与标签都是数据，
    拼错只会多出一条没人看的序列（**静默但不致命**），比让请求 500 好。
    这与第 9 步 `MetricsRegistry` 的口径一致，所以两者可以直接互换（见 `MetricsRegistry`）。
    """

    def __init__(self, *, settings: MetricsSettings | None = None) -> None:
        self.settings = settings or DEFAULT_SETTINGS
        self.registry = CollectorRegistry()
        self._lock = threading.Lock()
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._gauges: dict[str, Gauge] = {}

    # ── 写入口 ───────────────────────────────────────────────────────────────
    def counter(self, name: str, labels: Mapping[str, str] | None = None, *, value: float = 1.0) -> None:
        """给 `name` 这个系列加 `value`（标签是数据，不是位置参数）。"""
        self._inc(name, self._normalize(labels), float(value))

    def observe(self, name: str, seconds: float, labels: Mapping[str, str] | None = None) -> None:
        """往 `name` 这个直方图里记一次观测（**单位必须是秒**，名字以 `_seconds` 结尾）。

        ⚠️ 名字不带 `_seconds` 后缀时 `prometheus_client` 不会报错，只是它会
        **自动补上** `_seconds` —— 于是你写的 `observe("shopsage_x", ...)` 与
        别人写的 `observe("shopsage_x_seconds", ...)` 会变成两条不同的序列。
        所以这里直接**规整**名字（而不是抛错）：指标写错不该打断业务请求。
        """
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds < 0:
            # 时钟回拨 / 假计时器：负耗时会让 `_sum` 变成负数，Prometheus 的 rate() 会算出鬼值
            return
        self._obs(self._histogram_name(name), self._normalize(labels), seconds)

    def gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """把 `name` 这个 gauge **设成** `value`（当前值，可以升也可以降）。

        ⚠️ 三个不能混用的语义，写错任何一个都会得到一条**看起来正常**的曲线：

        | 语义 | 方法 | 例子 |
        |---|---|---|
        | 「发生了 N 次」（只增） | `counter()` | 入队了几笔、失败了几笔 |
        | 「一次花了多久」 | `observe()` | 单任务耗时（→ 分位数） |
        | 「现在是多少」（可升可降） | **`gauge()`** | 队列积压、待处理死信、最老死信躺了多久 |

        用 `counter()` 记积压是**最常见的错**：`rate()` 之后它会变成「积压增长速率」，
        看起来像一条合法曲线，而「现在积压多少」这个问题永远答不出来
        （这也是为什么 `QUEUE_DEPTH` 必须有一条自己的写入口 —— 第 11 步只留了常量，
        本步把它接上）。

        `NaN` / `inf` 直接丢弃：`prometheus_client` 对它们会写出一个 Prometheus
        无法解析的文本行（整个 scrape 失败），而调用方那边完全看不出问题。
        """
        number = float(value)
        if not math.isfinite(number):
            return
        self._set(self._gauge_name(name), self._normalize(labels), number)

    def _inc(self, name: str, labels: dict[str, str], value: float) -> None:
        try:
            child = self._counter(name, tuple(labels)).labels(**labels) if labels else self._counter(name, ())
        except (ValueError, TypeError) as exc:  # noqa: BLE001 - 指标写错不该打断业务请求
            _warn("counter", name, exc)
            return
        child.inc(value)

    def _obs(self, name: str, labels: dict[str, str], seconds: float) -> None:
        try:
            hist = self._histogram(name, tuple(labels))
            child = hist.labels(**labels) if labels else hist
        except (ValueError, TypeError) as exc:  # noqa: BLE001
            _warn("histogram", name, exc)
            return
        child.observe(seconds)

    def _set(self, name: str, labels: dict[str, str], value: float) -> None:
        try:
            gauge = self._gauge(name, tuple(labels))
            child = gauge.labels(**labels) if labels else gauge
        except (ValueError, TypeError) as exc:  # noqa: BLE001
            _warn("gauge", name, exc)
            return
        child.set(value)

    # ── 内部：collector 的建/取（**标签名在首见时定型**，见 `LABEL_SPECS`）────────
    def _counter(self, name: str, labelnames: tuple[str, ...]) -> Counter:
        with self._lock:
            existing = self._counters.get(name)
            if existing is None:
                existing = Counter(
                    name,
                    f"shopsage 自采计数器：{name}",
                    labelnames,
                    registry=self.registry,
                )
                self._counters[name] = existing
            return existing

    def _histogram_name(self, name: str) -> str:
        """把名字规整成 `<...>_seconds`（理由见 `observe`）。"""
        return name if name.endswith("_seconds") else f"{name}_seconds"

    def _gauge_name(self, name: str) -> str:
        """gauge 的名字**保持原样**（不加后缀）。

        ⚠️ 与直方图相反，这里**不能**自动补后缀：`shopsage_queue_depth` 补成
        `_seconds` 会变成一条语义完全错误的序列，而 `prometheus_client` 的
        `Gauge('shopsage_queue_depth_gauge', ...)` 在**对外暴露时**会去掉 `_gauge` 后缀
        （那是库自己的命名约定），于是「我怎么写名字」与「抓到的名字」不再一致 ——
        对一个要写进 Grafana 查询的量来说，这是最不该有的不确定性。
        """
        return name

    def _collector_kinds(self) -> dict[str, str]:
        """`{样本名: 类型}`（`counter` / `histogram` / `gauge`），来自 collector 自己。

        ⚠️ 为什么不用「名字后缀」猜：`prometheus_client` 的样本名里**没有类型信息**，
        而 gauge 与 counter 的名字形状可以一模一样（`shopsage_queue_depth` 两种都合法）。
        猜错的后果不是报错，而是 gauge 的值悄悄出现在 `snapshot()["counters"]` 里 ——
        一个「当前积压」被读成「累计入队数」，而两条曲线看起来都很正常。
        所以这里直接问 `metric.type`（collector 的公开属性）。
        """
        kinds: dict[str, str] = {}
        for metric in self.registry.collect():
            kind = str(getattr(metric, "type", "") or "")
            if kind not in ("counter", "histogram", "gauge"):
                continue
            for sample in metric.samples:
                kinds[sample.name] = kind
        return kinds

    def _histogram(self, name: str, labelnames: tuple[str, ...]) -> Histogram:
        with self._lock:
            existing = self._histograms.get(name)
            if existing is None:
                # 桶按用途选：LLM 的系列用慢桶，其余用快桶（理由见模块 docstring）
                buckets = (
                    self.settings.slow_buckets
                    if _is_llm_series(name)
                    else self.settings.fast_buckets
                )
                existing = Histogram(
                    name,
                    f"shopsage 自采直方图（秒）：{name}",
                    labelnames,
                    registry=self.registry,
                    buckets=buckets,
                )
                self._histograms[name] = existing
            return existing

    def _gauge(self, name: str, labelnames: tuple[str, ...]) -> Gauge:
        with self._lock:
            existing = self._gauges.get(name)
            if existing is None:
                existing = Gauge(
                    name,
                    f"shopsage 自采 gauge（瞬时值）：{name}",
                    labelnames,
                    registry=self.registry,
                )
                self._gauges[name] = existing
            return existing

    def _normalize(self, labels: Mapping[str, str] | None) -> dict[str, str]:
        """标签归一：转字符串、按上限截断、**按名字排序取值**。

        为什么排序而不是保持插入顺序：`prometheus_client` 的 `labels(**kwargs)`
        按**构造时的名字顺序**取值，而收集器的标签顺序由「首次调用」定型。
        排序让「首次调用的键顺序」不再是行为的一部分 ——
        否则 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 会走到两个不同的分支（一个能跑、一个报错）。
        """
        out: dict[str, str] = {}
        for key, value in sorted((labels or {}).items(), key=lambda kv: str(kv[0])):
            text = str(value)
            limit = LABEL_LIMITS.get(str(key), self.settings.max_label_chars)
            if len(text) > limit:
                text = text[: limit - 1] + "…"
            out[str(key)] = text
        return out

    # ── 读出口 ───────────────────────────────────────────────────────────────
    def render(self) -> str:
        """渲染成 Prometheus 文本格式（**唯一**的渲染实现）。"""
        return generate_latest(self.registry).decode("utf-8")

    def snapshot(self) -> dict[str, Any]:
        """结构化快照（**只给测试与演示脚本用**，结构不承诺对外稳定）。

        `histograms` 里给的是**桶计数**而不是单个数字：分位数必须从桶算
        （这正是 Prometheus 的设计意图，见 `histogram_quantile()`）。
        """
        counters: dict[str, float] = {}
        histograms: dict[str, dict[str, Any]] = {}
        gauges: dict[str, float] = {}
        # 类型问 collector 自己，不靠名字猜（理由见 `_collector_kinds`）
        kinds = self._collector_kinds()
        for metric in self.registry.collect():
            for sample in metric.samples:
                if kinds.get(sample.name) == "gauge":
                    # ⚠️ gauge 必须与直方图/计数器**分开收**：否则「当前积压」会与
                    # 「累计入队数」混在同一个字典里，而两条曲线看起来都很正常。
                    gauges[_series_key(sample.name, {k: v for k, v in sample.labels.items()})] = sample.value
                    continue
                # 一个直方图的全部样本（`_bucket` / `_count` / `_sum`）**必须聚到同一个键**，
                # 否则分位数会从一半数据里算出一个看似合理的数字（最难查的一类错）。
                # 所以键取「去掉后缀、且去掉 `le` 标签」之后的系列名。
                base, kind = _split_sample(sample.name)
                labels = {k: v for k, v in sample.labels.items() if k != "le"}
                key = _series_key(base, labels)
                if kind == "counter":
                    counters[key] = sample.value
                    continue
                entry = histograms.setdefault(key, {"buckets": {}, "count": 0, "sum": 0.0})
                if kind == "bucket":
                    bound = sample.labels.get("le")
                    if bound == "+Inf":
                        entry["count"] = sample.value
                    elif bound is not None:
                        entry["buckets"][float(bound)] = sample.value
                elif kind in ("count", "sum"):
                    entry[kind] = sample.value
        return {"counters": counters, "histograms": histograms, "gauges": gauges}

    def gauge_value(self, series: str) -> float | None:
        """读一个 gauge 的当前值（演示脚本与用例用；**不放请求路径**）。

        `series` 可以是裸名字（`shopsage_queue_depth`）或带标签的键
        （`shopsage_queue_jobs_total{function="x"}`）—— 与 `histogram_quantile` 同一口径。
        """
        snap = self.snapshot()["gauges"]
        key = next((k for k in snap if k == series or k.startswith(f"{series}{{")), None)
        return None if key is None else float(snap[key])

    def histogram_quantile(self, series: str, quantile: float) -> float | None:
        """从桶里算分位数（Prometheus `histogram_quantile()` 的等价实现）。

        存在的理由（`11.observability.md` §7 的 `test_histogram_has_p99`）：
        这条用例要证明的是「**直方图里真的能算出 P99，而不是只有均值**」。
        而「能算」这件事必须自己算一遍才成立 —— 光断言 `/metrics` 里有 `_bucket` 行
        证明不了分位数可用（桶全落在同一侧时 `histogram_quantile` 会返回边界值，
        那也是一个数字，但不是观测到的分布）。

        ⚠️ **返回的是桶边界的插值**，不是真实分位数（这是 Prometheus 的固有限制）：
        桶的粒度决定了精度。想更准就把桶设密（代价是序列数），这与
        「分位数必须能复现」这条纪律同源：**别在应用层自己算分位**。
        """
        snap = self.snapshot()["histograms"]
        key = next((k for k in snap if k == series or k.startswith(f"{series}{{")), None)
        if key is None:
            return None
        entry = snap[key]
        buckets: dict[float, float] = dict(entry["buckets"])
        total = float(entry["count"])
        if not buckets or total <= 0:
            return None
        if quantile <= 0:
            return 0.0

        # 分位数落在哪个桶里？从**最小**的桶开始累加，第一个「累计计数 ≥ 目标名次」的桶就是它。
        # 桶计数是**累计**的（`le` 语义），所以不需要再自己求和 —— 这也是第 9 步的
        # 手写实现里唯一容易写错、又不会报错的地方。
        target = quantile * total
        upper = sorted(buckets)
        lower = 0.0
        for bound in upper:
            count_here = buckets[bound]
            if count_here >= target:
                # 落在 (lower, bound]：按桶内均匀分布插值（Prometheus 的同一算法）
                count_below = sum(buckets[b] for b in upper if b < bound) or 0.0
                count_in = count_here - count_below
                if count_in <= 0:
                    return bound
                return lower + (bound - lower) * ((target - count_below) / count_in)
            lower = bound
        return upper[-1]

    def reset(self) -> None:
        """清空所有序列（测试用；**已注册的 collector 不注销**）。

        为什么要保留 collector：`prometheus_client` 的 `unregister` 之后
        旧的 child 对象仍能写（写进一个不再被 collect 的副本）→ 「reset 了但还是有数」
        这种幽灵行为。清空 `_metrics` 里每个 child 的值才是彻底的做法。
        """
        with self._lock:
            for counter in self._counters.values():
                counter._metrics.clear()  # noqa: SLF001 - 官方没有公开的 reset（见 docstring）
            for histogram in self._histograms.values():
                histogram._metrics.clear()  # noqa: SLF001
            for gauge in self._gauges.values():
                gauge._metrics.clear()  # noqa: SLF001


def _series_key(name: str, labels: Mapping[str, str]) -> str:
    """`(名字, 标签)` → `name{a="b"}`（标签**排序**，否则同一系列会分裂成多条）。"""
    if not labels:
        return name
    pairs = sorted((str(k), str(v)) for k, v in labels.items())
    body = ",".join(f'{k}="{_esc(v)}"' for k, v in pairs)
    return f"{name}{{{body}}}"


#: 直方图的样本后缀 → 样本种类。**顺序有意义**（`_bucket` 要先于 `_count` 判，
#: 虽然两者不会同时匹配，但把长后缀排在前面能让「加后缀」这件事只写一遍）。
_SAMPLE_SUFFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("_bucket", "bucket"),
    ("_count", "count"),
    ("_sum", "sum"),
)


def _split_sample(sample_name: str) -> tuple[str, str]:
    """`shopsage_agent_duration_seconds_bucket` → `(shopsage_agent_duration_seconds, "bucket")`。

    不是直方图样本（没有已知后缀）时返回 `(原名, "counter")`。

    ⚠️ 为什么要显式列后缀而不是 `rsplit("_", 1)`：`_seconds` 后缀让「去掉最后一段」
    变成错的 —— `shopsage_x_seconds_count` 去掉最后一段是 `shopsage_x_seconds`（对），
    而 `shopsage_x_count`（没有 `_seconds` 的系列）去掉最后一段也会得到 `shopsage_x`（也对），
    但 `shopsage_x_sum` 与 `shopsage_x_count` 会在**不同**的 base 上分家 —— 一旦分家，
    分位数就只从一半数据里算。显式后缀表把「哪些名字属于同一个直方图」变成数据。

    ⚠️ 本函数**区分不了 gauge**（`prometheus_client` 的样本名里没有类型信息），
    所以 `snapshot()` 不靠它判型，而是问 collector 本身（见 `_collector_kinds()`）。
    """
    for suffix, kind in _SAMPLE_SUFFIXES:
        if sample_name.endswith(suffix):
            return sample_name[: -len(suffix)], kind
    return sample_name, "counter"


def _esc(value: str) -> str:
    """Prometheus 文本格式的标签值转义（0.0.4 口径：反斜杠、引号、换行）。"""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _warn(kind: str, name: str, exc: Exception) -> None:
    """指标写入失败时**只告警、不抛**。

    为什么不让它抛：埋点是**旁路**。一次「标签多写了一个」或「同一个名字先无标签后有标签」
    （`prometheus_client` 会直接 `ValueError`）不该让用户的这一轮问答变成 500 ——
    那样「加了监控之后可用性下降」会成为一次真实事故，而它本来只是一行日志的事。
    但也**不能静默**：静默会让「指标怎么没数据」变成一次盲查。
    """
    logger.warning("指标写入失败（已忽略该次观测）：kind=%s series=%s error=%s: %s", kind, name, type(exc).__name__, exc)


def _is_llm_series(name: str) -> bool:
    """这个系列是不是 LLM 调用（决定用慢桶还是快桶）。"""
    return "llm" in name.lower()


# ── instrument 定义（`11.observability.md` §2 的对外接口）─────────────────────
#
# 这些是**模块级单例**（进程一份），因为它们的语义就是「整个进程的累计量」。
# 想拿到干净的一份（测试 / 演示）就 `new_metrics()` 造一个新的。

#: HTTP 请求数（标签：`endpoint` / `status_class`，**低基数**）。
REQUEST_COUNT: Final = f"{NAMESPACE}_requests_total"
#: HTTP 请求耗时（histogram，快桶）。
REQUEST_DURATION: Final = f"{NAMESPACE}_request_duration_seconds"
#: 单次 Agent 节点耗时（histogram，标签 `node`）。**B1 的主角**：按节点分组算 P99 就能定位瓶颈。
AGENT_DURATION: Final = f"{NAMESPACE}_agent_duration_seconds"
#: 单次工具调用耗时（histogram，标签 `tool`）。
TOOL_DURATION: Final = f"{NAMESPACE}_tool_duration_seconds"
#: 单次 LLM 调用耗时（histogram，标签 `model`，**慢桶**）。
LLM_DURATION: Final = f"{NAMESPACE}_llm_duration_seconds"
#: 单次 SQL 执行耗时（histogram，标签 `tool`）。
SQL_DURATION: Final = f"{NAMESPACE}_sql_duration_seconds"
#: 单次向量/混合检索耗时（histogram）。
RETRIEVAL_DURATION: Final = f"{NAMESPACE}_retrieval_duration_seconds"
#: LLM token 消耗（counter，标签 `model` / `kind`：`prompt` / `completion`）。
LLM_TOKENS: Final = f"{NAMESPACE}_llm_tokens_total"
#: LLM 成本（counter，标签 `model`；**单位美元**）。找不到定价的模型不计（不是 0）。
LLM_COST: Final = f"{NAMESPACE}_llm_cost_usd_total"
#: 调用链里出错的 span 数（counter，标签 `kind`：`node` / `tool` / `sql` / `llm` / `retrieval`）。
SPAN_ERRORS: Final = f"{NAMESPACE}_span_errors_total"
#: **完成的请求**数（counter，标签 `endpoint`）。
#:
#: ⚠️ 与 `REQUEST_COUNT` 的区别不是名字，而是**语义**：`REQUEST_COUNT` 记「请求进来了几笔」
#: （含状态码），本指标记「几笔**跑完了**（成功走完调用链）」。两者都由
#: `instrument.record_request` 写，所以值相等 —— 但它们的**用途不同**：
#: 前者是黄金指标的分母（按 `status_class` 分组算错误率），
#: 后者是「有没有流量」（第 12 步队列/看板「服务还在被人用吗」的第一信号）。
#:
#: 更要紧的是：**它不是用来数 trace 的**。trace 数 = 「请求数」，
#: 而真正的 trace 采样/丢弃统计由 OTEL SDK 的内部指标负责（本仓不做采样）。
#: 把它当 trace 计数会让「采样率」这类问题永远看不出来（一个恒等于请求数的数字）。
TRACE_COUNT: Final = f"{NAMESPACE}_traces_total"
#: 写路径工具的结果计数（标签 `tool` / `outcome`：`created` / `idempotent_hit` / `truncated`…）。
#:
#: 为什么值得单独一条（而不只靠 `TOOL_DURATION` 与 span）：
#: 「**幂等命中率**」是队列/重投健康度的核心数字（第 12 步），而它是**长期趋势**问题 ——
#: 单条 trace 只能告诉你「这一次命中了」，回答不了「最近是不是一直在命中」。
#: 一直命中通常意味着上游在重复投递（A2 的幂等保护在工作，但那是**故障的信号**，不是成功）。
TOOL_OUTCOME: Final = f"{NAMESPACE}_tool_outcome_total"
#: 队列深度（第 12 步 `queue.md` 的输入；本步只留出口，值由队列自己写）。
#:
#: ⚠️ 它是 **gauge**（瞬时值，可升可降），不是 counter。第 11 步只留了这一个常量，
#: 第 12 步把它接上（`Metrics.gauge()` 也是那一步才加的）——
#: 用 `counter()` 写它会得到「积压增长速率」，而「现在积压多少」永远答不出来。
QUEUE_DEPTH: Final = f"{NAMESPACE}_queue_depth"
#: 待处理死信条数（gauge，第 12 步）。它回答「人工兜底这条路径上堆了多少活」。
QUEUE_DLQ_PENDING: Final = f"{NAMESPACE}_queue_dead_letter_pending"
#: 最老的一条待处理死信躺了多久（gauge，秒，第 12 步）。
#:
#: 与上一条的区别是**这个才值得告警**：条数多可能只是刚发生一批抖动，
#: 而「最老的躺了三天」意味着人工兜底流程根本没人在走（A1 失效）。
QUEUE_DLQ_OLDEST: Final = f"{NAMESPACE}_queue_dead_letter_oldest_seconds"

#: 进程级默认实例（唯一一份被 `/metrics` 渲染的指标）。
_DEFAULT: Metrics | None = None
_DEFAULT_LOCK = threading.Lock()


def default_metrics() -> Metrics:
    """取进程默认指标实例（懒建；**import 期不建**，避免 import 就产生全局状态）。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = Metrics()
        return _DEFAULT


def new_metrics(*, settings: MetricsSettings | None = None) -> Metrics:
    """造一个**独立**的指标实例（测试、演示脚本、多租户隔离场景）。"""
    return Metrics(settings=settings)


def set_default_metrics(metrics: Metrics | None) -> None:
    """替换进程默认实例（测试用；`None` = 丢弃旧实例，下次访问重建）。"""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = metrics


@contextmanager
def use_metrics(metrics: Metrics):
    """临时把进程默认实例换成 `metrics`（`with` 块内所有埋点都进它）。"""
    previous = default_metrics() if _DEFAULT is not None else None
    set_default_metrics(metrics)
    try:
        yield metrics
    finally:
        set_default_metrics(previous)


def record_tokens(metrics: Metrics, *, model: str, prompt: int, completion: int, total: int) -> None:
    """记一次调用的 token 账（prompt / completion / total 三条，**total 单独记**）。

    为什么 total 要单独一条而不是 `prompt + completion`：某些 provider 的
    `total_tokens` **不等于**两者之和（含 reasoning token、含缓存命中不同计价的 token）。
    自己加会与账单对不上，而「账单对不上」正是成本看板最容易失去信任的方式。
    """
    if prompt:
        metrics.counter(LLM_TOKENS, {"model": model, "kind": "prompt"}, value=float(prompt))
    if completion:
        metrics.counter(LLM_TOKENS, {"model": model, "kind": "completion"}, value=float(completion))
    if total:
        metrics.counter(LLM_TOKENS, {"model": model, "kind": "total"}, value=float(total))


def record_cost(metrics: Metrics, *, model: str, cost_usd: float | None) -> None:
    """记一次调用的成本。

    ⚠️ `cost_usd is None`（litellm 的定价表里没有这个模型，例如本地模型）**不计入**：
    把它当 0 会让「成本」这个数字**看起来完整、实际偏低** ——
    一个偏低的成本数字比一个明显缺失的数字危险得多（它不会触发任何告警）。
    """
    if cost_usd is None or not math.isfinite(float(cost_usd)):
        return
    metrics.counter(LLM_COST, {"model": model}, value=float(cost_usd))


def quantiles(metrics: Metrics, series: str, *, value_key: str = "seconds") -> dict[str, float | None]:
    """一次算出 P50/P95/P99（给演示脚本与用例用；**不放请求路径**）。"""
    del value_key  # 目前只有「秒」一种口径，参数留着是为了调用点可读
    return {f"p{int(q * 100)}": metrics.histogram_quantile(series, q) for q in _QUANTILE_GRID}


def histogram_lines(text: str, series: str) -> list[str]:
    """从渲染好的文本里挑出某个系列的行（排障时「这个指标到底有没有数据」最常用）。"""
    return [line for line in text.splitlines() if line.startswith(series)]


def buckets_of(metrics: Metrics, series: str) -> Sequence[float]:
    """某个直方图当前的桶边界（升序）—— 用例用它断言「桶选对了」。"""
    snap = metrics.snapshot()["histograms"]
    key = next((k for k in snap if k == series or k.startswith(f"{series}{{")), None)
    if key is None:
        return ()
    return tuple(sorted(snap[key]["buckets"]))


__all__ = [
    "AGENT_DURATION",
    "CONTENT_TYPE",
    "DEFAULT_SETTINGS",
    "LLM_COST",
    "LLM_DURATION",
    "LLM_TOKENS",
    "NAMESPACE",
    "QUEUE_DEPTH",
    "QUEUE_DLQ_OLDEST",
    "QUEUE_DLQ_PENDING",
    "REQUEST_COUNT",
    "REQUEST_DURATION",
    "RETRIEVAL_DURATION",
    "SPAN_ERRORS",
    "SQL_DURATION",
    "TOOL_DURATION",
    "TOOL_OUTCOME",
    "TRACE_COUNT",
    "Metrics",
    "MetricsSettings",
    "buckets_of",
    "default_metrics",
    "histogram_lines",
    "new_metrics",
    "quantiles",
    "record_cost",
    "record_tokens",
    "set_default_metrics",
    "use_metrics",
]
