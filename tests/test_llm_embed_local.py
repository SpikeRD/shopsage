"""本地 embedding 后端（`EMBEDDING_BACKEND=local`）的测试：**不联网、不需要 torch**。

对应 `docs/modules/5.llm.md` 第 4 节第 7、8 条，操作手册见 `docs/runbook/5.llm-runbook.md` §7.2–§7.8。

⚠️ 这里**不 import sentence_transformers**（它没装，装它要 2 GB）：
`client._load_local_model()` 是在**函数内** import 的（这正是为了「没装 extras 的环境也能用 API 后端」），
所以测试往 `sys.modules` 里塞一个假模块就能驱动整条本地路径。
假模块同时还钉住了两条契约：`encode(..., normalize_embeddings=True, convert_to_numpy=True)` 的调用形状，
以及「模型实例只加载一次」（每个请求加载一次 = 线上灾难）。
"""

import asyncio
import contextlib
import sys
import time
import types

import pytest

from app.llm import client as llm_client
from app.llm.errors import PermanentLLMError

from tests.test_llm_client_retry import MESSAGES, FastRetryPolicy, fake_settings  # noqa: F401


class _FakeArray:
    """假装是 `encode()` 返回的 numpy 二维数组。

    ⚠️ **故意不用 numpy**：numpy 只随 `--extra local` 装（默认环境没有），
    测试文件顶层 import numpy 会让「离线三件套」在默认环境直接 collection error。
    而 `client._embed_local` 对返回值只做 `for row in vectors` + `float(x)`，
    所以一个「可迭代的行 + 可迭代的元素」的小对象就够了 —— 这就是「假对象只需要实现被用到的那部分」。
    """

    def __init__(self, rows: list[list[float]] | list[list[int]]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


def _rows(texts, dim: int) -> list[list[float]]:
    """确定性结果：第 i 条文本的第 0 维 = i+1（用来验证顺序与条数）。"""
    return [[float(i + 1)] + [0.0] * (dim - 1) for i, _ in enumerate(texts)]


@pytest.fixture
def fast_retry(monkeypatch):
    monkeypatch.setattr(llm_client, "_RetryPolicy", FastRetryPolicy)


@pytest.fixture
def local_settings(monkeypatch):
    """切到本地后端；不读本机 .env。"""
    monkeypatch.setenv("LLM_PRIMARY", "deepseek/deepseek-flash")
    monkeypatch.setenv("LLM_API_KEY", "sk-fake")
    monkeypatch.setenv("LLM_FALLBACK", "")
    monkeypatch.setenv("EMBEDDING_BACKEND", "local")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("EMBEDDING_DIM", "0")
    monkeypatch.setenv("EMBEDDING_LOCAL_PATH", "")
    monkeypatch.setenv("EMBEDDING_DEVICE", "")
    from app.config import get_settings

    get_settings.cache_clear()
    _reset_local_model()
    yield
    get_settings.cache_clear()
    _reset_local_model()


def _reset_local_model() -> None:
    """清掉进程内模型单例（它是模块级全局，用例之间必须互不影响）。"""
    llm_client._local_model = None
    llm_client._local_model_name = None


def _install_fake_st(
    monkeypatch,
    *,
    dim: int = 3,
    delay: float = 0.0,
    loads: list[str] | None = None,
    encode_calls: list[list[str]] | None = None,
):
    """往 sys.modules 塞一个假的 sentence_transformers 模块，返回它。"""

    class FakeSentenceTransformer:
        def __init__(self, model_name, device=None):
            self.model_name = model_name
            self.device = device
            if loads is not None:
                loads.append(model_name)

        def encode(self, texts, normalize_embeddings=False, convert_to_numpy=False):
            if encode_calls is not None:
                encode_calls.append(list(texts))
            if delay:
                time.sleep(delay)  # 模拟同步 CPU 推理
            # 造一个「每条文本一个向量」的确定性结果：第 i 条的第 0 维 = i + 1
            return _FakeArray(_rows(texts, dim))

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return module


# ── 头号坑：不能阻塞事件循环；同时又必须串行（两者要同时成立）──────────


@pytest.mark.asyncio
async def test_local_backend_does_not_block_event_loop(
    monkeypatch, local_settings, fast_retry
):
    """⚠️ 忘了 `asyncio.to_thread` 就会红：同步的 encode 不能占着事件循环线程。

    判据是「encode 期间事件循环还能跑多少次别的协程」，而不是掐总耗时 ——
    因为本实现**故意串行** encode（见下一条用例），总耗时取决于机器，
    用时长当判据会把「串行是否必要」和「是否阻塞事件循环」两件事混在一起。
    """
    _install_fake_st(monkeypatch, delay=0.3)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.005)

    task = asyncio.create_task(ticker())
    try:
        await llm_client.embed(["a"])
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task   # 吃掉取消异常，避免 "Task was destroyed but it is pending"

    assert ticks > 10, (
        f"encode 期间事件循环只跑了 {ticks} 次 —— 说明 encode 占着事件循环线程。"
        "必须 asyncio.to_thread，否则本地推理时整个服务都会卡住"
    )


