"""指标层用例：**分位数、基数、以及「指标真的到了抓取端」**（`11.observability.md` §7）。

| 用例 | 它钉住的错误改动 |
|---|---|
| `test_metrics_exposed_*` | 「`/metrics` 里只有 API 层指标，节点级指标写进了另一个注册表」 |
| `test_histogram_has_p99` | 「只存 `_sum` / `_count`（那就能算均值了），不存桶」—— B1 的直接成因 |
| `test_histogram_quantile_*` | 「分位数算错但看起来是个合理的数」（最容易骗过 code review 的一类） |
| `test_cardinality_*` | 「把 user_id / 原始路径当标签」（监控把监控搞垮） |
| `test_observe_*` | 「负耗时 / 非有限值」进直方图（`_sum` 变负，`rate()` 算出鬼值） |
| `test_*_unit_names` | 「耗时直方图内部不可比」（三个调用点三种计时口径） |

⚠️ 本文件**不测**「Prometheus 能抓」—— 那需要真起 Prometheus（`2.infra-runbook.md` 的活）。
这里测的是「抓取端会拿到语法正确、维度正确的文本」，那是抓取成功的前提条件。
"""

from __future__ import annotations

import math
import re

import pytest

from app.observability import metrics as M


# ── 「指标被暴露」：文本格式与名字 ────────────────────────────────────────────


def test_metrics_exposed_with_expected_series(obs_metrics: M.Metrics) -> None:
    """`GET /metrics` 必须能拿到 request_count / agent_duration / sql_duration 三族。

    这是 `11.observability.md` §7 第一条用例的**离线版**：
    线上版（真的走 HTTP）在 `test_observability_api.py`，这里只证明
    「把三个量写进注册表之后，渲染出来的文本里确实有它们」。
    """
    obs_metrics.counter(M.REQUEST_COUNT, {"endpoint": "POST /api/chat", "status_class": "2xx"})
    obs_metrics.observe(M.AGENT_DURATION, 0.31, {"node": "router"})
    obs_metrics.observe(M.SQL_DURATION, 0.012, {"tool": "sql_query"})

    text = obs_metrics.render()

    assert 'shopsage_requests_total{endpoint="POST /api/chat",status_class="2xx"} 1.0' in text
    assert 'shopsage_agent_duration_seconds_count{node="router"} 1.0' in text
    assert 'shopsage_sql_duration_seconds_count{tool="sql_query"} 1.0' in text
    assert text.endswith("\n"), "Prometheus 文本格式要求末尾有换行"


def test_metrics_text_is_parseable_line_by_line(obs_metrics: M.Metrics) -> None:
    """每一行都必须是「指标名（可选标签） 数值」—— 格式错会让**整个抓取**失败。"""
    obs_metrics.counter(M.REQUEST_COUNT, {"endpoint": "GET /health", "status_class": "2xx"})
    obs_metrics.observe(M.AGENT_DURATION, 0.5, {"node": "synthesis"})
    obs_metrics.counter(M.LLM_TOKENS, {"model": "m", "kind": "total"}, value=42)

    pattern = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? -?[0-9]")
    for line in obs_metrics.render().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        assert pattern.match(line), f"这行不是合法指标：{line}"


def test_exposition_has_no_created_series(obs_metrics: M.Metrics) -> None:
    """`*_created` 那一族必须被关掉（`PROMETHEUS_DISABLE_CREATED_SERIES`）。

    它是一族**时间戳** gauge：值在进程重启后**变小**，而 Prometheus 看到 counter
    变小会认为「回绕了」，于是每个副本重启都会凭空造出一次假的 rate 尖峰。
    第 9 步定下的 `/metrics` 输出契约里也没有它（见 `metrics.py` 的说明）。
    """
    obs_metrics.counter(M.REQUEST_COUNT, {"endpoint": "GET /health", "status_class": "2xx"})
    text = obs_metrics.render()
    assert "_created" not in text


# ── P99：本步最核心的一条 ─────────────────────────────────────────────────────


def test_histogram_has_p99(obs_metrics: M.Metrics) -> None:
    """`agent_duration` 必须能算出 **P99**（而不是只有均值）。

    手法：灌 1..100ms 一百个样本，然后断言 P99 ≈ 99ms。
    如果实现只存 `_sum` / `_count`（只能算均值 50.5ms），这条断言会失败 ——
    这正是要钉住的东西（`11.observability.md` §4 第 3 条：均值会掩盖长尾）。
    """
    for index in range(1, 101):
        obs_metrics.observe(M.AGENT_DURATION, index / 1000.0, {"node": "router"})

    series = f'{M.AGENT_DURATION}{{node="router"}}'
    p99 = obs_metrics.histogram_quantile(series, 0.99)
    p50 = obs_metrics.histogram_quantile(series, 0.5)

    assert p99 is not None and p50 is not None
    # 桶边界是 0.05 / 0.1，所以 P99 落在 (0.05, 0.1] 里按均匀分布插值 ≈ 0.099
    assert 0.09 <= p99 <= 0.1, f"P99 应当接近 99ms，实际 {p99}"
    assert 0.045 <= p50 <= 0.055, f"P50 应当接近 50ms，实际 {p50}"
    # 最关键的一条：**P99 必须显著大于均值**，否则「分位数」白算了
    assert p99 > (p50 * 1.5)


