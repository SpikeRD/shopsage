"""LLM 统一门面：全项目**唯一**允许出现 `litellm.*` 调用的地方。

硬约束（`docs/modules/5.llm.md` 第 1 节）：所有 Agent、工具、评测一律
`from app.llm import llm_call, embed`，不允许各自直接 `openai.chat.completions`。
绕过去的调用不会经过门面，于是重试/退避/（第 13 步的）熔断/降级/计费**全部失守**。
可用 `docs/runbook/5.llm-runbook.md` §11 的 grep 自查这一条。

本步（第 5 步）只做基础接入：单模型 + 指数退避重试（带 jitter）+ embedding。
fallback 链、熔断器、Langfuse 回调属第 13 步 —— 所以 `llm_call` 的签名里**刻意没有**
`fallback_models`：传进来会被静默忽略，那比「没有这个功能」更糟（调用方以为有降级，其实没有）。
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import litellm
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt

from app.config import get_settings
from app.llm.errors import (
    LLMError,
    PermanentLLMError,
    RetryableLLMError,
    is_permanent,
    is_retryable,
)

logger = logging.getLogger(__name__)

# 退避上限：单次等待最多 20s（不设上限时第 10 次要等 17 分钟，「重试」就变成「挂死」）
MAX_BACKOFF_SECONDS = 20.0
# 单次调用超时：交给 litellm 的 per-request timeout（不用全局 socket 超时）
DEFAULT_TIMEOUT = 30.0
# 默认重试次数（**总尝试次数 = 1 + max_retries**）
DEFAULT_MAX_RETRIES = 3

_T = TypeVar("_T")


def _wait(state: RetryCallState) -> float:
    """指数退避 + jitter：`min(2^n, cap)` 秒再乘一个随机系数。

    为什么必须带 jitter（`5.llm.md` 第 5 节）：无抖动时所有失败请求会在**同一时刻**重试，
    多副本/多 Agent 一起撞上去形成「惊群」，把刚缓过来的模型再打趴一次。
    乘 `random.uniform(0.5, 1.0)` 保留退避量级又错开时刻；不用 `random()`，
    因为它可能退避到接近 0，等于退避没生效。

    ⚠️ 本函数只读 `state.attempt_number`。`tests/test_llm_client_retry.py` 用「只有该属性的
    假对象」驱动它 —— 将来若改用别的字段，那些用例不会报错，只会静默地测一个假对象。
    """
    attempt = state.attempt_number
    return min(2.0**attempt, MAX_BACKOFF_SECONDS) * random.uniform(0.5, 1.0)


def make_retry_logger(max_retries: int) -> Callable[[RetryCallState], None]:
    """造一个 `before_sleep` 回调：把「第几次 / 共几次 / 什么错」打成一条日志。

    这条日志是「退避真的生效」的**主要证据来源**（比用 time 掐表稳得多，
    见 `docs/runbook/5.llm-runbook.md` §4.3）。

    为什么用闭包而不是在回调里问 tenacity：`RetryCallState` 只有
    `attempt_number` / `outcome` / `seconds_since_start` 这类很少的字段，
    **没有**「最多几次」；总次数在构造策略时就知道，闭包捕获最省事。
    """
    max_attempts = max_retries + 1

    def _log_retry(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        logger.warning(
            "LLM 调用失败，准备退避重试：attempt=%d/%d error=%s: %s",
            state.attempt_number,
            max_attempts,
            type(exc).__name__,
            exc,
        )

    return _log_retry


class _RetryPolicy(AsyncRetrying):
    """一份退避策略，`llm_call` / `embed` 共用（两处各写一份必然漂移）。

    `reraise=True`：重试用尽后抛**原始**异常（而不是 tenacity 的 RetryError 包一层）——
    原始异常带着 litellm 的诊断信息，包一层会让 `errors.is_permanent()` 的判定失准。
    原始异常随后由 `_run_with_retry()` 统一翻译成本模块的对外异常。
    """

    def __init__(self, max_retries: int) -> None:
        super().__init__(
            stop=stop_after_attempt(max_retries + 1),  # 1 次首发 + max_retries 次重试
            wait=_wait,
            retry=retry_if_exception(is_retryable),  # 不可恢复错误**不进**退避
            before_sleep=make_retry_logger(max_retries),
            reraise=True,
        )


@dataclass(frozen=True)
class LLMResponse:
    """结构化结果：第 11/13 步的 token/成本计量直接吃这些字段，调用方不必再解 litellm 对象。

    返回自定义结构而不是原样 `ModelResponse`，是为了让「计费口径只有一处」：
    否则每个 Agent 都要自己 `response.usage.prompt_tokens`、自己算 cost，
    「统一计量」就无处落脚。代价是丢掉了 litellm 的部分透传信息（如 `response.id`）——
    第 13 步接 Langfuse 若需要 trace id，就在这里加字段，依然只改 `_to_response` 一处。
    """

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float | None
    latency_ms: int


def _translate(exc: Exception, *, max_retries: int) -> LLMError:
    """把 litellm 原始异常翻译成本模块的两种对外错误（判据就是 errors.py 的分类表）。

    ⚠️ 这一步是**必须**的：策略里的 `reraise=True` 会直接抛 litellm 原始异常，
    而调用方（第 7/12 步）需要能区分「可以稍后重试」与「别重试了」——
    少了翻译，retryable/permanent 的区分就形同虚设。
    """
    if is_permanent(exc):
        return PermanentLLMError(f"LLM 不可恢复错误：{type(exc).__name__}: {exc}")
    return RetryableLLMError(
        f"LLM 重试 {max_retries} 次后仍失败：{type(exc).__name__}: {exc}"
    )


async def _run_with_retry(
    call: Callable[[], Awaitable[_T]], *, max_retries: int
) -> _T:
    """按退避策略执行 `call`，并把最终异常翻译成对外异常。

    抽成公共函数而不是在 llm_call/embed 各写一遍：两处各写一份必然漂移
    （尤其「不可恢复错误不重试」这条，很容易在某一份里漏掉）。

    ⚠️ **「不可恢复错误」优先抛出**：`is_retryable()` 为假时 tenacity 不重试、
    原异常直接冒泡，我们在这里把它翻成 `PermanentLLMError` ——
    这正是 `5.llm.md` 第 7 节 `test_no_fallback_on_4xx_param_error` 要的行为。
    """
    try:
        async for attempt in _RetryPolicy(max_retries):
            with attempt:
                return await call()
    except asyncio.CancelledError:
        # 取消不该被翻译成 LLM 错误（否则上层无法区分「任务被取消」与「模型不可用」）
        raise
    except LLMError:
        # ⚠️ 已经是本模块的对外异常（如 embed 自己抛的「条数不一致」）→ 原样抛出。
        #    漏掉这一条会把 PermanentLLMError 二次包装成 RetryableLLMError，
        #    于是「不可恢复」被误报成「稍后可重试」，第 12 步的队列会一直重排它。
        raise
    except Exception as exc:  # noqa: BLE001 - 统一翻译成 LLMError 的两个子类
        raise _translate(exc, max_retries=max_retries) from exc

    # 逻辑上不可达：上面的循环要么 return，要么抛异常。
    raise AssertionError("不可达：_RetryPolicy 未返回也未抛异常")


def _to_response(response: Any, latency_ms: int) -> LLMResponse:
    """把 litellm 响应翻译成 `LLMResponse`（第 13 步接 Langfuse 时只改这一处）。"""
    usage = getattr(response, "usage", None)
    try:
        # litellm 内置成本表；算不出来（如本地模型没有定价）时会抛错 → 用 None 表示「未知」，
        # 绝不能因为计费失败影响业务调用。
        cost: float | None = float(litellm.completion_cost(completion_response=response))
    except Exception:  # noqa: BLE001 - 计费是旁路功能
        cost = None

    choices = getattr(response, "choices", None) or []
    content = ""
    if choices:
        message = getattr(choices[0], "message", None)
        content = (getattr(message, "content", None) or "") if message is not None else ""

    return LLMResponse(
        content=content,
        model=getattr(response, "model", "") or "",
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        cost_usd=cost,
        latency_ms=latency_ms,
    )


async def llm_call(
    messages: list[dict[str, Any]],
    model: str | None = None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
    **kwargs: Any,
) -> LLMResponse:
    """调一次 LLM，带指数退避重试。

    参数（与 `5.llm.md` 第 2 节的接口一致）：
      messages     OpenAI 格式的消息列表
      model        主模型；None = 用 config 的 `llm_primary`
      max_retries  重试次数（总尝试 = 1 + max_retries）
      timeout      单次请求超时（秒）；**超时错误本身也是可重试的**
      **kwargs     透传给 litellm（temperature / max_tokens / …）

    抛出：`PermanentLLMError`（不可恢复，第一次就抛）/ `RetryableLLMError`（重试用尽）。
    """
    settings = get_settings()
    used_model = model or settings.llm_primary

    async def _call() -> LLMResponse:
        t0 = time.perf_counter()
        response = await litellm.acompletion(
            model=used_model,
            messages=messages,
            api_key=settings.llm_api_key.get_secret_value(),  # 唯一密钥入口
            timeout=timeout,
            # ⚠️ 必须显式关掉 litellm 自带重试，否则一次业务调用会是「两层重试」
            #    （4 × (1+N) 个请求），退避时间失控且会主动放大限流压力。
            #    ⚠️ `num_retries` **不在** acompletion 的具名参数里（走 **kwargs 透传），
            #    所以参数名写错不会报错、只会静默不生效 —— 唯一的哨兵是
            #    tests/test_llm_client_retry.py 里的计数断言。
            num_retries=0,
            **kwargs,
        )
        return _to_response(response, int((time.perf_counter() - t0) * 1000))

    return await _run_with_retry(_call, max_retries=max_retries)


async def embed(
    texts: Sequence[str],
    model: str | None = None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[list[float]]:
    """把一批文本向量化；**顺序与入参严格一致**（调用方依赖这一点做对齐）。

    embedding 也必须在门面里：它同样花钱、同样有延迟，第 10 步 rag 检索与第 13 步 cache 判相似
    都会高频调用。若各处自行 `openai.embeddings.create(...)`，就会出现改不了超时、加不了重试、
    计不了费、也没有「切换 embedding 模型」的单一开关。

    ⚠️ **保序**：litellm 响应里每个 data 项带 `index`，这里**按 index 排序**后返回，
    而不是假定返回顺序（批量并发时顺序不保证 —— 这是最隐蔽的一类 bug，
    症状是「检索偶尔答非所问」）。同时校验条数：数量对不上就抛错，
    绝不返回一个「长度不对但顺序看着对」的列表。

    抛出：同 `llm_call`。
    """
    items = [str(text) for text in texts]
    if not items:
        # 空输入不该发请求：既省一次计费，也避免某些 provider 对空批返回怪异结构
        return []

    settings = get_settings()
    used_model = model or settings.embedding_model

    async def _call() -> list[list[float]]:
        response = await litellm.aembedding(
            model=used_model,
            input=items,
            api_key=settings.llm_api_key.get_secret_value(),  # 唯一密钥入口
            timeout=timeout,
            num_retries=0,  # 同上：两层重试必须避免
        )
        data = list(getattr(response, "data", None) or [])
        if len(data) != len(items):
            raise PermanentLLMError(
                f"embedding 返回条数与入参不一致：期望 {len(items)}，实际 {len(data)}"
                "（不猜顺序，避免向量与文本错配）"
            )
        # 按 index 排序后再取值：litellm 批量返回的顺序不保证与入参一致
        ordered = sorted(data, key=lambda item: item["index"])
        return [list(item["embedding"]) for item in ordered]

    return await _run_with_retry(_call, max_retries=max_retries)


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "MAX_BACKOFF_SECONDS",
    "LLMResponse",
    "embed",
    "llm_call",
]
