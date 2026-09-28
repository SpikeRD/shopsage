"""`embed()` 的测试：**不联网、不需要真 key**。

对应 `docs/modules/5.llm.md` 第 6 节第 3 条（embedding 同样走 LiteLLM 门面，供 rag/cache 使用）。

⚠️ 这里**不用** litellm 的 `mock_response=` 来造多元素响应，有两个实测坑（1.103.0）：
  ① `input=['a','b']` + `mock_response=['x','y']` 只返回**一个** data 项
     （`data[0]['embedding'] == ['x','y']`）→ 拿它做顺序断言会「假绿」；
  ② 把 `model="bge-m3"` 这类非 litellm 认识的模型名配 mock 会**直接抛异常**
     （mock 未生效，litellm 试图解析 provider）。
所以这里 monkeypatch `litellm.aembedding`，自己造「故意逆序」的响应对象；
假对象只实现 `data` 一个属性（`embed()` 也只用这一个），并用 `_FakeEmbedding`
复现真实 `litellm.Embedding` 的 `__getitem__` 行为 —— 该假设由
`tests/test_llm_errors.py::test_real_litellm_embedding_item_supports_dict_access` 钉住。
"""

import asyncio
import ast
import inspect
from dataclasses import dataclass

import litellm
import pytest

from app.llm import client as llm_client
from app.llm.errors import PermanentLLMError, RetryableLLMError

from tests.test_llm_client_retry import FastRetryPolicy, fake_settings  # noqa: F401


class _FakeEmbedding:
    """复现真实 `litellm.Embedding` 的下标访问（`item["index"]` / `item["embedding"]`）。"""

    def __init__(self, index: int, embedding: list) -> None:
        self._data = {"index": index, "embedding": embedding, "object": "embedding"}

    def __getitem__(self, key: str):
        return self._data[key]


@dataclass
class _EmbeddingResponse:
    """只提供 `embed()` 用到的 `data` 属性（真实对象还有 model/object/usage）。"""

    data: list


@pytest.fixture
def fast_retry(monkeypatch):
    monkeypatch.setattr(llm_client, "_RetryPolicy", FastRetryPolicy)


def _embedding_response(pairs, reverse=True):
    """造 index/向量一一对应的响应；默认**逆序**返回，用来验证 embed() 会按 index 排序。"""
    items = [_FakeEmbedding(index=idx, embedding=vec) for idx, vec in pairs]
    return _EmbeddingResponse(data=list(reversed(items)) if reverse else items)


def _install(monkeypatch, response, recorder=None):
    async def fake_aembedding(**kwargs):
        if recorder is not None:
            recorder.update(kwargs)
        return response

    monkeypatch.setattr(llm_client.litellm, "aembedding", fake_aembedding)


def test_embed_preserves_input_order(monkeypatch, fake_settings, fast_retry):
    """批量 embedding 的顺序必须与入参一致（乱序 = 检索答非所问，最隐蔽的一类 bug）。"""
    texts = ["退货政策是什么", "怎么改地址", "发票怎么开"]
    pairs = [(0, [1.0, 0.0]), (1, [0.0, 1.0]), (2, [0.5, 0.5])]
    _install(monkeypatch, _embedding_response(pairs, reverse=True))

    vectors = asyncio.run(llm_client.embed(texts))

    assert vectors == [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]], "向量必须按 index 对齐入参，而不是按返回顺序"


def test_embed_passes_model_and_disables_internal_retry(monkeypatch, fake_settings, fast_retry):
    """模型名取 `settings.embedding_model`；`num_retries=0` 同样要传下去。"""
    seen: dict[str, object] = {}
    _install(monkeypatch, _embedding_response([(0, [1.0])]), recorder=seen)

    asyncio.run(llm_client.embed(["a"]))

    assert seen["model"] == "text-embedding-3-small", "默认应用 config 的 embedding_model"
    assert seen["input"] == ["a"], "input 应为文本列表"
    assert seen["num_retries"] == 0, "litellm 自带重试必须关掉（否则两层重试）"
    assert seen["api_key"] == "sk-fake-for-test"


def test_embed_explicit_model_overrides_config(monkeypatch, fake_settings, fast_retry):
    """显式传 `model=` 时不被 `embedding_model` 覆盖（第 10 步 rag 可能用另一个厂商）。"""
    seen: dict[str, object] = {}
    _install(monkeypatch, _embedding_response([(0, [1.0])]), recorder=seen)

    asyncio.run(llm_client.embed(["a"], model="bge-m3"))

    assert seen["model"] == "bge-m3", "显式传入的模型名不应被 config 的 embedding_model 覆盖"


