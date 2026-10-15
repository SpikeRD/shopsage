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
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

# ⚠️⚠️ 顺序是**语义的一部分，不要重排**：必须在 `import litellm` 之前摆好 HF 缓存环境。
#
# 为什么：`huggingface_hub` 的缓存路径（HF_HOME / HF_HUB_CACHE）在**模块导入时**就被读成
# 常量固化了，之后再改环境变量**不生效**。而 `import litellm` 会连带导入
# `litellm.llms.huggingface.*`，于是 HF 常量在那一行就定死了 ——
# 实测把兜底放在函数里（懒加载处）是**无效**的，仍会去写 `~/.cache` 并抛
# `PermissionError`。`app/llm/hf_cache.py` 本身只 import os/pathlib，所以能安全地最先执行。
from app.llm.hf_cache import prepare_hf_cache

prepare_hf_cache()

import litellm  # noqa: E402 - 必须在 prepare_hf_cache() 之后（见上方说明）
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.llm.errors import (  # noqa: E402
    LLMError,
    PermanentLLMError,
    RetryableLLMError,
    is_permanent,
    is_retryable,
)
from app.observability.instrument import embed_span, llm_span, record_llm_call  # noqa: E402

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
    # `finish_reason`（如 "stop" / "length"）：**推理模型**（deepseek-*、o1 系等）会先把 token
    # 花在 `reasoning_content` 上，`max_tokens` 给小了就会出现「content 为空但调用成功」——
    # 有它就分得清「模型真的没说话」和「被截断了」（实测踩到，见 runbook §10.1 坑 8）。
    finish_reason: str = ""


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
    finish_reason = ""
    if choices:
        choice = choices[0]
        message = getattr(choice, "message", None)
        content = (getattr(message, "content", None) or "") if message is not None else ""
        finish_reason = str(getattr(choice, "finish_reason", "") or "")

    if not content and finish_reason == "length":
        # 不是「模型没说话」，而是被 max_tokens 截断了。推理模型（deepseek-* 等）会先把
        # token 花在 reasoning 上，`max_tokens` 太小就会走到这里 —— 这条日志能让排查少绕一大圈。
        logger.warning(
            "LLM 返回内容为空但 finish_reason=length（被 max_tokens 截断）："
            "model=%s completion_tokens=%s —— 推理模型会把 token 先花在 reasoning 上，请调大 max_tokens。",
            getattr(response, "model", "?"),
            getattr(usage, "completion_tokens", "?"),
        )

    return LLMResponse(
        content=content,
        model=getattr(response, "model", "") or "",
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        cost_usd=cost,
        latency_ms=latency_ms,
        finish_reason=finish_reason,
    )


def _resolve_credentials(model: str, settings: Any) -> tuple[str, str | None]:
    """按**最终要打的模型**解析凭据，返回 `(api_key, api_base)`。

    ⚠️ 为什么必须「按模型」而不是「在函数入口拿一次 llm_api_key」：
    降级链里 `used_model` 会在运行期从主模型切成备模型，若密钥在入口解析一次，
    降级那一刻用的仍是**主模型的钥匙** —— 跨厂商时必然 401，而 401 被判为不可恢复错误，
    于是「重试不重试、降级也不再降级」，可用性比不加降级还差。
    详见 docs/runbook/5.llm-runbook.md §1.2.1 与 §8.1。

    规则：
      - 模型 == `llm_fallback` → 用 `llm_fallback_api_key`（+ `llm_fallback_api_base`）；
        该 key 没配就**明确报错**（而不是静默回落到主密钥 —— 那正是要防的 401）。
      - 其它模型 → 用 `llm_api_key`。
    """
    fallback_key = settings.llm_fallback_api_key.get_secret_value()
    if settings.llm_fallback and model == settings.llm_fallback:
        if not fallback_key:
            raise PermanentLLMError(
                f"模型 {model!r} 是降级模型（llm_fallback），但没有配置 llm_fallback_api_key"
                "（LLM_FALLBACK_API_KEY）。降级模型必须用独立密钥：跨厂商复用主密钥必然 401，"
                "而降级正是主模型不可用的时候。"
            )
        return fallback_key, (settings.llm_fallback_api_base or None)
    return settings.llm_api_key.get_secret_value(), None


