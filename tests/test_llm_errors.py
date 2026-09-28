"""LLM 错误分类的纯逻辑测试（不联网、不需要真 key）。

对应 `docs/modules/5.llm.md` 第 7 节的：
  - `test_fallback_on_timeout` 的**判据基础**（只有可恢复错误才允许降级/重试）
  - `test_no_fallback_on_4xx_param_error`（参数错误不降级、不无限重试）

这一层是整套重试逻辑的地基：`app/llm/client.py` 的 `retry_if_exception(is_retryable)`
直接吃这里的判定结果，所以分类错了，重试行为一定错。
"""

import asyncio

import httpx
import litellm
import openai
import pytest

from app.llm import errors
from app.llm.errors import (
    LLMError,
    PermanentLLMError,
    RetryableLLMError,
    is_permanent,
    is_retryable,
)


def _fake_http_response(status_code: int) -> httpx.Response:
    """造一个 httpx.Response（litellm 的 403/4xx 异常要求必填 response 参数）。

    用 `httpx.Response(..., request=...)` 构造：litellm 会读 `.status_code` 与 `.text`。
    """
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    return httpx.Response(status_code=status_code, request=request, json={"error": "forbidden"})


def _rate_limit() -> litellm.exceptions.RateLimitError:
    return litellm.exceptions.RateLimitError(
        message="rate limited", llm_provider="openai", model="gpt-4o-mini"
    )


def _bad_request() -> litellm.exceptions.BadRequestError:
    return litellm.exceptions.BadRequestError(
        message="invalid parameter", llm_provider="openai", model="gpt-4o-mini"
    )


# ── 可恢复：必须重试 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc_factory",
    [
        pytest.param(_rate_limit, id="429-rate-limit"),
        pytest.param(
            lambda: litellm.exceptions.Timeout(
                message="timeout", llm_provider="openai", model="gpt-4o-mini"
            ),
            id="litellm-timeout",
        ),
        pytest.param(
            lambda: litellm.exceptions.APIConnectionError(
                message="conn reset", llm_provider="openai", model="gpt-4o-mini"
            ),
            id="api-connection-error",
        ),
        pytest.param(
            lambda: litellm.exceptions.InternalServerError(
                message="500", llm_provider="openai", model="gpt-4o-mini"
            ),
            id="500-internal",
        ),
        pytest.param(
            lambda: litellm.exceptions.ServiceUnavailableError(
                message="503", llm_provider="openai", model="gpt-4o-mini"
            ),
            id="503-unavailable",
        ),
        pytest.param(lambda: asyncio.TimeoutError("asyncio timeout"), id="asyncio-timeout"),
    ],
)
def test_retryable_errors_are_recognized(exc_factory):
    """限流/超时/连接失败/5xx → 判定为可恢复（否则 429 不会退避，A1 直接失守）。"""
    exc = exc_factory()
    assert is_retryable(exc) is True
    assert is_permanent(exc) is False


# ── 不可恢复：绝不能重试 ──────────────────────────────────────────


@pytest.mark.parametrize(
    "exc_factory",
    [
        pytest.param(_bad_request, id="400-bad-request"),
        pytest.param(
            lambda: litellm.exceptions.AuthenticationError(
                message="invalid key", llm_provider="openai", model="gpt-4o-mini"
            ),
            id="401-auth",
        ),
        pytest.param(
            lambda: litellm.exceptions.PermissionDeniedError(
                message="forbidden",
                llm_provider="openai",
                model="gpt-4o-mini",
                # ⚠️ 实测 litellm 1.103.0：PermissionDeniedError 的 response 是**必填**
                #    （httpx.Response），不传会 TypeError —— 构造假异常时最容易踩的一处。
                response=_fake_http_response(403),
            ),
            id="403-permission",
        ),
        pytest.param(
            lambda: litellm.exceptions.NotFoundError(
                message="model not found", llm_provider="openai", model="gpt-4o-mini"
            ),
            id="404-not-found",
        ),
        pytest.param(
            lambda: litellm.exceptions.ContentPolicyViolationError(
                message="policy", llm_provider="openai", model="gpt-4o-mini"
            ),
            id="content-policy",
        ),
        pytest.param(
            lambda: litellm.exceptions.ContextWindowExceededError(
                message="too long", llm_provider="openai", model="gpt-4o-mini"
            ),
            id="context-window",
        ),
    ],
)
def test_permanent_errors_are_not_retried(exc_factory):
    """参数错/鉴权/模型名错/内容违规/上下文超长 → 不可恢复（重试只会重复犯错）。"""
    exc = exc_factory()
    assert is_retryable(exc) is False
    assert is_permanent(exc) is True


