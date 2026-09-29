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
# 「像占位符」的片段：`.env.example` 抄过来忘了换、或临时填了个假值时，应当**跳过**而不是真打网络。
# 实测踩到过：把 LLM_API_KEY 填成 `sk-from_env` 之类的假值后，本文件不再 skip，
# 而是真的去请求 api.deepseek.com 并拿到 401 —— 既污染了验收信号，也白跑网络。
_PLACEHOLDER_HINTS = ("your-key", "your_key", "yourkey", "from_env", "fake", "placeholder", "changeme", "xxx")

pytestmark = pytest.mark.llm_live


@pytest.mark.parametrize(
    "key",
    ["", "   ", "sk-your-key", "sk-from_env", "sk-FAKE-for-test", "placeholder", "changeme", "sk-xxxx"],
)
def test_placeholder_detector_skips_obviously_fake_keys(monkeypatch, key):
    """守卫：**看起来就是假钥匙**时，真实探活必须 skip，而不是真的发请求。

    实测踩到过：`.env` 里填了 `sk-from_env` 这类假值，探测器没认出来 → 用例真的请求了
    `api.deepseek.com` 并拿到 401。后果有两个：① 「验收通过/失败」的信号被污染
    （它测的不再是「key 能用」，而是「本机网络能不能到厂商」）；② 白跑一次真实网络与配额。

    ⚠️ 这条用例本身**不发请求**：它只检查「判定函数」，所以能在离线环境跑（属于 llm_live 标记，
    用 `-m "not llm_live"` 排除即可）。
    """
    from app.config import get_settings

    monkeypatch.setenv("LLM_PRIMARY", "deepseek/deepseek-flash")
    monkeypatch.setenv("LLM_API_KEY", key)
    get_settings.cache_clear()
    try:
        reasons = _live_blockers()
    finally:
        get_settings.cache_clear()

    assert reasons, f"key={key!r} 应被判为占位符并 skip"
    assert "占位符" in reasons[0]


@pytest.mark.parametrize("key", ["sk-0123456789abcdef0123456789abcdef", "sk-proj-AbCdEf123456"])
def test_placeholder_detector_allows_real_looking_keys(monkeypatch, key):
    """反向守卫：**像真钥匙**的值必须放行（否则真实探活会永远 skip，验收变成空转）。"""
    from app.config import get_settings

    monkeypatch.setenv("LLM_PRIMARY", "deepseek/deepseek-flash")
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setenv("LLM_FALLBACK", "")
    get_settings.cache_clear()
    try:
        reasons = _live_blockers()
    finally:
        get_settings.cache_clear()

    assert reasons == [], f"key={key} 不该被判为占位符，实际原因：{reasons}"


def _looks_like_placeholder(key: str) -> bool:
    """判断 `LLM_API_KEY` 是否明显不是真钥匙（占位符/测试值）。"""
    lowered = key.strip().lower()
    if lowered in PLACEHOLDER_KEYS:
        return True
    return any(hint in lowered for hint in _PLACEHOLDER_HINTS)


def _live_blockers() -> list[str]:
    """返回「跑不了真实探活」的原因列表；空列表 = 可以跑。"""
    settings = get_settings()
    reasons: list[str] = []
    key = settings.llm_api_key.get_secret_value().strip()
    if _looks_like_placeholder(key):
        reasons.append(
            "LLM_API_KEY 看起来还是占位符/测试值（未配置真实密钥）—— "
            "在仓库根 .env 填入真实 key 后重跑；若出现 401，说明这把 key 无效，同样需要换掉"
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
    """单模型调通（5.llm.md 第 6 节第 2 条）：返回内容 + 结构化的 token/cost/latency。

    ⚠️ **`max_tokens` 不能给小**：`deepseek-flash` 这类**推理模型**会先把 token 花在
    `reasoning_content` 上（实测：`max_tokens=16` 时 16 个 token 全被推理吃掉，
    `content` 返回空串、`finish_reason=length`，而调用本身是「成功」的）。
    所以这里给足额度，并把「被截断」与「模型真没说话」分开断言 —— 否则排查会绕一大圈。
    """
    import asyncio

    result: LLMResponse = asyncio.run(
        llm_call(
            messages=[{"role": "user", "content": "只回复两个字：在的"}],
            max_tokens=256,   # 推理模型要先"想"，给小了会得到空 content（见 docstring）
        )
    )

    print(
        f"\n[live] content={result.content!r} model={result.model} finish_reason={result.finish_reason!r} "
        f"tokens={result.prompt_tokens}/{result.completion_tokens}/{result.total_tokens} "
        f"cost_usd={result.cost_usd} latency_ms={result.latency_ms}"
    )

    assert result.finish_reason != "length", (
        "被 max_tokens 截断了（finish_reason=length）：推理模型会先消耗 reasoning token，"
        f"请调大 max_tokens。content={result.content!r} completion_tokens={result.completion_tokens}"
    )
    assert result.content.strip(), f"真实调用应返回非空内容（finish_reason={result.finish_reason!r}）"
    assert result.model, "应回传实际使用的模型名"
    assert result.total_tokens > 0, "应带回 token 用量（第 11/13 步计费的输入）"
    assert result.latency_ms > 0


def test_live_embedding(live_env):
    """embedding 门面可用（供第 10 步 rag / 第 13 步 cache）。

    ⚠️ 这条**跑得通与否取决于 embedding 的选型**，失败信息会明确指出是哪一类问题：
      - `EMBEDDING_BACKEND=local`：需要 `uv sync --extra local`，且首次要能从 HuggingFace
        下到权重（离线/无凭证环境会 401/404）；
      - `EMBEDDING_BACKEND=litellm`：需要有支持 embedding 端点的厂商与 key
        （主模型厂商不一定提供，见 docs/runbook/5.llm-runbook.md §7.4）。
    无论哪种，**都不该靠改这条断言让它变绿**。
    """
    import asyncio

    from app.config import get_settings

    settings = get_settings()
    vectors = asyncio.run(embed(["退货政策是什么", "怎么改地址"]))

    assert len(vectors) == 2, "应返回与入参等量的向量"
    assert len(vectors[0]) == len(vectors[1]), "同一批向量维度应一致"
    assert vectors[0] != vectors[1], "不同文本不应得到相同向量（否则顺序对齐测试没有意义）"
    print(
        f"\n[live] embedding backend={settings.embedding_backend!r} model={settings.embedding_model!r} "
        f"dim={len(vectors[0])}（记得把它填进 EMBEDDING_DIM）"
    )