def _embedding_api_key(settings: Any) -> str:
    """embedding 的密钥：单独配了就用它，否则复用主模型密钥。

    允许复用是刻意的（同厂商时确实没必要多配一把），但**厂商不同就必须单独配** ——
    错误信息里会把这一点说清楚，避免只看到一句 401。
    """
    dedicated = settings.embedding_api_key.get_secret_value()
    return dedicated or settings.llm_api_key.get_secret_value()


# ── 本地 embedding 后端（EMBEDDING_BACKEND=local）────────────────────────
#
# 三件事必须做对，否则「本地免费」会变成线上事故：
#   ① `sentence_transformers` 只能在**函数内** import（可选依赖）：
#      顶层 import 会让没装 extras 的环境连 `import app.llm` 都失败 ——
#      那时连 API 后端都用不了了。
#   ② `encode()` 是同步 CPU 密集调用 → 必须 `asyncio.to_thread`，
#      否则会阻塞事件循环，症状是「并发一上来**所有**接口都变慢」。
#   ③ 模型加载很慢（几百 MB~2 GB），必须**懒加载 + 进程内单例**，绝不能每个请求加载一次。

_local_model: Any = None
_local_model_name: str | None = None
_local_model_lock = threading.Lock()  # 加载与 encode 都靠它串行（实例不是线程安全的）


def _missing_local_dependency_error() -> PermanentLLMError:
    """缺可选依赖时的报错：**必须能照着修**，不能是裸 ModuleNotFoundError。"""
    return PermanentLLMError(
        "本地 embedding 后端需要可选依赖 sentence-transformers（含 torch，约 2 GB）。"
        "请执行 `uv sync --extra local`；或把 EMBEDDING_BACKEND 切回 litellm 用 API 后端。"
        "详见 docs/runbook/5.llm-runbook.md §7.4。"
    )


def _load_local_model(model_name: str, *, device: str = "") -> Any:
    """懒加载 + 进程内单例。首次调用会下载权重（这是「第一个请求很慢」的原因）。"""
    global _local_model, _local_model_name

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # noqa: PERF203 - 可选依赖
        raise _missing_local_dependency_error() from exc

    with _local_model_lock:
        if _local_model is None or _local_model_name != model_name:
            logger.info("加载本地 embedding 模型：%s（device=%s）", model_name, device or "auto")
            _local_model = SentenceTransformer(model_name, device=device or None)
            _local_model_name = model_name
        return _local_model


async def _embed_local(items: list[str], model_name: str, settings: Any) -> list[list[float]]:
    """本地后端：把 encode 丢进线程，避免阻塞事件循环（runbook §7.3.2 的头号坑）。"""
    path = settings.embedding_local_path.strip()
    resolved = path or model_name  # 离线环境用本地目录覆盖 HF 仓库名

    # ⚠️ 前置检查是**确定性**的，必须放在第一次尝试之前 —— 它抛 PermanentLLMError，
    #    由 `_run_with_retry` 原样抛出（不会重试）。实测踩到过反例：缺少可选依赖时
    #    ImportError 被当成「未识别异常 → 不可恢复」，但**模型名写错**（HF 401/404）却被
    #    当成可恢复错误重试 3 次，每次真的去请求 HuggingFace，最后抛 RetryableLLMError ——
    #    把「配置错」伪装成「暂时不可用」，用户会去重排队列而不是改配置。
    _validate_local_backend(resolved, settings)

    def _run() -> list[list[float]]:
        model = _load_local_model(resolved, device=settings.embedding_device.strip())
        with _local_model_lock:  # encode 串行（并发调用会互相踩；吞吐靠多进程/GPU 解决）
            vectors = model.encode(items, normalize_embeddings=True, convert_to_numpy=True)
        # numpy 数组 → 纯 Python 列表：跨进程/JSON 边界更安全（numpy 是本地后端的传递依赖）
        return [[float(x) for x in row] for row in vectors]

    return await asyncio.to_thread(_run)