def test_histogram_quantile_is_monotonic(obs_metrics: M.Metrics) -> None:
    """分位数随 q 单调不减。违反它意味着插值写错了（而结果仍是个「像样」的数）。"""
    for index in range(1, 201):
        obs_metrics.observe(M.AGENT_DURATION, index / 1000.0, {"node": "data"})
    series = f'{M.AGENT_DURATION}{{node="data"}}'
    values = [obs_metrics.histogram_quantile(series, q) for q in (0.1, 0.5, 0.9, 0.95, 0.99)]
    assert all(value is not None for value in values)
    assert values == sorted(values), f"分位数不是单调的：{values}"


def test_histogram_quantile_returns_none_for_unknown_series(obs_metrics: M.Metrics) -> None:
    """查不到的系列返回 `None`（而不是 0）。

    ⚠️ 返回 0 是最坏的选择之一：它会被当成「这一条一点都不慢」，
    于是「指标名写错」在图上表现为**完美的性能**。这是本仓最讨厌的一类静默错误。
    """
    assert obs_metrics.histogram_quantile("shopsage_nope_seconds", 0.99) is None
    assert obs_metrics.histogram_quantile(f'{M.AGENT_DURATION}{{node="never"}}', 0.99) is None


def test_histogram_series_keeps_buckets_separate_from_other_series(obs_metrics: M.Metrics) -> None:
    """不同标签的观测**不能**互相污染（否则 A 节点的 P99 会被 B 节点的慢请求拉高）。"""
    for _ in range(50):
        obs_metrics.observe(M.AGENT_DURATION, 0.001, {"node": "fast"})
    for _ in range(50):
        obs_metrics.observe(M.AGENT_DURATION, 0.9, {"node": "slow"})

    fast = obs_metrics.histogram_quantile(f'{M.AGENT_DURATION}{{node="fast"}}', 0.99)
    slow = obs_metrics.histogram_quantile(f'{M.AGENT_DURATION}{{node="slow"}}', 0.99)
    assert fast is not None and slow is not None
    assert fast < 0.01 < 0.5 < slow, f"两个节点的分位数串了：fast={fast} slow={slow}"


# ── 基数：监控把监控搞垮的几种经典形态 ────────────────────────────────────────


def test_labels_do_not_leak_user_id() -> None:
    """标签名白名单是**约定**，所以这里用源码检查钉住它。

    为什么值得一条用例：`user_id` 进标签是「一人一条时间序列」，
    而它在功能上完全正常 —— 只有 Prometheus 的内存会告诉你错了（那时已经晚了）。
    """
    from pathlib import Path

    source = Path(M.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", "*", "⚠️"))
    )
    for forbidden in ("user_id", "session_id", "request_id", "thread_id", "sql"):
        # 允许出现在**注释/文档**里（本仓的注释专门解释「为什么不能这么写」）
        occurrences = [
            line
            for line in code.splitlines()
            if f'"{forbidden}"' in line or f"'{forbidden}'" in line
        ]
        assert not occurrences, f"指标标签里不允许出现 {forbidden}：{occurrences}"


def test_unknown_label_keys_are_dropped_not_crashing(obs_metrics: M.Metrics) -> None:
    """写标签时**同一个指标**的标签集必须一致 —— 不一致的那次被丢弃并告警，不抛异常。

    实测（本步第一版）：`prometheus_client` 在标签集不匹配时抛 `ValueError`，
    而埋点跑在请求路径上 —— 于是「加了一个指标标签」变成「这个接口开始 500」。
    旁路组件无权改变业务的控制流，所以这里测的是「**不抛**」。
    """
    obs_metrics.counter("shopsage_probe_total", {"a": "1"})
    obs_metrics.counter("shopsage_probe_total", {"a": "1", "b": "2"})  # 不匹配：应被丢弃
    text = obs_metrics.render()
    assert 'shopsage_probe_total{a="1"} 1.0' in text


def test_endpoint_label_is_truncated_to_bound_cardinality(obs_metrics: M.Metrics) -> None:
    """超长标签值被截断（原始路径当标签是基数爆炸的头号来源）。"""
    long_path = "/api/orders/" + "x" * 500
    obs_metrics.counter(M.REQUEST_COUNT, {"endpoint": long_path, "status_class": "2xx"})
    text = obs_metrics.render()
    line = next(line for line in text.splitlines() if line.startswith(M.REQUEST_COUNT))
    value = re.search(r'endpoint="([^"]*)"', line)
    assert value is not None
    assert len(value.group(1)) <= M.LABEL_LIMITS["endpoint"], "endpoint 标签没有被截断"