def test_local_encode_calls_never_overlap(monkeypatch, local_settings, fast_retry):
    """并发的两个 embed 之间，encode **绝不能重叠**（`SentenceTransformer` 实例不是线程安全的）。

    与上一条是一对：上一条要求「不阻塞事件循环」，这一条要求「同一实例不并发 encode」；
    同时满足的唯一写法就是「丢进线程 + 锁串行」。

    ⚠️ 判据必须是「是否重叠」本身，而不是「结果对不对」：真实的线程不安全会表现为
    偶发崩溃/静默结果错乱，**很难稳定复现**，用它当断言会得到一个时绿时红的用例。
    这里直接用一个 in-flight 计数器把「重叠」变成确定性事实 —— 去掉锁立刻红，
    而且与机器快慢、GIL 调度无关（`sleep` 一定会让出 GIL）。
    """
    in_flight = {"now": 0, "peak": 0}

    class MutualExclusionProbe:
        def __init__(self, model_name, device=None):
            pass

        def encode(self, texts, normalize_embeddings=False, convert_to_numpy=False):
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            try:
                time.sleep(0.05)   # 让出 GIL：并发的第二个线程必然能挤进来
                return _FakeArray(_rows(texts, 2))
            finally:
                in_flight["now"] -= 1

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = MutualExclusionProbe
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    async def both():
        return await asyncio.gather(llm_client.embed(["第一段"]), llm_client.embed(["第二段"]))

    vectors = asyncio.run(both())

    assert vectors == [[[1.0, 0.0]], [[1.0, 0.0]]]
    assert in_flight["peak"] == 1, (
        f"检测到 {in_flight['peak']} 个 encode 同时进行 —— 同一模型实例被并发使用。"
        "SentenceTransformer 不是线程安全的，必须用锁把 encode 串行"
        "（吞吐不够要靠多进程/GPU，而不是去掉锁）"
    )


# ── 模型加载：单例、传递对齐参数 ─────────────────────────────────


def test_local_backend_returns_vectors_in_input_order(monkeypatch, local_settings, fast_retry):
    """本地后端天然保序，但接口契约（与入参一一对齐）照旧适用。"""
    _install_fake_st(monkeypatch, dim=3)

    vectors = asyncio.run(llm_client.embed(["甲", "乙", "丙"]))

    assert vectors == [[1.0, 0.0, 0.0], [2.0, 0.0, 0.0], [3.0, 0.0, 0.0]]


