"""Langfuse 用例：**LLM 的 token / 成本 / 质量**（`11.observability.md` §7 第三条）。

| 用例 | 它钉住的错误改动 |
|---|---|
| `test_langfuse_records_tokens` | 「接了 Langfuse 但没记 token/成本」—— 成本看板永远是空的 |
| `test_quality_is_recorded_as_score` | 「质量分写进 metadata」（那就只能人肉翻，无法聚合/画趋势） |
| `test_quality_is_clamped` | 「1.4 分的质量分」进趋势图（一个异常值会把整条线压平） |
| `test_trace_context_*` | 「trace id 没传下去」→ 两个系统对不上账；**更糟**：非法 id 让 SDK 抛 `ValueError` |
| `test_scores_must_be_inside_the_observation` | 「打分在 `with` 块外」（SDK 静默跳过，一条都不会到看板） |
| `test_disabled_handler_never_touches_network` | 「没配密钥还是构造了 client」→ 真的去连网（测试里每条用例多 1.5~3s） |
| `test_submit_never_raises` | 「上报失败把 LLM 调用打成 500」 |
| `test_flush_is_never_on_the_request_path` | 「顺手在请求里 flush」（实测会阻塞 2~24s） |

## 为什么全部用假 client

真的 Langfuse client 在**给假密钥**时会：构造花 1.5~3s、起 3~4 个后台线程、
`flush()` 对不可达端点阻塞 **2.0~23.6s**（记过 score 之后 6~13s）。
在测试里用真 client 等于把「跑一条用例」变成「等它超时」——
而本模块要验证的是**我们自己的账目口径与防御逻辑**，那些全在假 client 上可断言。
"""

from __future__ import annotations

import re
import threading
import time

import pytest

from app.observability import langfuse_cb
from app.observability.langfuse_cb import (
    TRACE_ID_RE,
    LangfuseHandler,
    _trace_context,
    _usage_details,
    langfuse_handler,
)

HEX32 = "0123456789abcdef0123456789abcdef"


# ── §7 第三条：LLM 调用后有 token / 耗时 / cost 记录 ──────────────────────────


def test_langfuse_records_tokens(langfuse) -> None:
    """`11.observability.md` §7 第三条：LLM 调用后必须有 token / 耗时 / cost 记录。"""
    handler, client = langfuse

    with handler.record(model="deepseek-chat", user_id="2", session_id="s-1") as rec:
        rec.tokens(prompt=1200, completion=380, total=1580).cost(0.00042)

    assert len(client.observations) == 1
    sent = client.observations[0]
    assert sent["as_type"] == "generation", "必须用 generation 类型（只有它会做成本/延迟聚合）"
    assert sent["model"] == "deepseek-chat"
    assert sent["usage_details"] == {"input": 1200, "output": 380, "total": 1580}
    assert sent["cost_details"] == {"total": pytest.approx(0.00042)}
    # 时刻信息进 metadata（Langfuse 的延迟面板不读它，但记录要能自解释）
    assert sent["metadata"]["latency_ms"] == 0

    finding = handler.findings[0]
    assert finding.exported is True, "上报成功后必须标记 exported"
    assert finding.observation_id, "必须拿到 observation id（与 trace 对齐用）"


def test_total_tokens_falls_back_to_sum_when_absent(langfuse) -> None:
    """只给了 prompt/completion 时，`total` 用两者之和兜底（provider 没给 total 的场合）。"""
    handler, client = langfuse
    with handler.record(model="m") as rec:
        rec.tokens(prompt=10, completion=5)
    assert client.observations[0]["usage_details"]["total"] == 15


def test_recorder_works_without_with_block(langfuse) -> None:
    """生产路径（`app/llm/client.py`）用 `open()` 而不是 `with`（见模块 docstring）。"""
    handler, client = langfuse
    rec = handler.open(model="m")
    rec.tokens(prompt=1, completion=2).cost(None)
    finding = rec.done()
    assert finding.exported is True
    assert client.observations[0]["cost_details"] is None, "未知成本不传（而不是传 0）"


def test_recorder_done_is_idempotent(langfuse) -> None:
    """重复 `done()` 不会重复上报（生产路径在 `finally` 里调，可能被调两次）。"""
    handler, client = langfuse
    rec = handler.open(model="m")
    rec.done()
    rec.done()
    assert len(client.observations) == 1


# ── 质量：必须进 score ────────────────────────────────────────────────────────