def test_embed_empty_input_makes_no_request(monkeypatch, fake_settings, fast_retry):
    """空输入不该发请求（省一次计费，也避免某些 provider 对空批返回怪异结构）。"""
    calls = {"n": 0}

    async def fake_aembedding(**kwargs):
        calls["n"] += 1
        return _embedding_response([])

    monkeypatch.setattr(llm_client.litellm, "aembedding", fake_aembedding)

    assert asyncio.run(llm_client.embed([])) == []
    assert calls["n"] == 0


def test_embed_count_mismatch_raises_permanent(monkeypatch, fake_settings, fast_retry):
    """返回条数与入参不一致 → 报错，**绝不**返回一个「长度不对但看着对」的列表。"""
    _install(monkeypatch, _embedding_response([(0, [1.0])]))

    with pytest.raises(PermanentLLMError) as excinfo:
        asyncio.run(llm_client.embed(["a", "b"]))

    assert "不一致" in str(excinfo.value)


def test_embed_dim_consistent(monkeypatch, fake_settings, fast_retry):
    """同一批返回的向量维度一致（不一致说明混了模型/被截断）。"""
    _install(monkeypatch, _embedding_response([(0, [1.0, 2.0]), (1, [3.0, 4.0])]))

    vectors = asyncio.run(llm_client.embed(["a", "b"]))

    assert {len(v) for v in vectors} == {2}


def test_embed_retries_on_429_then_succeeds(monkeypatch, fake_settings, fast_retry):
    """embedding 与 llm_call 共用同一套退避策略（不重复实现）。"""
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise litellm.exceptions.RateLimitError(
                message="rate limited", llm_provider="openai", model=kwargs.get("model")
            )
        return _embedding_response([(0, [1.0, 2.0])])

    monkeypatch.setattr(llm_client.litellm, "aembedding", flaky)

    vectors = asyncio.run(llm_client.embed(["a"], max_retries=2))

    assert vectors == [[1.0, 2.0]]
    assert calls["n"] == 2


def test_embed_permanent_error_not_retried(monkeypatch, fake_settings, fast_retry):
    """400 对 embedding 同样不可恢复：一次都不重试。"""
    calls = {"n": 0}

    async def bad(**kwargs):
        calls["n"] += 1
        raise litellm.exceptions.BadRequestError(
            message="bad embedding request", llm_provider="openai", model=kwargs.get("model")
        )

    monkeypatch.setattr(llm_client.litellm, "aembedding", bad)

    with pytest.raises(PermanentLLMError):
        asyncio.run(llm_client.embed(["a"], max_retries=3))
    assert calls["n"] == 1


def test_embed_exhausted_raises_retryable(monkeypatch, fake_settings, fast_retry):
    """一直限流 → 抛 `RetryableLLMError`（调用方可稍后重试）。"""
    calls = {"n": 0}

    async def always(**kwargs):
        calls["n"] += 1
        raise litellm.exceptions.RateLimitError(
            message="rate limited", llm_provider="openai", model=kwargs.get("model")
        )

    monkeypatch.setattr(llm_client.litellm, "aembedding", always)

    with pytest.raises(RetryableLLMError):
        asyncio.run(llm_client.embed(["a"], max_retries=2))
    assert calls["n"] == 3


def test_llm_call_and_embed_share_one_policy():
    """两条路径必须共用同一个策略类与同一个执行包装（各写一份必然漂移）。

    用 AST 而不是字符串 `count()`：源码里 `_RetryPolicy(` 与 `num_retries=0` 在**注释/文档串**里
    也会出现，字符串计数会把注释算进去，得到假结论。
    """
    tree = ast.parse(inspect.getsource(llm_client))

    policy_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_RetryPolicy"
    ]
    assert len(policy_calls) == 1, f"退避策略应只有一处实例化，实际 {len(policy_calls)} 处"

    run_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run_with_retry"
    ]
    # llm_call 与 embed 各调用一次（def 里那次是 Call，不是 Call 的 FunctionDef 不计）
    assert len(run_calls) == 2, f"llm_call / embed 应各自复用 _run_with_retry，实际 {len(run_calls)} 处"

    # 两条路径都必须显式关闭 litellm 自带重试（num_retries=0）
    num_retries_zeros = [
        kw
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "num_retries"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value == 0
    ]
    assert len(num_retries_zeros) == 2, (
        f"llm_call 与 embed 都要写 num_retries=0，实际 {len(num_retries_zeros)} 处"
    )
