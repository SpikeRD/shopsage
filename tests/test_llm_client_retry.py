"""`app/llm/client.py` 的重试行为测试：**不联网、不需要真 key**。

对应 `docs/modules/5.llm.md` 第 7 节：
  - `test_retry_on_429`：前两次 429、第三次成功 → 最终成功且重试了 2 次
  - `test_no_fallback_on_4xx_param_error`：参数错误既不降级也不重试

⚠️ 关退避的正确姿势是**替换策略类**（`_RetryPolicy`），不要 monkeypatch `_wait`：
`_RetryPolicy.__init__` 里写的是 `wait=_wait`（捕获的是函数对象），
替换模块属性改不到它 —— 你以为关掉了，用例却在真等 2/4/8 秒（症状：套件莫名变慢）。
"""

import asyncio
import logging
import time

import litellm
import pytest
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from app.llm import client as llm_client
from app.llm.errors import PermanentLLMError, RetryableLLMError, is_retryable


class FastRetryPolicy(AsyncRetrying):
    """与 `_RetryPolicy` 同语义但 `wait=0`：单测测的是**次数**，不是时长。"""

    def __init__(self, max_retries: int) -> None:
        super().__init__(
            stop=stop_after_attempt(max_retries + 1),
            wait=lambda state: 0,
            retry=retry_if_exception(is_retryable),
            reraise=True,
        )


@pytest.fixture
def fake_settings(monkeypatch):
    """钉住配置：模型名与 key 都用假值，绝不读本机 .env。"""
    monkeypatch.setenv("LLM_PRIMARY", "gpt-4o-mini")
    monkeypatch.setenv("LLM_API_KEY", "sk-fake-for-test")
    monkeypatch.setenv("LLM_FALLBACK", "")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fast_retry(monkeypatch):
    """把真实退避换成 0 等待（同语义），并返回调用记录。"""
    monkeypatch.setattr(llm_client, "_RetryPolicy", FastRetryPolicy)
    return FastRetryPolicy


def _rate_limit(**kwargs):
    return litellm.exceptions.RateLimitError(
        message="rate limited", llm_provider="openai", model=kwargs.get("model")
    )


def _mock_response(text: str = "好的"):
    """用 litellm 自己的 mock 造一个**形状真实**的响应。

    比手搓 `SimpleNamespace(choices=[...], usage=...)` 稳：后者会在 litellm 改内部结构时静默变假。
    实测该 mock 的 usage 固定为 prompt=10 / completion=20 / total=30。
    """
    return litellm.completion(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        mock_response=text,
        api_key="sk-fake",
    )


MESSAGES = [{"role": "user", "content": "hi"}]


# ── 核心：429 退避重试（A1 基础） ─────────────────────────────────


def test_retry_on_429_then_success(monkeypatch, fake_settings, fast_retry):
    """前两次 429、第三次成功 → 最终成功，且**恰好**重试了 2 次（5.llm.md §7 第 1 条）。"""
    calls = {"n": 0}

    async def fake_acompletion(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _rate_limit(**kwargs)
        return _mock_response("好的")

    monkeypatch.setattr(llm_client.litellm, "acompletion", fake_acompletion)

    resp = asyncio.run(llm_client.llm_call(MESSAGES, max_retries=3))

    assert resp.content == "好的"
    assert calls["n"] == 3, f"期望 3 次尝试（1 首发 + 2 重试），实际 {calls['n']}"


def test_litellm_internal_retry_is_disabled(monkeypatch, fake_settings, fast_retry):
    """`num_retries=0` 必须真的传下去（两层重试的哨兵）。

    `num_retries` 不在 `acompletion` 的具名参数里（走 **kwargs 透传），
    所以**参数名写错不会报错，只会静默地不生效** —— 断言收到的 kwargs 是唯一的哨兵。
    """
    seen: dict[str, object] = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)
        return _mock_response("ok")

    monkeypatch.setattr(llm_client.litellm, "acompletion", fake_acompletion)
    asyncio.run(llm_client.llm_call(MESSAGES))

    assert seen.get("num_retries") == 0, f"litellm 自带重试必须关掉，实际 num_retries={seen.get('num_retries')!r}"
    assert seen.get("api_key") == "sk-fake-for-test", "密钥应来自 Settings 且是明文（只传给 litellm）"
    assert seen.get("model") == "gpt-4o-mini", "默认应使用 config 的 llm_primary"
    assert seen.get("timeout") == llm_client.DEFAULT_TIMEOUT


def test_retry_exhausted_raises_retryable(monkeypatch, fake_settings, fast_retry):
    """**一直** 429 → 重试用尽后抛 `RetryableLLMError`，尝试次数 = 1 + max_retries。"""
    calls = {"n": 0}

    async def always_429(**kwargs):
        calls["n"] += 1
        raise _rate_limit(**kwargs)

    monkeypatch.setattr(llm_client.litellm, "acompletion", always_429)

    with pytest.raises(RetryableLLMError):
        asyncio.run(llm_client.llm_call(MESSAGES, max_retries=3))
    assert calls["n"] == 4, f"1 首发 + 3 重试 = 4 次，实际 {calls['n']}"