def test_quality_is_recorded_as_score(langfuse) -> None:
    """质量分必须走 **score**（而不是 metadata）—— `14.eval.md` 要对齐「成本 vs 质量」。

    score 是 Langfuse 里能聚合、能画趋势、能按数据集筛选的一等公民；
    metadata 只是挂在单条 trace 上的一段 JSON。写错地方的后果不是报错，
    而是「质量数据永远无法聚合」——那正是这一层要回答的问题之一。
    """
    handler, client = langfuse
    with handler.record(model="m") as rec:
        rec.tokens(prompt=1, completion=1).quality(name="answer_relevance", value=0.86)

    assert client.scores == [{"name": "answer_relevance", "value": pytest.approx(0.86)}]


def test_quality_is_clamped_into_unit_range(langfuse) -> None:
    """超范围的质量分被夹住（写原始分请用 metadata）。

    一个 1.4 分的质量分在趋势图里是异常值，会把整条线的尺度带偏 ——
    而它**不会**被 Langfuse 拒绝（NUMERIC score 的区间是约定而非校验）。
    """
    handler, _client = langfuse
    with handler.record(model="m") as rec:
        rec.quality(name="high", value=1.4).quality(name="low", value=-0.2)
    assert rec.finding.scores == {"high": 1.0, "low": 0.0}


def test_all_quality_scores_are_emitted(langfuse) -> None:
    """多个 score 都要上报（不能只发最后一个）。"""
    handler, client = langfuse
    with handler.record(model="m") as rec:
        rec.quality(name="a", value=0.5).quality(name="b", value=0.7).quality(name="c", value=0.9)
    assert {item["name"] for item in client.scores} == {"a", "b", "c"}


def test_manual_score_for_remote_trace(langfuse) -> None:
    """`score(trace_id=...)` 走 `create_score`（评测回填指定 trace 的入口）。"""
    handler, client = langfuse
    assert handler.score(name="ragas_faithfulness", value=0.91, trace_id=HEX32, comment="评测回填") is True
    assert client.scores[-1]["trace_id"] == HEX32
    assert client.scores[-1]["comment"] == "评测回填"


# ── trace id 的接线（本步最容易出事的一处）───────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "",  # 空：没有活跃 span
        "abc123",  # 短十六进制：SDK 会静默左补零 → 对不上账的 trace
        "abc123defg",  # 非十六进制：SDK 直接 ValueError（把 LLM 调用打成 500）
        "550e8400-e29b-41d4-a716-446655440000",  # UUID 带横杠：同样 ValueError
        "A" * 32,  # 大写：SDK 会小写化（本次仍能关联，但形状不合约定）
        "a" * 40,  # 过长
    ],
)
def test_trace_context_rejects_malformed_ids(bad: str) -> None:
    """不合格的 trace id **不传** —— 宁可少一次关联，也不能让埋点把请求打崩。

    这三条都是实测出来的：`"abc123defg"` 与带横杠的 UUID 会让
    `int(x, 16)` 抛 `ValueError`（SDK 只在日志里说 "Ignoring trace ID"，行为却是崩）；
    `"abc123"` 被静默补零成 `00…abc123`，得到一条**看起来对、实际对不上**的 trace。
    """
    assert _trace_context(bad) is None


def test_trace_context_accepts_canonical_id() -> None:
    """规范的 32 位小写十六进制照原样传下去（这是 OTEL 自己的格式）。"""
    assert _trace_context(HEX32) == {"trace_id": HEX32}


def test_trace_id_linkage_is_actually_sent(langfuse) -> None:
    """**回归**：`finding.trace_id` 必须真的出现在 `trace_context` 里。

    本步第一版算出了 `trace_id` 却**没有传给 SDK** —— 于是「Grafana 上看 P99 →
    去 Langfuse 查那次调用」这条路径根本走不通（两个系统的 trace 是两条），
    而模块 docstring 里写着「两个系统能对上账」。这条用例就是那个承诺的哨兵。
    """
    handler, client = langfuse
    # 手工造一条带 trace id 的 finding（模拟「在一个活跃 span 里记录」）
    finding = langfuse_cb.LLMFinding(model="m", trace_id=HEX32)
    handler.submit(finding)

    assert client.observations[0]["trace_context"] == {"trace_id": HEX32}


def test_trace_id_captured_from_active_span(telemetry) -> None:
    """有活跃 span 时，`_current_trace_id()` 抓到的是那个 span 的 trace id（32 位十六进制）。"""
    from app.observability import tracing as T

    handler = langfuse_handler()
    with T.trace_span("llm.call", kind="llm"):
        captured = handler._current_trace_id()
    assert TRACE_ID_RE.match(captured), f"不是规范的 trace id：{captured!r}"


def test_no_active_span_yields_empty_trace_id() -> None:
    """没有活跃 span 时返回空串（而不是抛异常或返回垃圾）。"""
    assert langfuse_handler()._current_trace_id() == ""