def _validate_local_backend(model_name: str, settings: Any) -> None:
    """本地后端的**前置检查**：把能确定性判断的配置错误在第一次尝试之前报出来。

    只做三件能确定性判断的事（不去猜配置）：
      ① 可选依赖是否装了；
      ② 模型名是否为空 / 首尾带空白；
      ③ 名字里是否混进了说明文字（`.env` 取值行抄了示例注释的典型症状：
         `EMBEDDING_MODEL=BAAI/bge-m3    # ...` → 下游拿着不存在的仓库名报 401/404）。
    """
    try:
        import sentence_transformers  # noqa: F401
    except ImportError as exc:
        raise _missing_local_dependency_error() from exc

    if not model_name.strip():
        raise PermanentLLMError(
            "本地 embedding 后端的模型名为空：请设置 EMBEDDING_MODEL（如 BAAI/bge-m3）"
            "或 EMBEDDING_LOCAL_PATH（本地模型目录）。"
        )
    if model_name != model_name.strip():
        raise PermanentLLMError(
            f"本地 embedding 模型名首尾带空白：{model_name!r} —— 检查 .env 的取值行"
            "（取值行只写「键=值」，说明文字要单独成行）。"
        )
    if "#" in model_name or "（" in model_name:
        raise PermanentLLMError(
            f"本地 embedding 模型名疑似混入说明文字：{model_name!r} ——"
            "检查 .env 的取值行只写「键=值」（注释单独成行）。"
        )