def test_local_model_is_loaded_once(monkeypatch, local_settings, fast_retry):
    """模型只加载一次（进程内单例）：每次请求都加载 = 几百 MB 反复读盘/下载。"""
    loads: list[str] = []
    _install_fake_st(monkeypatch, loads=loads)

    asyncio.run(llm_client.embed(["a"]))
    asyncio.run(llm_client.embed(["b"]))
    asyncio.run(llm_client.embed(["c", "d"]))

    assert loads == ["BAAI/bge-m3"], f"应只加载一次，实际 {loads}"


def test_local_backend_passes_normalize_and_convert_flags(
    monkeypatch, local_settings, fast_retry
):
    """`normalize_embeddings=True` 必须真的传下去：归一化与否对应不同的相似度算子，
    阈值（13.cache.md）是按归一化+余弦调的，漏了就「阈值调不出来」。"""
    seen: dict[str, object] = {}

    class Spy:
        def __init__(self, model_name, device=None):
            pass

        def encode(self, texts, normalize_embeddings=False, convert_to_numpy=False):
            seen["normalize"] = normalize_embeddings
            seen["convert"] = convert_to_numpy
            return _FakeArray([[1.0, 2.0]])

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = Spy
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    asyncio.run(llm_client.embed(["a"]))

    assert seen == {"normalize": True, "convert": True}


def test_local_path_overrides_model_name(monkeypatch, local_settings, fast_retry):
    """离线环境用 `EMBEDDING_LOCAL_PATH` 指向本地目录（HF 仓库名换成路径）。"""
    monkeypatch.setenv("EMBEDDING_LOCAL_PATH", "D:/models/bge-m3")
    from app.config import get_settings

    get_settings.cache_clear()
    loads: list[str] = []
    _install_fake_st(monkeypatch, loads=loads)

    asyncio.run(llm_client.embed(["a"]))

    assert loads == ["D:/models/bge-m3"]


def test_local_device_is_forwarded(monkeypatch, local_settings, fast_retry):
    """`EMBEDDING_DEVICE=cpu` 要传给 SentenceTransformer（避免无 CUDA 机器上刷 warning）。"""
    monkeypatch.setenv("EMBEDDING_DEVICE", "cpu")
    from app.config import get_settings

    get_settings.cache_clear()
    devices: list[object] = []

    class Spy:
        def __init__(self, model_name, device=None):
            devices.append(device)

        def encode(self, texts, normalize_embeddings=False, convert_to_numpy=False):
            return _FakeArray([[1.0]])

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = Spy
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    asyncio.run(llm_client.embed(["a"]))

    assert devices == ["cpu"]


def test_empty_input_does_not_load_model(monkeypatch, local_settings, fast_retry):
    """空输入不发请求、也不该去加载模型（本地后端加载很贵）。"""
    loads: list[str] = []
    _install_fake_st(monkeypatch, loads=loads)

    assert asyncio.run(llm_client.embed([])) == []
    assert loads == []


# ── 缺可选依赖：报错必须能照着修 ─────────────────────────────────


def test_missing_sentence_transformers_error_is_actionable(
    monkeypatch, local_settings, fast_retry
):
    """没装 `sentence-transformers` 时报错要能照着修，而不是裸 ModuleNotFoundError。

    做法：让 `import sentence_transformers` 抛 ImportError（把 sys.modules 里的名字设成 None）。
    """
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    with pytest.raises(PermanentLLMError) as excinfo:
        asyncio.run(llm_client.embed(["a"]))

    message = str(excinfo.value)
    assert "sentence-transformers" in message
    assert "uv sync --extra local" in message, "报错里要给出可直接执行的修法"
    assert "EMBEDDING_BACKEND" in message, "同时给出「切回 API 后端」这条退路"


# ── 维度守卫（runbook §7.6） ─────────────────────────────────────


