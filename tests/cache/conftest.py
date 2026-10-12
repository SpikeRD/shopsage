"""`tests/cache/` 的装置：**确定性的假 embedding + 内存缓存 + 内存版本表**。

三条纪律（与 `tests/queue/conftest.py`、`tests/rag/conftest.py` 同口径）：

1. **不连真 Redis**：键值层用 `MemoryKeyValue`（Redis 那一组用例单独开门，
   见 `test_cache_redis.py`，理由与第 12 步的 `SHOPSSAGE_TEST_REDIS` 完全一样）；
2. **不连真库**：条目表用 `MemoryEntryStore`；真库那几条打 `@pytest.mark.db`
   （依赖不可用时 `tests/conftest.py` 会 skip 并说明原因）；
3. **不花真钱**：embedding 用本文件的 `make_fake_embedder`，**零模型加载**。

## 假 embedding 为什么必须能「造出相似度」

`tests/rag/conftest.py` 的假 embedding 是**噪声**（hash 出来的向量与含义无关），
因为那边验的是「管道通不通」。缓存不一样：**它的全部语义就是相似度阈值** ——
拿噪声向量测阈值，得到的是「随机数落在 0.93 以上的概率」，
它在任何实现下都会通过或者都会失败，与代码对不对无关。

所以这里的假 embedding 是**可控的**：`SemanticSpace` 把一段文本映射到
**指定的单位向量**，用例通过「让两段文本的向量夹角 = 某个余弦值」直接构造灰区与命中区。
这样「阈值 0.93 → 0.92 不命中、0.94 命中」这条验收才是一条**真**断言。
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence

import pytest

from app.cache.backend import CacheKeySpace, MemoryKeyValue
from app.cache.corpus import MemoryRevisionStore
from app.cache.settings import CacheBackendSettings, CacheConfig, CacheSettings
from app.cache.stores import MemoryEntryStore

#: 本地用例的向量维度（比 1024 小得多：判定逻辑与维度无关，而小维度让用例跑得快）。
TEST_DIM = 8


def unit(*values: float) -> tuple[float, ...]:
    """把一串数归一成单位向量（余弦相似度只关心方向）。"""
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return tuple(value / norm for value in values)


def at_similarity(base: Sequence[float], target: float, *, axis: int = 1) -> tuple[float, ...]:
    """造一个与 `base` 的余弦相似度**恰好等于 `target`** 的单位向量。

    实现：在 `base` 之上叠加一个与它正交的方向（`axis` 分量），
    然后按 `cos = target` 解出两个分量的比例。
    `target=1` 时直接返回 `base` 的拷贝（正交分量系数为 0）。
    """
    b = unit(*base)
    if target >= 1.0:
        return b
    if target <= 0.0:
        other = [0.0] * len(b)
        other[axis] = 1.0
        return unit(*other)
    # base 与正交方向 other：v = t*b + sqrt(1-t^2)*other 是一个单位向量，
    # 它与 b 的余弦正好是 t。
    other_axis = axis if axis < len(b) else 0
    ortho = [0.0] * len(b)
    ortho[other_axis] = 1.0
    # 去掉 ortho 在 b 方向上的分量，保证两者正交（axis 选得合适时它本来就是 0）
    dot = sum(value * b[index] for index, value in enumerate(ortho))
    ortho = [value - dot * b[index] for index, value in enumerate(ortho)]
    norm = math.sqrt(sum(value * value for value in ortho)) or 1.0
    ortho = [value / norm for value in ortho]
    scale = math.sqrt(max(0.0, 1.0 - target * target))
    return unit(*[target * b[index] + scale * ortho[index] for index in range(len(b))])


class SemanticSpace:
    """「文本 → 向量」的**可控**映射（`embedder(text)` 就是它的 `__call__`）。

    用法：

        space = SemanticSpace(dim=8)
        space.place("怎么退货", similarity_with=None)        # 基准方向
        space.place("退货流程是什么", base="怎么退货", similarity=0.97)
        space.place("怎么改地址", base="怎么退货", similarity=0.62)
    """

    def __init__(self, dim: int = TEST_DIM) -> None:
        self.dim = int(dim)
        self._vectors: dict[str, tuple[float, ...]] = {}
        #: 已经用掉的正交方向数（每个 `base` 占一个，见 `place`）
        self._bases = 0
        self.calls: list[str] = []

    def place(self, text: str, *, base: str | None = None, similarity: float = 1.0, vector: Sequence[float] | None = None) -> tuple[float, ...]:
        # 幂等：已经放过就直接返回。理由：测试的常见写法是「先给追问问法定方向，
        # 再调 build()（它会把基准放好）」—— 顺序反了不该让第二次调用**静默改掉**向量。
        if text in self._vectors and vector is None:
            return self._vectors[text]
        if vector is not None:
            self._vectors[text] = unit(*vector)
            return self._vectors[text]
        if base is None:
            # 基准：给每个新基准一个**互相正交**的方向（第 n 个基准 = 第 n 个基）。
            # ⚠️ 用 `_bases` 而不是 `len(self._vectors)` 计数：同一个 base 上挂多条
            # 「相似问法」时，`len` 会把它们也算成基准，于是「怎么退货」（第 0 个）与
            # 「退货流程是什么」（第 1 个）**不正交** —— 而测试的整套推理建立在
            # 「不同基准互不相干」上（否则「不相干的问题不命中」那条用例根本测不了）。
            raw = [0.0] * self.dim
            raw[self._bases % self.dim] = 1.0
            self._bases += 1
            self._vectors[text] = unit(*raw)
            return self._vectors[text]
        # 这里 base 必须已经在场：不在场说明用例写错了（拼错了基准名），
        # **必须报错**而不是静默换一个方向 —— 静默会让「相似度 0.97 该命中」这条断言
        # 以一个「0.0 低于兜底线」的形态失败，看上去像判定写错了。
        if base not in self._vectors:
            raise KeyError(f"基准 {base!r} 还没放过：先 `place({base!r})` 再挂相似问法")
        self._vectors[text] = at_similarity(self._vectors[base], similarity)
        return self._vectors[text]

    def vector_of(self, text: str) -> tuple[float, ...]:
        if text not in self._vectors:
            # 未登记的文本：给一个与**所有基准**都接近正交的方向 —— 它模拟的是
            # 「库里没有这句话」，而**它绝不该命中**（这是最安全的一类未知）。
            #
            # ⚠️ 这个方向必须与**每一个可能被 `place(base=None)` 用到的下标**都不同。
            # 第一版用 `(_bases + 7) % dim` 算它 —— 在 `_bases=1`、`dim=8` 时结果是 **0**，
            # 也就是「正好等于第一个基准的方向」，于是**任何**未登记的问题都会与
            # 「怎么退货」相似度约 1.0 并命中（一条真实的假阳性，本仓实测踩到）。
            # 现在固定在 `dim - 1`：`place()` 的基准从下标 0 开始递增，
            # 只有在登记了 `dim - 1` 个基准之后才会碰到它（用例不可能用到那么多）。
            raw = [0.0] * self.dim
            raw[self.dim - 1] = 1.0
            raw[self.dim - 2] = 0.05
            self._vectors[text] = unit(*raw)
        return self._vectors[text]

    def __call__(self, text: str, *, cache: CacheSettings | None = None) -> Sequence[float]:
        del cache
        self.calls.append(str(text))
        return self.vector_of(str(text))

    def resolve_model(self, cache: CacheSettings | None = None) -> str:
        """假 embedder 也要有 `resolve_model`（指纹要用它；这模拟的是真适配器的接口）。"""
        del cache
        return "fake/model"


@pytest.fixture
def space() -> SemanticSpace:
    return SemanticSpace()


@pytest.fixture
def embedder(space: SemanticSpace) -> SemanticSpace:
    return space


@pytest.fixture
def key_value() -> MemoryKeyValue:
    return MemoryKeyValue()


@pytest.fixture
def entry_store() -> MemoryEntryStore:
    return MemoryEntryStore(max_entries=50)


@pytest.fixture
def revisions() -> MemoryRevisionStore:
    return MemoryRevisionStore()


@pytest.fixture
def settings() -> CacheSettings:
    """离线用例的默认配置：**与生产同口径**，只有 TTL 缩短不影响正确性。

    ⚠️ 刻意**不调低阈值**：阈值本身就是要被测的东西（`test_cache_core.py` 直接钉 0.93）。
    用例想改就显式传（`CacheSettings(semantic_threshold=0.5)`）——
    与「测试里偷偷把安全阀拧松」是两回事。
    """
    return CacheSettings()


@pytest.fixture
def cache(
    entry_store: MemoryEntryStore,
    key_value: MemoryKeyValue,
    revisions: MemoryRevisionStore,
    embedder: SemanticSpace,
    settings: CacheSettings,
) -> Iterator[object]:
    """一个**全离线**的 `Cache`（内存条目表 + 内存键值 + 内存版本表 + 假 embedding）。

    ⚠️ 它同时被装成**进程默认**（`set_default_cache`）：这样 `semantic_cache_get`
    这类门面函数在用例里也是安全的 —— 漏掉这一步的用例会去连 `.env` 里的真 Redis，
    症状是「用例随机变慢/偶尔红」（本仓踩过同类问题）。
    """
    from app.cache.api import Cache, set_default_cache

    instance = Cache(
        store=entry_store,
        results=_result_store(key_value),
        settings=settings,
        revisions=revisions,
        embedder=embedder,
        keys=CacheKeySpace(namespace="cache", instance="test"),
        key_value=key_value,
        degraded=False,
    )
    set_default_cache(instance)
    from app.cache.corpus import set_revision_store

    set_revision_store(revisions)
    try:
        yield instance
    finally:
        set_default_cache(None)
        set_revision_store(None)


def _result_store(key_value: MemoryKeyValue):
    from app.cache.result import ResultStore

    return ResultStore(key_value, namespace="cache", instance="test")


@pytest.fixture
def config(settings: CacheSettings) -> CacheConfig:
    """一份「不回落到 Redis」的配置（造实例用）。"""
    return CacheConfig(cache=settings, backend=CacheBackendSettings(redis_url="", instance="test"))


def make_cache(
    *,
    space: SemanticSpace,
    store: MemoryEntryStore | None = None,
    key_value: MemoryKeyValue | None = None,
    revisions: MemoryRevisionStore | None = None,
    settings: CacheSettings | None = None,
    base: str | None = None,
) -> object:
    """造一个全离线的 `Cache`（用例想自己控制装置时用它，而不是用 `cache` 装置）。

    与 `cache` 装置的区别只有两个：**不装成进程默认**，以及可以顺手放一个基准问题
    （`base="怎么退货"`）—— 需要「两个互不干扰的缓存实例」的用例（例如断言 scope 隔离）用它。
    """
    from app.cache.api import Cache

    if base is not None:
        space.place(base)
    kv = key_value if key_value is not None else MemoryKeyValue()
    return Cache(
        store=store if store is not None else MemoryEntryStore(max_entries=50),
        results=_result_store(kv),
        settings=settings or CacheSettings(),
        revisions=revisions if revisions is not None else MemoryRevisionStore(),
        embedder=space,
        keys=CacheKeySpace(namespace="cache", instance="test"),
        key_value=kv,
        degraded=False,
    )


def messages(*texts: str) -> list[dict[str, str]]:
    """按 `user` / `assistant` 交替造一串消息（最后一条一定是 user）。

    用法：`messages("怎么退货")` 或 `messages("我的订单呢", "已发货", "那退货运费谁出")`。
    """
    out: list[dict[str, str]] = []
    for index, text in enumerate(texts):
        role = "user" if index % 2 == 0 else "assistant"
        out.append({"role": role, "content": text})
    return out


# ── 真库装置（只有声明了 `@pytest.mark.db` 的用例才用它）─────────────────────


@pytest.fixture(scope="session")
def pg_ready() -> bool:
    """真库 + **缓存表**是否可用（不可用时用例自己 skip，而不是让整组报错）。

    ⚠️ 为什么本目录要自带一份（而不是复用 `tests/rag/conftest.py` 的）：
    那个装置检查的是 `rag_chunks`（第 10 步的表），而本步要的是 `cache_semantic_entries`。
    复用会让「缓存迁移没跑、但 RAG 迁移跑过了」这种状态**被误判成可用** ——
    然后整组用例红在 `relation "cache_semantic_entries" does not exist` 上。
    （pytest 的装置可见性是向下的，`tests/rag/conftest.py` 对 `tests/cache/` 本来也不可见。）
    """
    from tests.db_helpers import database_available

    if not database_available():
        pytest.skip(
            "PostgreSQL 不可达：检查 .env 的 DATABASE_URL host"
            "（本机无 Docker，依赖栈在 VMware-Linux 上；见 3.db-runbook.md §1）"
        )
    from sqlalchemy import text

    from app.cache.schema import ENTRIES_TABLE
    from app.db.session import get_read_engine

    try:
        with get_read_engine().connect() as conn:
            exists = conn.execute(
                text(f"select to_regclass('public.{ENTRIES_TABLE}') is not null")
            ).scalar()
    except Exception as exc:  # noqa: BLE001 - 探测本身出错也算不可用
        pytest.skip(f"缓存表探测失败：{type(exc).__name__}: {exc}")
    if not exists:
        pytest.skip("缓存表不存在：先跑 `uv run alembic upgrade head`（第 13 步的迁移）")
    return True