@pytest.mark.parametrize("max_retries", [0, 1, 5])
def test_total_attempts_follows_max_retries(monkeypatch, fake_settings, fast_retry, max_retries):
    """尝试次数 = max_retries + 1（边界：0 表示「只打一次」）。"""
    calls = {"n": 0}

    async def always_429(**kwargs):
        calls["n"] += 1
        raise _rate_limit(**kwargs)

    monkeypatch.setattr(llm_client.litellm, "acompletion", always_429)
    with pytest.raises(RetryableLLMError):
        asyncio.run(llm_client.llm_call(MESSAGES, max_retries=max_retries))
    assert calls["n"] == max_retries + 1, f"实际打了 {calls['n']} 次"


def test_timeout_is_retried_then_succeeds(monkeypatch, fake_settings, fast_retry):
    """超时也是可恢复错误 → 重试（`5.llm.md` §7 `test_fallback_on_timeout` 的基础）。"""
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise litellm.exceptions.Timeout(message="timeout", llm_provider="openai", model=kwargs.get("model"))
        return _mock_response("终于好了")

    monkeypatch.setattr(llm_client.litellm, "acompletion", flaky)
    resp = asyncio.run(llm_client.llm_call(MESSAGES, max_retries=2))

    assert resp.content == "终于好了"
    assert calls["n"] == 2


# ── 核心：不可恢复错误一次都不重试 ────────────────────────────────


def test_no_retry_on_bad_request(monkeypatch, fake_settings, fast_retry):
    """400 参数错误：**一次都不重试**，抛 `PermanentLLMError`（5.llm.md §7 第 4 条）。"""
    calls = {"n": 0}

    async def fake_acompletion(**kwargs):
        calls["n"] += 1
        raise litellm.exceptions.BadRequestError(
            message="invalid parameter", llm_provider="openai", model=kwargs.get("model")
        )

    monkeypatch.setattr(llm_client.litellm, "acompletion", fake_acompletion)

    with pytest.raises(PermanentLLMError):
        asyncio.run(llm_client.llm_call(MESSAGES, max_retries=3))

    assert calls["n"] == 1, f"400 不该重试，实际打了 {calls['n']} 次"


def test_no_retry_on_auth_error(monkeypatch, fake_settings, fast_retry):
    """401 key 错：不重试（否则每个副本都在打厂商鉴权接口，放大故障）。"""
    calls = {"n": 0}

    async def fake_acompletion(**kwargs):
        calls["n"] += 1
        raise litellm.exceptions.AuthenticationError(
            message="invalid key", llm_provider="openai", model=kwargs.get("model")
        )

    monkeypatch.setattr(llm_client.litellm, "acompletion", fake_acompletion)

    with pytest.raises(PermanentLLMError) as excinfo:
        asyncio.run(llm_client.llm_call(MESSAGES, max_retries=3))

    assert calls["n"] == 1
    # 报错文本要能看出是哪一类，方便分诊（但不得含密钥明文）
    assert "AuthenticationError" in str(excinfo.value)
    assert "sk-fake-for-test" not in str(excinfo.value)


# ── 退避函数的量级 / 抖动 / 上限 ──────────────────────────────────


class _FakeState:
    """`_wait()` 只读 `attempt_number`，所以假对象只要这一个属性即可。"""

    def __init__(self, attempt_number: int) -> None:
        self.attempt_number = attempt_number


def test_backoff_is_exponential_and_jittered():
    """退避量级：第 n 次约 2^n 秒，带抖动，且随次数单调不减。

    ⚠️ 断言量级与单调性，**不要断言精确秒数** —— jitter 的存在就是为了让它不是定值。
    """
    mins = []
    for n in range(1, 5):
        ceiling = min(2.0**n, llm_client.MAX_BACKOFF_SECONDS)
        samples = [llm_client._wait(_FakeState(n)) for _ in range(50)]
        assert min(samples) >= ceiling * 0.5 - 1e-9, f"第 {n} 次退避低于 jitter 下界"
        assert max(samples) <= ceiling + 1e-9, f"第 {n} 次退避超过 min(2^n, cap)"
        assert len(set(samples)) > 1, "没有抖动 → 多副本会同时重试（惊群）"
        mins.append(min(samples))
    assert mins == sorted(mins), "退避应随尝试次数单调不减"


def test_max_backoff_is_capped():
    """第 10 次重试不应等 1024 秒 —— 上限 `MAX_BACKOFF_SECONDS` 必须生效。"""
    samples = [llm_client._wait(_FakeState(10)) for _ in range(50)]
    assert max(samples) <= llm_client.MAX_BACKOFF_SECONDS
    assert max(samples) >= llm_client.MAX_BACKOFF_SECONDS * 0.5