def test_unknown_error_is_conservatively_not_retryable():
    """**没见过的异常**（含非 litellm 的 bug 类异常）一律不重试。

    这是「白名单」设计的核心：漏判一个可恢复错误只损失一次重试机会，
    误判一个不可恢复错误则是「拿真实流量反复撞墙」。
    """
    assert is_retryable(KeyError("boom")) is False
    assert is_retryable(TypeError("boom")) is False
    assert is_retryable(RuntimeError("boom")) is False
    assert is_permanent(KeyError("boom")) is False  # 「不可恢复」= 显式登记过的，未知错误不算


def test_both_tables_only_contain_real_litellm_error_classes():
    """守卫：两张表里的类必须都真实存在、都是 Exception 子类。

    这条防的是「表被写错」—— 拼错类名会在 import 期就炸，但把某个**非异常类**
    （例如某个 TypedDict 或字符串常量）塞进表里不会立刻报错，
    而是在运行期让 `isinstance(exc, ...)` 抛 `TypeError`，那时故障现场已经远离这里了。
    """
    for cls in errors.RETRYABLE + errors.NON_RETRYABLE:
        assert isinstance(cls, type), f"{cls!r} 不是类"
        assert issubclass(cls, Exception), f"{cls!r} 不是 Exception 子类"


def test_core_provider_errors_are_all_classified():
    """litellm 的**核心**错误必须落到两张表之一（漏一个就是「静默不重试/静默重试」）。

    注意：litellm 的异常类有 30 个（实测 1.103.0），本步只对**调用路径上真会遇到**的这 11 个负责。
    其余（如 `APIResponseValidationError`、`MockException`）落到「未识别 → 保守不重试」，
    这是刻意留的余地：每次 litellm 升级都去补全表，属于替上游维护清单。
    """
    core = [
        litellm.exceptions.RateLimitError,
        litellm.exceptions.Timeout,
        litellm.exceptions.APIConnectionError,
        litellm.exceptions.InternalServerError,
        litellm.exceptions.ServiceUnavailableError,
        litellm.exceptions.BadRequestError,
        litellm.exceptions.AuthenticationError,
        litellm.exceptions.PermissionDeniedError,
        litellm.exceptions.NotFoundError,
        litellm.exceptions.ContentPolicyViolationError,
        litellm.exceptions.ContextWindowExceededError,
    ]
    classified = set(errors.RETRYABLE) | set(errors.NON_RETRYABLE)
    missing = [c.__name__ for c in core if c not in classified]
    assert not missing, f"以下核心异常没有分类：{missing}"


def test_tables_disjoint_and_unknown_base_is_not_retried():
    """两张表不得有交集；且**没登记的**上游基类一律不重试。

    `litellm.exceptions.APIError` 是 litellm 错误族的基类，它既不在 RETRYABLE 也不在
    NON_RETRYABLE —— 这正是「白名单」的体现：没登记 = 不重试。
    """
    assert not (set(errors.RETRYABLE) & set(errors.NON_RETRYABLE)), "两张表不应有交集"

    generic = litellm.exceptions.APIError(
        status_code=500, message="generic", llm_provider="openai", model="m"
    )
    assert is_retryable(generic) is False, "未登记的通用基类不该被重试"
    assert is_permanent(generic) is False, "它不是「显式拒绝」，只是「未识别」"

    subtype = litellm.exceptions.APIConnectionError(
        message="conn", llm_provider="openai", model="m"
    )
    assert is_retryable(subtype) is True, "已登记的可恢复子类必须能重试"


def test_raw_openai_transport_errors_are_retryable():
    """**裸 openai 异常**也必须是可恢复的（这是实测踩出来的一个真缺口）。

    实测（litellm 1.103.0）：`litellm.exceptions.Timeout` 的 MRO 走的是
    `litellm.exceptions.Timeout → openai.APITimeoutError → openai.APIConnectionError`，
    而 `litellm.exceptions.APIConnectionError` 是**另一个类**：
        issubclass(litellm.exceptions.Timeout, litellm.exceptions.APIConnectionError) → False
    所以只登记 litellm 那一族时，一个从 SDK 直接冒出来的连接/超时错误会被判成「永久错误」、
    一次都不重试 —— 传输层的瞬时抖动被当成配置错误，是最难查的一类误判。
    这条用例把 openai 族也钉在表里，防止有人「整理表」时把它删掉。
    """
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")

    assert is_retryable(openai.APIConnectionError(request=request)) is True
    assert is_retryable(openai.APITimeoutError(request=request)) is True
    # 裸 openai 的 4xx 仍然不可恢复（与 litellm 族语义一致）
    assert is_retryable(openai.AuthenticationError(
        "bad key", response=httpx.Response(401, request=request), body=None
    )) is False