# ── 写入侧的边界（负值 / 非有限值 / 空）───────────────────────────────────────


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), float("-inf")])
def test_observe_ignores_impossible_durations(obs_metrics: M.Metrics, bad: float) -> None:
    """负耗时与非有限值**不写**直方图。

    负耗时来自「用假计时器」或系统时钟回拨；它会让 `_sum` 变成负数，
    而 Prometheus 的 `rate()` 在 counter/histogram 上遇到负增量会算出负速率 ——
    那个数看起来像是「流量倒流」，排查起来毫无头绪。
    """
    obs_metrics.observe(M.AGENT_DURATION, bad, {"node": "router"})
    assert obs_metrics.snapshot()["histograms"] == {}


def test_observe_appends_seconds_suffix(obs_metrics: M.Metrics) -> None:
    """不带 `_seconds` 的名字被自动补上后缀。

    为什么值得钉：`prometheus_client` 也会自动补，但**它补在内部**
    （注册名与查询名可能不一致）。本门面显式补，于是
    「用例里查的名字」与「渲染出来的名字」保证是同一个。
    """
    obs_metrics.observe(M.AGENT_DURATION.removesuffix("_seconds"), 0.02, {"node": "rag"})
    assert any(M.AGENT_DURATION in key for key in obs_metrics.snapshot()["histograms"])


# ── 三件工具的边界：指标里不该出现别的东西 ────────────────────────────────────


def test_token_and_cost_recording_matches_documented_contract(obs_metrics: M.Metrics) -> None:
    """`record_tokens` / `record_cost` 的账目口径（`13.cache.md`/`14.eval.md` 的对比基数）。"""
    M.record_tokens(obs_metrics, model="m1", prompt=100, completion=20, total=125)
    M.record_cost(obs_metrics, model="m1", cost_usd=0.0012)

    text = obs_metrics.render()
    assert 'shopsage_llm_tokens_total{kind="prompt",model="m1"} 100.0' in text
    assert 'shopsage_llm_tokens_total{kind="completion",model="m1"} 20.0' in text
    # total 单独记（125 ≠ 100+20：provider 的 total 可能含 reasoning token）
    assert 'shopsage_llm_tokens_total{kind="total",model="m1"} 125.0' in text
    assert 'shopsage_llm_cost_usd_total{model="m1"} 0.0012' in text


def test_unknown_cost_is_not_recorded_as_zero(obs_metrics: M.Metrics) -> None:
    """`cost_usd=None`（定价表里没有这个模型）**不计入**，而不是记 0。

    记 0 会让成本看板「看起来完整、实际偏低」—— 而偏低的成本不会触发任何告警，
    这是「成本失控」最可能的起点（本地模型、自建网关都算不出价格）。
    """
    M.record_cost(obs_metrics, model="local-model", cost_usd=None)
    assert M.LLM_COST not in obs_metrics.render()


def test_slow_buckets_used_for_llm_series(obs_metrics: M.Metrics) -> None:
    """LLM 系列用慢桶（100ms 起），其余用快桶（10ms 起）。

    桶选错的后果是**分位数被钉死在桶边界上**：用 10ms 起的细桶量 LLM 调用，
    前几个桶永远是 0（白占序列）；用 100ms 起的粗桶量本地 SQL，所有值挤进第一个桶。
    两者都不会报错 —— 只会让分位数失去意义。
    """
    obs_metrics.observe(M.AGENT_DURATION, 0.02, {"node": "router"})
    obs_metrics.observe(M.LLM_DURATION, 1.5, {})

    fast = M.buckets_of(obs_metrics, f'{M.AGENT_DURATION}{{node="router"}}')
    slow = M.buckets_of(obs_metrics, M.LLM_DURATION)
    assert min(fast) == pytest.approx(0.0001), "用例注入了细桶，应当生效"
    assert min(slow) == pytest.approx(0.001)


def test_reset_clears_values_but_keeps_collectors(obs_metrics: M.Metrics) -> None:
    """`reset()` 清值但保留 collector（否则 reset 之后写入的数据会进「幽灵副本」）。"""
    obs_metrics.counter(M.REQUEST_COUNT, {"endpoint": "GET /health", "status_class": "2xx"})
    obs_metrics.reset()
    assert obs_metrics.snapshot()["counters"] == {}

    # reset 之后还能继续写（collector 没有被注销掉）
    obs_metrics.counter(M.REQUEST_COUNT, {"endpoint": "GET /health", "status_class": "2xx"})
    assert 'shopsage_requests_total{endpoint="GET /health",status_class="2xx"} 1.0' in obs_metrics.render()


def test_quantiles_helper_reports_p50_p90_p95_p99(obs_metrics: M.Metrics) -> None:
    """`quantiles()` 一次给出四个常用分位（演示脚本与看板口径一致）。"""
    for index in range(1, 101):
        obs_metrics.observe(M.AGENT_DURATION, index / 1000.0, {"node": "synthesis"})
    values = M.quantiles(obs_metrics, f'{M.AGENT_DURATION}{{node="synthesis"}}')
    assert set(values) == {"p50", "p90", "p95", "p99"}
    assert all(
        value is not None and not math.isnan(value) for value in values.values()
    ), f"有分位数算不出来：{values}"
    # P90/P95/P99 必须严格大于 P50（这条最便宜地证明「分桶真的被用上了」）
    assert values["p50"] < values["p90"] < values["p95"] < values["p99"]
