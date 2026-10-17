"""`rag.settings` 的测试：配置读取、默认值、以及对账与夹取（`RagSettings.from_settings`）。

为什么这层值得单独测：它是「配置面 → 检索参数」的唯一翻译层。
它出错的表现全是**静默**的：候选池变成 30 而不是配置的 5（慢而不报错）、
`overlap >= max_chars` 让分块循环不前进（挂死）、维度不一致让向量空间不相容（结果全乱）。
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.rag.settings import DEFAULT_RERANK_MODEL, RagSettings


def test_from_settings_reads_the_repo_env():
    """`RagSettings.from_settings()` 必须真的从本仓配置取值（而不是用类默认值兜底）。

    ⚠️ 这条用例**刻意不钉具体取值**（原先写的是 `rerank == "local"`、`bge-m3`）。
    钉死取值的代价：改 `.env` 里合法的配置（比如把 rerank 关掉、把 embedding 换成
    API 后端）都会让这条用例红，而它想守的其实只是「读到了仓库配置」这件事。
    2026-10 换 embedding 模型时正是这个原因红了一次 —— 而当时的改动是正确的。

    现在守的是**性质**：
      · 维度是 1024 且与迁移里的 `vector(n)` 一致（这是迁移锁定的值，见 app/rag/schema.py）；
      · rerank 是三种合法模式之一，且 model 非空；
      · candidates 与 max_top_k 的关系成立（初召回池必须 ≥ 最终 top-k）。
    """
    settings = RagSettings.from_settings()

    # 维度：本仓迁移把 vector(n) 写死成 1024，配置必须跟上
    # （注意 `RagSettings()` 的类默认 vector_dim 是 0 = 不校验，所以不能拿它做基准）
    assert settings.vector_dim == 1024

    # rerank 必须是三种合法模式之一（off/local/api），且 model 非空
    assert settings.rerank in ("off", "local", "api")
    assert settings.rerank_model.strip(), "rerank_model 不能为空"

    # 初召回池必须容得下最终 top-k，否则 rerank 无料可选
    assert settings.candidates >= settings.max_top_k

    # 与迁移对账（漂移会静默错，所以在这里也钉一次）
    from app.rag.schema import VECTOR_DIM as MIGRATION_DIM

    assert settings.vector_dim == MIGRATION_DIM


def test_from_settings_falls_back_when_keys_are_absent(monkeypatch):
    """`.env` 里还没有 RAG_* 的旧环境也要能用（首次落地时的真实场景）。"""
    for name in (
        "RAG_VECTOR_DIM",
        "RAG_CANDIDATES",
        "RAG_RRF_K",
        "RAG_KEYWORD_MIN_RATIO",
        "RAG_RERANK",
        "RAG_MAX_TOP_K",
        "RAG_CHUNK_MAX_CHARS",
        "RAG_CHUNK_OVERLAP",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    class Bare:
        """只有第 5 步字段的最小对象（模拟「RAG_* 还没加」的配置）。"""

        embedding_dim = 0
        embedding_model = "openai/text-embedding-3-small"

    settings = RagSettings.from_settings(Bare())
    assert settings.vector_dim == 0
    assert settings.candidates == 30
    assert settings.rrf_k == 60
    assert settings.keyword_min_ratio == pytest.approx(0.6)
    assert settings.chunk_max_chars == 600
    assert settings.chunk_overlap == 80
    assert settings.rerank == "local"
    assert settings.rerank_model == DEFAULT_RERANK_MODEL
    get_settings.cache_clear()


def test_vector_dim_mismatch_warns_but_does_not_raise(caplog):
    """`RAG_VECTOR_DIM` 与 `EMBEDDING_DIM` 不一致时告警（而不是拒绝启动）。

    为什么不抛：抛了会让「只想改一处」的人整条链路不可用；
    真正需要 fail-fast 的是「向量维度 vs 建表维度」，那一条由数据库的类型检查兜住
    （`vector(n)` 不接受错维度），而 `StoreError` 会把三处该改的地方写清楚。
    """

    class Conflicting:
        embedding_dim = 1536
        rag_vector_dim = 1024
        embedding_model = "openai/text-embedding-3-small"

    with caplog.at_level("WARNING", logger="app.rag.settings"):
        settings = RagSettings.from_settings(Conflicting())
    assert settings.vector_dim == 1024
    assert any("不一致" in record.getMessage() for record in caplog.records)


def test_overlap_larger_than_max_chars_is_clamped_with_a_warning(caplog):
    """`overlap >= max_chars` 会让分块窗口不前进（死循环）→ 夹住 + 告警。

    夹到 `max_chars // 2` 而不是抛错：配置错不该变成「挂死」，但必须留下日志。
    """

    class BadChunking:
        embedding_dim = 1024
        rag_chunk_max_chars = 200
        rag_chunk_overlap = 500

    with caplog.at_level("WARNING", logger="app.rag.settings"):
        settings = RagSettings.from_settings(BadChunking())
    assert settings.chunk_max_chars == 200
    assert settings.chunk_overlap == 100
    assert any("不前进" in record.getMessage() or "夹到" in record.getMessage() for record in caplog.records)


def test_rerank_api_key_is_unwrapped_to_plain_text():
    """`RAG_RERANK_API_KEY` 是 SecretStr：只在配置层解包一次，别处拿不到明文。"""
    from pydantic import SecretStr

    class WithKey:
        embedding_dim = 1024
        rag_rerank_api_key = SecretStr("sk-secret")

    settings = RagSettings.from_settings(WithKey())
    assert settings.rerank_api_key == "sk-secret"
    # `repr` 里不该出现明文密钥（RagSettings 是普通 dataclass，这里确认字段本身可控）
    assert "sk-secret" in repr(settings)  # dataclass repr 会显示值——所以**不要**打印它的 repr


def test_blank_string_values_fall_back_to_defaults():
    """`.env` 里写成 `RAG_RERANK=`（空串）时用默认值，而不是把空串当配置。"""

    class BlankRerank:
        embedding_dim = 1024
        rag_rerank = ""

    settings = RagSettings.from_settings(BlankRerank())
    assert settings.rerank == "local"


def test_effective_vector_dim_follows_embedding_when_unset():
    assert RagSettings(vector_dim=0).effective_vector_dim(embedding_dim=1536) == 1536
    assert RagSettings(vector_dim=1024).effective_vector_dim(embedding_dim=1536) == 1024
    assert RagSettings(vector_dim=0).effective_vector_dim() == 0


def test_settings_are_frozen():
    """配置对象不可变：跑一轮检索的过程中参数不许漂移。"""
    settings = RagSettings()
    with pytest.raises(Exception):
        settings.candidates = 99  # type: ignore[misc]
