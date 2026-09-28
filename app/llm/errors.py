"""LLM 错误分类：决定「重试」还是「立刻抛」（第 13 步还会用它决定「是否降级」）。

单独成模块的理由：`5.llm.md` 第 5 节把「只有限流/超时/5xx 这类可恢复错误才降级」列为关键技术点，
分类逻辑必须能被**单独单测**（`tests/test_llm_errors.py` 是纯逻辑、不起网络）。
第 13 步的熔断器与 fallback 链会复用同一个分类函数 —— 分类一旦有两份，熔断与降级就会打架。

判定方向是**白名单**：只有明确列在 `RETRYABLE` 里的才算可恢复。
理由见 `is_retryable()` 的 docstring。
"""

import asyncio

import litellm
import openai

# 可恢复：限流 / 超时 / 连接失败 / 5xx —— 等一会儿或换个模型真的能好。
#
# ⚠️ 为什么 litellm 与 openai 两族的类都要列（本仓实测，litellm 1.103.0）：
#   `litellm.exceptions.Timeout` 的 MRO 是
#       litellm.exceptions.Timeout → openai.APITimeoutError → **openai.APIConnectionError** → …
#   也就是说它继承的是 **openai 的** APIConnectionError，**不是** litellm 自己的同名类
#   （`issubclass(litellm.exceptions.Timeout, litellm.exceptions.APIConnectionError)` 实测为 **False**，
#    两者是两个不同的类对象）。所以只列 litellm 那一族会漏掉「裸 openai 异常」——
#   litellm 会把 SDK 异常包成自己的类型，但 catch 面留一条底裤更稳：真漏出来时，
#   一个瞬时连接故障会被误判成永久错误、一次都不重试。
RETRYABLE: tuple[type[BaseException], ...] = (
    litellm.exceptions.RateLimitError,
    litellm.exceptions.Timeout,
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.InternalServerError,
    litellm.exceptions.ServiceUnavailableError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    asyncio.TimeoutError,
)

# 不可恢复：参数错 / 鉴权 / 模型名错 / 内容违规 —— 重试与降级都无意义。
#
# 每一条都对应一类「多做一次只会更糟」的错误：
#   BadRequestError           参数写错，重试 4 次就是把同一个错误犯 4 次；
#   AuthenticationError        key 错，重试 100 次还是错，且每个副本都在打鉴权接口；
#   PermissionDeniedError     同上（权限/配额层面）；
#   NotFoundError             模型名/端点写错，属配置错误，必须立刻炸出来；
#   ContentPolicyViolationError 内容违规，换模型是「绕过风控」，安全红线（同 4.security.md）；
#   ContextWindowExceededError 上下文超长，重试不会变短（第 13 步若要换长上下文模型，需显式决策）。
NON_RETRYABLE: tuple[type[BaseException], ...] = (
    litellm.exceptions.BadRequestError,
    litellm.exceptions.AuthenticationError,
    litellm.exceptions.PermissionDeniedError,
    litellm.exceptions.NotFoundError,
    litellm.exceptions.ContentPolicyViolationError,
    litellm.exceptions.ContextWindowExceededError,
)


class LLMError(RuntimeError):
    """本模块对外统一的错误类型（调用方只需要 catch 这一个）。"""


class RetryableLLMError(LLMError):
    """可恢复错误：重试若干次后仍失败才抛这个。

    调用方语义：**「暂时不可用」**，可以放进队列带延迟重排（第 12 步），可以对用户说「稍后再试」。
    """


class PermanentLLMError(LLMError):
    """不可恢复错误：**第一次就抛**，不做任何重试。

    调用方语义：**「别再试了」**，直接报错/人工兜底，重排队列只会浪费资源。
    """


def is_retryable(exc: BaseException) -> bool:
    """保守判定：只有明确列在 RETRYABLE 里的才算可恢复。

    ⚠️ 返回 False 的两种情形要分清：
      ① 明确列在 NON_RETRYABLE 里（400/401/403/404/上下文超长）；
      ② **没见过的异常**（如 KeyError/TypeError，或 litellm 新增的异常类）—— 同样不重试。

    用「白名单」而不是「黑名单」，是因为两种误判的代价不对称：
    漏判一个可恢复错误只损失一次重试机会；误判一个不可恢复错误会变成「拿真实流量反复撞墙」，
    在 429 场景下还会主动放大限流压力。所以宁可保守。

    注意：判断顺序是「先排 NON_RETRYABLE，再看 RETRYABLE」——因为 litellm 的异常树里有
    同族继承（如 Timeout 继承 APIConnectionError），先查显式拒绝清单可以避免将来某个
    可恢复异常的父类被加进 RETRYABLE 后「顺带放行」了不该重试的子类。
    """
    if isinstance(exc, NON_RETRYABLE):
        return False
    return isinstance(exc, RETRYABLE)


def is_permanent(exc: BaseException) -> bool:
    """是否为「不可恢复」错误（显式列在 NON_RETRYABLE 里的）。"""
    return isinstance(exc, NON_RETRYABLE)


__all__ = [
    "LLMError",
    "NON_RETRYABLE",
    "PermanentLLMError",
    "RETRYABLE",
    "RetryableLLMError",
    "is_permanent",
    "is_retryable",
]