def test_dim_guard_raises_on_mismatch(monkeypatch, local_settings, fast_retry):
    """`EMBEDDING_DIM` 与实际不符 → 报错（防「维度相同但空间不相容」的静默错）。"""
    monkeypatch.setenv("EMBEDDING_DIM", "1024")  # 假模型实际给 3 维
    from app.config import get_settings

    get_settings.cache_clear()
    calls: list[list[str]] = []
    _install_fake_st(monkeypatch, dim=3, encode_calls=calls)

    with pytest.raises(PermanentLLMError) as excinfo:
        asyncio.run(llm_client.embed(["a"]))

    message = str(excinfo.value)
    assert "期望 1024" in message and "实际 3" in message
    assert "vector(n)" in message, "报错要指出同步三处（含 rag 建表）"
    assert len(calls) == 1, "维度不符属不可恢复错误，不该重试"


def test_dim_guard_passes_when_matching(monkeypatch, local_settings, fast_retry):
    """维度对得上就正常返回（守卫不能误杀）。"""
    monkeypatch.setenv("EMBEDDING_DIM", "3")
    from app.config import get_settings

    get_settings.cache_clear()
    _install_fake_st(monkeypatch, dim=3)

    vectors = asyncio.run(llm_client.embed(["a", "b"]))

    assert [len(v) for v in vectors] == [3, 3]


def test_assert_embedding_dim_rejects_ragged_batch():
    """同批维度不一致（被截断/混模型）→ 直接报错，不返回半残结果。"""
    with pytest.raises(PermanentLLMError) as excinfo:
        llm_client._assert_embedding_dim(
            [[1.0, 2.0], [1.0, 2.0, 3.0]], expected=0, model="m", backend="local"
        )
    assert "维度不一致" in str(excinfo.value)


# ── 与 API 后端共存：切后端不改调用方代码 ────────────────────────


def test_backend_switch_keeps_the_same_signature(monkeypatch, fast_retry):
    """两种后端对调用方是同一条代码路径：只换配置，不换调用方式。"""
    import inspect

    params = inspect.signature(llm_client.embed).parameters
    assert list(params) == ["texts", "model", "max_retries", "timeout"]
    assert "backend" not in params, "后端应由 config 决定，不该加进调用签名"


def test_api_backend_still_used_by_default(monkeypatch, fake_settings, fast_retry):
    """默认（未设 EMBEDDING_BACKEND）必须走 litellm，不能被本地后端抢走。"""
    called: dict[str, object] = {}

    async def fake_aembedding(**kwargs):
        called.update(kwargs)
        return _api_response([[1.0, 2.0]])

    monkeypatch.setattr(llm_client.litellm, "aembedding", fake_aembedding)

    vectors = asyncio.run(llm_client.embed(["a"]))

    assert vectors == [[1.0, 2.0]]
    assert called["model"] == "text-embedding-3-small"  # fake_settings 里设的值


def test_embedding_api_key_prefers_dedicated_key(monkeypatch, fake_settings, fast_retry):
    """单独配了 `EMBEDDING_API_KEY` 就用它；留空才复用主模型密钥（runbook §1.2.1 第 3 条）。"""
    seen: dict[str, object] = {}

    async def fake_aembedding(**kwargs):
        seen.update(kwargs)
        return _api_response([[1.0]])

    monkeypatch.setattr(llm_client.litellm, "aembedding", fake_aembedding)

    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-embed-dedicated")
    from app.config import get_settings

    get_settings.cache_clear()
    asyncio.run(llm_client.embed(["a"]))
    assert seen["api_key"] == "sk-embed-dedicated"

    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    get_settings.cache_clear()
    asyncio.run(llm_client.embed(["a"]))
    assert seen["api_key"] == "sk-fake-for-test", "留空时应回落到主模型密钥"


def _api_response(vectors):
    """造一个 litellm 形状的 EmbeddingResponse（故意逆序，验证按 index 排序）。"""

    class _Item(dict):
        pass

    items = [_Item(embedding=vec, index=i, object="embedding") for i, vec in enumerate(vectors)]

    class _Response:
        data = list(reversed(items))

    return _Response()