# ── 打分必须依附当前 observation ──────────────────────────────────────────────


def test_scores_are_sent_inside_the_observation(langfuse) -> None:
    """**score 必须在 `start_as_current_observation` 的 `with` 块内发**。

    实测：`score_current_trace()` 依附「当前活跃的 observation」，
    在块外调用它**不抛异常**（只打一条 `Context error: No active span…`）然后静默返回 ——
    症状是「质量分这条链看着配好了、实际一条都不会到看板上」。
    所以这里在假 client 上装一个「块内/块外」探针，断言发 score 时块还开着。
    """
    handler, client = langfuse
    state: dict[str, bool] = {"open": False}

    original_observation = client.start_as_current_observation
    original_score = client.score_current_trace

    def tracking_observation(**kwargs: object):
        original_observation(**kwargs)  # 仍然要记下这次的入参
        return _RecordingProbe(state)

    def tracking_score(**kwargs: object) -> None:
        assert state["open"], "score 必须在 observation 的 with 块内发送（块外会被静默忽略）"
        original_score(**kwargs)

    client.start_as_current_observation = tracking_observation  # type: ignore[method-assign]
    client.score_current_trace = tracking_score  # type: ignore[method-assign]

    with handler.record(model="m") as rec:
        rec.tokens(prompt=1, completion=1).quality(name="q", value=0.5)

    assert client.scores, "score 应当被发出"
    assert client.scores[0]["name"] == "q"


class _RecordingProbe:
    """一个「进出时改写共享标志」的上下文管理器（供上面的用例断言块的开闭）。

    为什么不用 `contextlib.contextmanager` + `yield from manager.__enter__()`：
    `yield from` 期待的是可迭代对象，而 `__enter__()` 返回的是 **observation 对象** ——
    实测会抛 `TypeError: 'FakeObservation' object is not iterable`，
    被 `_export` 的兜底捕获之后表现为「上报失败」（用例红在一个与断言无关的地方）。
    """

    def __init__(self, state: dict[str, bool]) -> None:
        self._state = state
        self.id = "obs-probe"

    def __enter__(self) -> _RecordingProbe:
        self._state["open"] = True
        return self

    def __exit__(self, *args: object) -> bool:
        self._state["open"] = False
        return False


# ── 关闭状态：绝不联网、绝不抛 ────────────────────────────────────────────────


def test_disabled_handler_never_touches_network() -> None:
    """没配密钥时：不构造 client、不联网、但**账目仍然留在进程内**。

    「什么都不做」是无法观测的：看板上没有数据时分不清「没人用」还是「密钥配错了」。
    所以关闭状态必须能被断言（`state["reason"]`）并且仍然记 findings（可进日志/可查）。
    """
    handler = LangfuseHandler(disabled_reason="缺 public_key 或 secret_key")
    assert handler.enabled is False
    assert handler.state["reason"] == "缺 public_key 或 secret_key"

    started = time.perf_counter()
    with handler.record(model="m") as rec:
        rec.tokens(prompt=1, completion=1).cost(0.1).quality(name="q", value=0.5)
    handler.flush()
    handler.shutdown()
    handler.score(name="q", value=0.5)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1, f"关闭状态下不该有任何阻塞（实测 {elapsed:.3f}s）"
    assert len(handler.findings) == 1, "关闭时账目仍要留在进程内"
    assert handler.findings[0].exported is False


def test_from_settings_without_keys_builds_no_client() -> None:
    """`from_settings` 在缺密钥时**根本不构造 client**（连环境变量都不看）。

    ⚠️ 这条边界很窄但很重要：Langfuse 的构造函数用 `is None` 判断密钥，
    所以 `LANGFUSE_PUBLIC_KEY=""`（空串）走环境变量时会**绕过**那个判断，
    建出一个真的会连网重试的 client（实测构造花 1.86s、起 4 个线程）。
    本仓的 `.env` 里这两个键**就是空串**，所以本函数的选择是「自己看密钥、
    不全就返回空实现」，而不是「把空串交给 SDK 去判断」。
    """

    class Settings:
        langfuse_public_key = ""
        langfuse_secret_key = ""
        langfuse_host = ""

    handler = LangfuseHandler.from_settings(Settings())
    assert handler.enabled is False
    assert "缺" in handler.disabled_reason


def test_from_settings_handles_secret_str() -> None:
    """密钥可能是 `SecretStr`（`app.config.Settings` 里就是）—— 要能取到真值。"""
    from pydantic import SecretStr

    class Settings:
        langfuse_public_key = "pk-lf-test"
        langfuse_secret_key = SecretStr("")
        langfuse_host = ""

    handler = LangfuseHandler.from_settings(Settings())
    assert handler.enabled is False, "secret 为空时同样不构造"


