"""**真实厂商探活**（第 5 步的最终验收）：会真的发网络请求、真的花钱/消耗额度。

⚠️ 这些用例**刻意不默认跑**（CI 里没有 key，也不该为每次 push 花钱）：
  - 用 `-m "not llm_live"` 或只跑 `tests/test_llm_*.py` 里的离线三件套；
  - 需要真实验收时：在 `.env` 填好真实的 `LLM_API_KEY`（`.env.example` 里默认是占位符
    `sk-your-key`），然后 `uv run pytest -q tests/test_llm_live.py -m llm_live -v`。

为什么值得单独开一个文件：离线 mock 证明的是「代码路径通」，**不能**证明
「key + 模型名真的能用」—— `Settings` 只做形状校验，不替 litellm 维护厂商模型表，
所以模型名/key 写错要到第一次真实调用才暴露（见 docs/runbook/5.llm-runbook.md §1.2）。
"""

from importlib.metadata import version

import pytest

from app.config import get_settings
from app.llm import LLMResponse, embed, llm_call

PLACEHOLDER_KEYS = {"", "sk-your-key", "sk-fake", "sk-fake-for-test", "sk-test"}

pytestmark = pytest.mark.llm_live


def _live_blockers() -> list[str]:
    """返回「跑不了真实探活」的原因列表；空列表 = 可以跑。"""
    settings = get_settings()
    reasons: list[str] = []
    key = settings.llm_api_key.get_secret_value().strip()
    if key.lower() in PLACEHOLDER_KEYS:
        reasons.append(
            "LLM_API_KEY 仍是占位符（未配置真实密钥）—— 在仓库根 .env 填入真实 key 后重跑"
        )
    if not settings.llm_primary.strip():
        reasons.append("LLM_PRIMARY 为空，无法确定要调哪个模型")
    return reasons


@pytest.fixture(scope="module")
def live_env():
    """真实探活的前置检查 + 把生效配置（已掩码）打出来当证据。"""
    reasons = _live_blockers()
    if reasons:
        pytest.skip("真实 LLM 探活不可用：" + "；".join(reasons))
    settings = get_settings()
    masked = settings.dump_for_logging()
    print(
        f"\n[live] litellm={version('litellm')} primary={masked['llm_primary']} "
        f"fallback={masked['llm_fallback']!r} embedding={masked.get('embedding_model')!r} "
        f"key={masked['llm_api_key']}"
    )
    return settings


def test_live_chat_completion(live_env):
    """单模型调通（5.llm.md 第 6 节第 2 条）：返回内容 + 结构化的 token/cost/latency。"""
    import asyncio

    result: LLMResponse = asyncio.run(
        llm_call(
            messages=[{"role": "user", "content": "只回复两个字：在的"}],
            max_tokens=16,
        )
    )

    print(
        f"\n[live] content={result.content!r} model={result.model} "
        f"tokens={result.prompt_tokens}/{result.completion_tokens}/{result.total_tokens} "
        f"cost_usd={result.cost_usd} latency_ms={result.latency_ms}"
    )

    assert result.content.strip(), "真实调用应返回非空内容"
    assert result.model, "应回传实际使用的模型名"
    assert result.total_tokens > 0, "应带回 token 用量（第 11/13 步计费的输入）"
    assert result.latency_ms > 0


def test_live_embedding(live_env):
    """embedding 门面可用（供第 10 步 rag / 第 13 步 cache）。

    ⚠️ 前提是**厂商/key 支持 embedding 端点**：embedding_model 与 llm_primary 常常不是
    同一个厂商（见 docs/runbook/5.llm-runbook.md §7.4）。不支持时这条会失败，
    那时先做 embedding 选型，而不是改这条断言。
    """
    import asyncio

    vectors = asyncio.run(embed(["退货政策是什么", "怎么改地址"]))

    assert len(vectors) == 2, "应返回与入参等量的向量"
    assert len(vectors[0]) == len(vectors[1]), "同一批向量维度应一致"
    assert vectors[0] != vectors[1], "不同文本不应得到相同向量（否则顺序对齐测试没有意义）"
    print(f"\n[live] embedding dim={len(vectors[0])}")