def test_non_retryable_is_checked_first(monkeypatch):
    """判定顺序：NON_RETRYABLE **先于** RETRYABLE。

    当前两张表无交集，所以这条顺序暂时观察不到差别；但它防的是「将来表被写成互相覆盖」
    （例如某个泛化类同时进了两族）——那时若先查 RETRYABLE，一个显式拒绝的错误会被误重试。
    这里临时把 RateLimitError 塞进拒绝族来构造该场景。
    """
    assert is_retryable(_rate_limit()) is True  # 前提：正常情况可重试

    monkeypatch.setattr(
        errors, "NON_RETRYABLE", (*errors.NON_RETRYABLE, litellm.exceptions.RateLimitError)
    )
    assert is_retryable(_rate_limit()) is False, "显式拒绝族应当优先于可恢复族"


# ── 分类表本身的自检（防止表被清空/写错类） ────────────────────────


def test_classification_tables_are_non_empty():
    """守卫：两张表都不能为空，否则「重试」或「拒绝」语义直接失效。"""
    assert errors.RETRYABLE, "RETRYABLE 不能为空，否则任何错误都不重试"
    assert errors.NON_RETRYABLE, "NON_RETRYABLE 不能为空"


def test_timeout_family_requires_two_distinct_classes():
    """超时族要**同时**登记 litellm 的 Timeout 与内建 TimeoutError；且继承关系与直觉不同。

    实测（Python 3.12.9 / litellm 1.103.0 / openai SDK）：
      - `asyncio.TimeoutError is TimeoutError` → **True**（同一个对象，不是两个类）；
      - `litellm.exceptions.Timeout` 是内建 TimeoutError 之外的另一棵树的类；
      - `litellm.exceptions.Timeout` 继承 **openai.APITimeoutError**，
        而 `litellm.exceptions.APIConnectionError` 是**另一个类**（两者无继承关系）。

    后面这条正是 §errors.RETRYABLE 里必须同时列 litellm 族与 openai 族的原因。
    """
    assert asyncio.TimeoutError is TimeoutError, "3.12 下两者同一对象（断言写法要跟着这个事实）"
    assert litellm.exceptions.Timeout is not TimeoutError
    assert issubclass(litellm.exceptions.Timeout, openai.APITimeoutError)
    assert not issubclass(litellm.exceptions.Timeout, litellm.exceptions.APIConnectionError)

    lite = litellm.exceptions.Timeout(message="t", llm_provider="openai", model="m")
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    assert is_retryable(lite) is True
    assert is_retryable(TimeoutError("t")) is True
    assert is_retryable(openai.APITimeoutError(request=request)) is True


def test_real_litellm_embedding_item_supports_dict_access():
    """守卫：`client.embed()` 用 `item["index"]` / `item["embedding"]` 取值，真实对象必须支持。

    litellm 返回的 `EmbeddingResponse.data` 是一组 **pydantic `Embedding`** 对象；它们实现了
    `__getitem__`（`return getattr(self, k)`），所以按字典取值是可行的 —— 但这是**上游实现细节**，
    一旦 litellm 改成不支持下标访问，`embed()` 会在运行期抛 `TypeError`。
    这里把这个假设显式钉住：它一破，测试先红，而不是等第 10 步 rag 上线才发现。
    """
    import litellm.types.utils as litellm_types

    item = litellm.embedding(
        model="text-embedding-3-small", input=["seed"], mock_response=["seed"], api_key="sk-fake"
    ).data[0]
    assert isinstance(item, litellm_types.Embedding)
    # ⚠️ `litellm.Embedding` 在**运行期**取不到（模块级 `__getattr__` 不导出它，实测 AttributeError）；
    #    正确的引用位置是 `litellm.types.utils.Embedding`（`litellm.utils.Embedding` 是同一个对象）。
    assert item["index"] == 0
    assert item["embedding"] == ["seed"]
    assert set(type(item).model_fields) >= {"index", "embedding", "object"}


def test_public_error_types_share_one_base():
    """对外只需 catch 一个 `LLMError`，且两种语义必须可区分（第 12 步队列按它分流）。"""
    assert issubclass(PermanentLLMError, LLMError)
    assert issubclass(RetryableLLMError, LLMError)
    assert issubclass(LLMError, RuntimeError)
    assert not issubclass(PermanentLLMError, RetryableLLMError)
    assert not issubclass(RetryableLLMError, PermanentLLMError)