def test_submit_never_raises_when_client_explodes(langfuse) -> None:
    """上报过程中 client 抛异常 → **被吞掉并告警**，绝不影响 LLM 调用。"""
    handler, client = langfuse

    def boom(**kwargs: object) -> None:
        raise RuntimeError("Langfuse 服务挂了")

    client.start_as_current_observation = boom  # type: ignore[method-assign]

    rec = handler.open(model="m")
    finding = rec.done()  # 不抛

    assert finding.exported is False
    assert len(handler.findings) == 1, "失败的账目仍要留在进程内（排障要靠它）"


def test_default_handler_is_disabled_before_configuration() -> None:
    """从未 `configure_langfuse` 时，默认 handler 是**关闭**的（不读 settings、不建 client）。"""
    langfuse_cb.set_langfuse_handler(None)
    handler = langfuse_handler()
    assert handler.enabled is False
    assert "setup_observability" in handler.disabled_reason


# ── flush / shutdown 的纪律 ───────────────────────────────────────────────────


def test_flush_skips_when_disabled_and_when_timeout_is_zero() -> None:
    """关闭时不 flush；`timeout_ms<=0` 时显式跳过（调用方声明「不想等」）。"""
    handler = LangfuseHandler(disabled_reason="off")
    handler.flush()  # 不抛、不阻塞

    from tests.observability.conftest import FakeLangfuseClient

    client = FakeLangfuseClient()
    enabled = LangfuseHandler(client=client, enabled=True)
    enabled.flush()
    assert client.flushes == 1
    enabled.flush(timeout_ms=0)
    assert client.flushes == 1, "timeout_ms<=0 应当跳过（而不是假装生效）"
    enabled.shutdown()
    assert client.shutdowns == 1


def test_flush_and_shutdown_never_raise() -> None:
    """client 的 flush/shutdown 抛异常时也不往上冒（关服路径不能被监控打断）。"""

    class Exploding:
        _base_url = "http://x"

        def flush(self) -> None:
            raise RuntimeError("nope")

        def shutdown(self) -> None:
            raise RuntimeError("nope")

    handler = LangfuseHandler(client=Exploding(), enabled=True)
    handler.flush()
    handler.shutdown()


def test_findings_are_bounded() -> None:
    """findings 必须有上限（跑一天的服务不能把 LLM 账目全留在内存里）。"""
    from tests.observability.conftest import FakeLangfuseClient

    handler = LangfuseHandler(client=FakeLangfuseClient(), enabled=True, max_findings=10)
    for index in range(50):
        handler.submit(langfuse_cb.LLMFinding(model=f"m{index}"))
    assert len(handler.findings) == 10
    assert handler.findings[-1].model == "m49", "保留的应当是最新的那批"


def test_concurrent_records_are_all_kept() -> None:
    """并发记录不丢账（`submit` 有锁；丢了的话成本会**静默偏低**）。"""
    from tests.observability.conftest import FakeLangfuseClient

    handler = LangfuseHandler(client=FakeLangfuseClient(), enabled=True, max_findings=2000)

    def worker() -> None:
        for _ in range(50):
            handler.submit(langfuse_cb.LLMFinding(model="m"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(handler.findings) == 400


def test_usage_details_returns_none_when_no_tokens() -> None:
    """一个 token 都没记时不传 `usage_details`（传全 0 会让看板出现无意义的空记录）。"""
    assert _usage_details(langfuse_cb.LLMFinding()) is None
    assert _usage_details(langfuse_cb.LLMFinding(prompt_tokens=3)) == {
        "input": 3,
        "output": 0,
        "total": 3,
    }


def test_metadata_carries_user_and_session(langfuse) -> None:
    """`user_id` / `session_id` 进 metadata（Langfuse 里「按人/按会话看成本」的入口）。"""
    handler, client = langfuse
    with handler.record(model="m", user_id="2", session_id="s-1") as rec:
        rec.tokens(prompt=1, completion=1)
    meta = client.observations[0]["metadata"]
    assert meta["user_id"] == "2" and meta["session_id"] == "s-1"


def test_quality_range_constant_matches_documented_contract() -> None:
    """质量分区间常量与 `11.observability.md` / `14.eval.md` 的口径一致。"""
    assert langfuse_cb.QUALITY_RANGE == (0.0, 1.0)
    assert re.match(r"^\^\[0-9a-f\]\{32\}\$$", TRACE_ID_RE.pattern.replace("\\", ""))
