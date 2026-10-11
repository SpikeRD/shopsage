"""`tests/observability/` 的局部装置：**一份干净的 telemetry + 一份假的 Langfuse**。

## 四条纪律（与 `tests/api/conftest.py` 同源）

1. **不联网**：telemetry 的 `otel_endpoint` 一律留空 → 只挂内存 exporter
   （不建 OTLP exporter、不重试、没有后台线程）；
2. **不构造真 Langfuse client**：只注入假 client（真 client 的构造要 1.5~3s 且会起线程）；
3. **不污染别的用例**：每个用例一份全新的 `Metrics` + `TracerProvider`，
   通过 `use_telemetry()` 临时设成「进程根」—— 块外自动恢复；
4. **不读本机 `.env` 的模型配置**：会调用 `llm_call` 的用例必须显式钉住环境变量
   （见 `tests/test_llm_client_retry.py` 里那条注释踩过的坑）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.observability import langfuse_cb, metrics as metrics_mod
from app.observability.langfuse_cb import LangfuseHandler
from app.observability.metrics import Metrics, MetricsSettings
from app.observability.tracing import Telemetry, configure_telemetry, use_telemetry

#: 上一条用例关闭埋点后必须复位（否则「后面的用例没有 span」会被误判成代码坏了）
@pytest.fixture(autouse=True)
def _restore_langfuse_default() -> Iterator[None]:
    """每条用例结束后把进程默认 Langfuse handler 换回「未配置」状态。

    必要性：`langfuse_cb` 的默认 handler 是**进程级**的，而用例会注入自己的假 handler ——
    不清的话，下一个用例的 `langfuse_handler()` 会拿到上一个用例的假 client，
    于是「没配密钥时应当静默」这类断言会莫名失败（顺序依赖，最难查）。
    """
    previous = langfuse_cb._DEFAULT  # noqa: SLF001 - 装置就是要动进程级状态
    yield
    langfuse_cb.set_langfuse_handler(previous)


@pytest.fixture
def obs_metrics() -> Metrics:
    """一份干净、且**桶边界很细**的指标注册表。

    ⚠️ 桶为什么必须变细（本步的一个真实约束）：这些用例在离线环境里跑，
    被观测的系统耗时是**微秒级**（假模型、内存 saver），
    而生产桶的最小边界是 10ms —— 所有观测值都落进第一个桶，
    「能算出 P99」这条用例就变成了「在第一个桶上恒真」的空话
    （它会在任何实现下都通过，包括把 P99 算成 0 的错误实现）。
    """
    return metrics_mod.new_metrics(
        settings=MetricsSettings(
            fast_buckets=(0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
            slow_buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0),
        )
    )


@pytest.fixture
def telemetry(obs_metrics: Metrics) -> Iterator[Telemetry]:
    """一份干净的 telemetry，并在用例期间设成**进程根**（块外自动恢复）。

    为什么不直接用 `configure_telemetry()` 的返回值：那只是「进程默认」，
    而埋点（`instrument._metrics()`）解析的是 `telemetry()` ——
    它优先看**请求根**（`set_request_root`）。用 `use_telemetry()` 才是
    「这个 with 块里的所有埋点都进这一份」，与 API app 的行为一致。
    """
    instance = configure_telemetry(
        enabled=True,
        service_name="shopsage-test",
        otel_endpoint="",  # 关键：空 = 只留内存 exporter（不建 OTLP、不重试）
        metrics=obs_metrics,
    )
    with use_telemetry(instance):
        yield instance


@pytest.fixture()
def langfuse(telemetry: Telemetry) -> Iterator[tuple[LangfuseHandler, "FakeLangfuseClient"]]:
    """注入一个**假** Langfuse client（记录所有上报调用，零网络）。"""
    client = FakeLangfuseClient()
    handler = LangfuseHandler(client=client, enabled=True, telemetry=telemetry)
    langfuse_cb.set_langfuse_handler(handler)
    yield handler, client


class FakeObservation:
    """假 observation：只需要 `id`（`_export` 会读它）。"""

    def __init__(self, observation_id: str = "obs-fake-1") -> None:
        self.id = observation_id


class FakeLangfuseClient:
    """把 Langfuse SDK 的**被调用形状**记下来，供用例断言。

    只实现本模块真的会用到的那几个方法（`start_as_current_observation` /
    `score_current_trace` / `create_score` / `flush` / `shutdown`）——
    多实现一个「将来可能会用」的方法是自欺：它会让「模块改用了别的方法」
    这件事在测试里看不见（假 client 照样通过）。
    """

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.flushes = 0
        self.shutdowns = 0
        self._base_url = "http://langfuse-fake"

    def start_as_current_observation(self, **kwargs: Any) -> Any:
        self.observations.append(dict(kwargs))
        import contextlib

        @contextlib.contextmanager
        def _cm() -> Iterator[FakeObservation]:
            yield FakeObservation(f"obs-fake-{len(self.observations)}")

        return _cm()

    def update_current_generation(self, **kwargs: Any) -> None:
        self.updates.append(dict(kwargs))

    def score_current_trace(self, **kwargs: Any) -> None:
        self.scores.append(dict(kwargs))

    def create_score(self, **kwargs: Any) -> None:
        self.scores.append({**dict(kwargs), "__remote__": True})

    def flush(self) -> None:
        self.flushes += 1

    def shutdown(self) -> None:
        self.shutdowns += 1


__all__ = [
    "FakeLangfuseClient",
    "FakeObservation",
    "LangfuseHandler",
    "Telemetry",
]