def _assert_embedding_dim(
    vectors: list[list[float]], *, expected: int, model: str, backend: str
) -> None:
    """维度守卫：同批维度必须一致；配了 `embedding_dim` 就强校验（runbook §7.6）。

    为什么值得拦：换模型最坏的情况不是「维度不一致」（写库会直接响），
    而是**维度恰好相同但向量空间不相容** —— 检索看起来正常、结果全是乱码，
    评测掉分时你会先去怀疑 prompt。API ↔ 本地 之间**必然**属于后者。
    """
    if not vectors:
        return
    dims = {len(row) for row in vectors}
    if len(dims) > 1:
        raise PermanentLLMError(
            f"embedding 同批维度不一致：{sorted(dims)}（model={model}）——"
            "通常意味着批量被截断或混了模型。"
        )
    actual = dims.pop()
    if expected and actual != expected:
        raise PermanentLLMError(
            f"embedding 维度不符：期望 {expected}，实际 {actual}（model={model}, backend={backend}）。"
            "换模型/换后端必须同步三处：① 改 EMBEDDING_DIM；"
            "② 改 10.rag.md 的 vector(n) 列并重建索引；③ 重灌全部向量。"
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
    # 凭据按**最终模型**解析（见 _resolve_credentials 的说明：入口拿一次会在降级时用错密钥）
    api_key, api_base = _resolve_credentials(used_model, settings)

    async def _call() -> LLMResponse:
        t0 = time.perf_counter()
        extra: dict[str, Any] = {"api_base": api_base} if api_base else {}
        # ── 第 11 步：LLM 埋点（**本步唯一的 LLM span 来源**）────────────────────
        # 落在这里而不是各个 Agent 里：本函数是全项目唯一允许打模型的地方（模块 docstring
        # 第 1 节的硬约束），所以在它里面埋点等于「不可能漏」——任何绕过去的调用
        # 本来就已经违反了那条约束，而那种绕行不该靠埋点来发现（有 grep 自查用例钉着）。
        #
        # ⚠️ 重试的每一次尝试**各自一个 span**：`_call` 是被 tenacity 反复调用的那个闭包，
        # 所以「重试了 3 次」在 trace 里就是 3 条 `llm.call`。
        # 若把 span 开在 `llm_call` 外面，重试会被折叠成一条 —— 而「这次请求到底打了几次模型」
        # 正是成本与延迟归因的关键（看不到重试次数，就看不出「退避把 P99 推高了」）。
        with llm_span(used_model, attributes={"backend": "litellm"}) as span:
            try:
                response = await litellm.acompletion(
                    model=used_model,
                    messages=messages,
                    api_key=api_key,  # 唯一密钥入口（按模型解析，见上）
                    timeout=timeout,
                    # ⚠️ 必须显式关掉 litellm 自带重试，否则一次业务调用会是「两层重试」
                    #    （4 × (1+N) 个请求），退避时间失控且会主动放大限流压力。
                    #    ⚠️ `num_retries` **不在** acompletion 的具名参数里（走 **kwargs 透传），
                    #    所以参数名写错不会报错、只会静默不生效 —— 唯一的哨兵是
                    #    tests/test_llm_client_retry.py 里的计数断言。
                    num_retries=0,
                    **extra,
                    **kwargs,
                )
            except BaseException as exc:  # noqa: BLE001 - 只为埋点，原样重抛
                span.record_exception(exc)
                raise
            latency_ms = int((time.perf_counter() - t0) * 1000)
            result = _to_response(response, latency_ms)
            # span 属性只放**白名单**里的量（prompt 全文绝不进 trace，见 tracing.ALLOWED_ATTRIBUTES）
            span.set_attributes(
                {
                    "model": result.model or used_model,
                    "tokens": result.total_tokens,
                    "cost_usd": result.cost_usd,
                    "attempts": 1,
                }
            )
            span.set_ok()
        # 指标在 span 之外记（`trace_llm` 的 `with` 只保证 span 会结束）：
        # token 与成本是 counter（累加量），与 span 的「一条记录」是两回事。
        record_llm_call(
            model=result.model or used_model,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            cost_usd=result.cost_usd,
            finish_reason=result.finish_reason,
        )
        return result

    return await _run_with_retry(_call, max_retries=max_retries)


async def embed(
    texts: Sequence[str],
    model: str | None = None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[list[float]]:
    """把一批文本向量化；**顺序与入参严格一致**（调用方依赖这一点做对齐）。

    两种后端，**同一个签名**（调用方不感知差异，配置项是 `embedding_backend`）：
      - `"litellm"`（默认）：`litellm.aembedding` —— 按 token 计费、文本出网、可能 429/超时
      - `"local"`：本地 sentence-transformers —— 零边际成本、文本不出网、吃本机 CPU/内存
        （需要可选依赖 `uv sync --extra local`；首次调用会下模型权重，见 runbook §7.5）

    ⚠️ **保序**：litellm 响应里每个 data 项带 `index`，这里**按 index 排序**后返回，
    而不是假定返回顺序（批量并发时顺序不保证 —— 这是最隐蔽的一类 bug，
    症状是「检索偶尔答非所问」）。同时校验条数：数量对不上就抛错，
    绝不返回一个「长度不对但顺序看着对」的列表。

    ⚠️ **维度守卫**（`embedding_dim > 0` 时生效）：换模型/换后端会让维度或向量空间变化，
    最坏情况是「维度恰好相同但空间不相容」——检索看着正常、结果全是乱的。所以不符就报错。

    抛出：同 `llm_call`（`PermanentLLMError` / `RetryableLLMError`）。
    """
    items = [str(text) for text in texts]
    if not items:
        # 空输入不该发请求：既省一次计费，也避免某些 provider 对空批返回怪异结构
        return []

    settings = get_settings()
    used_model = model or settings.embedding_model
    backend = settings.embedding_backend

    if backend == "local":
        # 本地后端：显式 timeout 无意义（不涉及网络），保留参数只为签名一致
        call = lambda: _embed_local(items, used_model, settings)  # noqa: E731
    else:

        async def call() -> list[list[float]]:
            response = await litellm.aembedding(
                model=used_model,
                input=items,
                api_key=_embedding_api_key(settings),  # 单独的一把，或复用主模型密钥
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
            return [list(item["embedding"]) for item in sorted(data, key=lambda i: i["index"])]

    vectors = await _run_with_retry(call, max_retries=max_retries)
    _assert_embedding_dim(vectors, expected=settings.embedding_dim, model=used_model, backend=backend)
    # ── 第 11 步：embedding 埋点────────────────────────────────────────────────
    # 为什么 embedding 也要埋点（文档 §4 第 1 条把「向量检索」列进了必埋项）：
    # 一次灌库会打上千次 embedding，而它是**按 token 计费**的 —— 成本看板上
    # 「embedding 花了多少」与「生成花了多少」必须是两条线，否则一次全量重灌
    # 会把账算到生成调用头上（第 10 步的「只重嵌 1 个片段」正是靠这个对比才成立）。
    #
    # token 口径用 `approx_tokens`（本地后端没有真实 usage）：
    # 这是一个**近似值**，所以它记在 span 属性里，而**不进** `LLM_TOKENS` counter ——
    # 把估算值混进计费口径的 counter 会让那个数字永久失真（而它看起来仍然精确）。
    from app.rag.textkit import approx_tokens

    approx = sum(approx_tokens(text) for text in items)
    with embed_span(used_model, backend=backend, input_tokens=approx, rows=len(vectors)) as span:
        span.set_ok()
    return vectors


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "MAX_BACKOFF_SECONDS",
    "LLMResponse",
    "embed",
    "llm_call",
]