def test_retry_logger_reports_attempt_and_max(caplog):
    """`before_sleep` 日志要能当证据：第几次 / 共几次 / 什么错。"""
    logger = llm_client.make_retry_logger(3)
    state = _FakeState.__new__(_FakeState)
    state.attempt_number = 2
    state.outcome = _FakeOutcome(_rate_limit(model="gpt-4o-mini"))

    with caplog.at_level(logging.WARNING, logger="app.llm.client"):
        logger(state)

    text = caplog.text
    assert "attempt=2/4" in text, f"应打印 attempt=2/4，实际：{text}"
    assert "RateLimitError" in text


class _FakeOutcome:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def exception(self) -> BaseException:
        return self._exc


# ── 真实退避的端到端证据（保留真实等待，故意慢） ──────────────────


def test_retry_really_waits(monkeypatch, fake_settings, caplog):
    """**不替换** `_RetryPolicy`：证明 `before_sleep` 真的在等（证据 2）。

    ⚠️ 这条用例故意慢（约 1–3 秒）。它存在的意义是「退避不是只写在代码里」：
    日志里有两条 attempt 记录，且墙钟时间 ≥ 两次等待之和的下界。
    """
    calls = {"n": 0}

    async def two_then_ok(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _rate_limit(**kwargs)
        return _mock_response("ok")

    monkeypatch.setattr(llm_client.litellm, "acompletion", two_then_ok)

    # 两次退避的下界：min(2^1,cap)*0.5 + min(2^2,cap)*0.5 = 1 + 2 = 3s；给点余量
    floor = (min(2.0**1, llm_client.MAX_BACKOFF_SECONDS) + min(2.0**2, llm_client.MAX_BACKOFF_SECONDS)) * 0.5
    t0 = time.perf_counter()
    with caplog.at_level(logging.WARNING, logger="app.llm.client"):
        resp = asyncio.run(llm_client.llm_call(MESSAGES, max_retries=3))
    elapsed = time.perf_counter() - t0

    assert resp.content == "ok"
    assert calls["n"] == 3
    assert "attempt=1/4" in caplog.text and "attempt=2/4" in caplog.text, caplog.text
    assert elapsed >= floor * 0.95, f"实际只用了 {elapsed:.2f}s，小于退避下界 {floor:.2f}s —— 退避没生效"


# ── 响应翻译 / 取消语义 ──────────────────────────────────────────


def test_response_fields_are_populated(monkeypatch, fake_settings, fast_retry):
    """`LLMResponse` 的 token/成本/延迟字段要真有数（第 11/13 步计费的输入）。"""
    monkeypatch.setattr(
        llm_client.litellm, "acompletion", lambda **kw: _async_return(_mock_response("hi"))
    )

    resp = asyncio.run(llm_client.llm_call(MESSAGES))

    assert resp.content == "hi"
    assert resp.model == "gpt-4o-mini"
    assert (resp.prompt_tokens, resp.completion_tokens, resp.total_tokens) == (10, 20, 30)
    assert resp.cost_usd is not None and resp.cost_usd > 0
    assert resp.latency_ms >= 0


def test_cost_failure_does_not_break_the_call(monkeypatch, fake_settings, fast_retry):
    """计费算不出来（本地模型没有定价表）时，`cost_usd=None` 而**不能**影响业务返回。"""
    monkeypatch.setattr(
        llm_client.litellm, "acompletion", lambda **kw: _async_return(_mock_response("hi"))
    )

    def boom(**kwargs):
        raise ValueError("no pricing for this model")

    monkeypatch.setattr(llm_client.litellm, "completion_cost", boom)
    resp = asyncio.run(llm_client.llm_call(MESSAGES))

    assert resp.content == "hi"
    assert resp.cost_usd is None


def test_custom_model_overrides_primary(monkeypatch, fake_settings, fast_retry):
    """显式传 `model=` 时不应被 `llm_primary` 覆盖。"""
    seen: dict[str, object] = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)
        return _mock_response("ok")

    monkeypatch.setattr(llm_client.litellm, "acompletion", fake_acompletion)
    asyncio.run(llm_client.llm_call(MESSAGES, model="gpt-4o"))

    assert seen["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_cancellation_is_not_translated(monkeypatch, fake_settings, fast_retry):
    """`asyncio.CancelledError` 必须原样抛出：不能翻成 LLMError。

    否则上层（第 9/12 步）无法区分「任务被取消」与「模型不可用」，
    会把取消当成故障重排队列。
    """

    async def cancelled(**kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(llm_client.litellm, "acompletion", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await llm_client.llm_call(MESSAGES)


def _async_return(value):
    async def _inner():
        return value

    return _inner()
