"""LLM 接入层对外出口。

别人的 import **只许**写 `from app.llm import llm_call, embed`。
不要从 `app.llm.client` 里 import —— 那样将来拆分/替换实现时所有调用方都要跟着改；
门面的意义就是「只有本模块是契约」。

见 `docs/runbook/5.llm-runbook.md`（第 5 步基础接入的实操手册）。
"""

from app.llm.client import LLMResponse, embed, llm_call
from app.llm.errors import LLMError, PermanentLLMError, RetryableLLMError

__all__ = [
    "LLMError",
    "LLMResponse",
    "PermanentLLMError",
    "RetryableLLMError",
    "embed",
    "llm_call",
]
