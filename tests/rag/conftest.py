"""`tests/rag/` 的公共装置（`tests/conftest.py` 里另有两个全局装置）。

三件事：

1. **离线优先**：绝大多数用例不碰数据库、不加载模型 —— 用假 embedding 驱动整条链路。
   这样做不是因为「连库麻烦」，而是因为**契约与算法不需要真库**：
   分块策略、RRF 融合、token 压缩、降级路径，用假后端测反而更干净（与
   `tests/test_tools_retriever.py` 用 `search=` 注入是同一个思路）。
2. **真库用例单独分组**（`tests/rag/test_rag_store_db.py` / `test_rag_integration_db.py`）：
   关键词腿的 BM25 是**写在 SQL 里的**，不跑真 PostgreSQL 等于什么都没测。
   它们在建表前先探测依赖，不可用时整组 skip（仓库的统一口径，见 `tests/conftest.py`）。
3. **临时表而不是临时库**：本仓只有一套开发库（没有 Docker/容器数据库），
   所以真库用例在 `public` schema 里建 `test_rag_*` 表，用完删掉。
   表名用同一套 DDL（`app/rag/schema.create_statements`）——保证「测的就是生产结构」。
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from app.rag.parse import ParsedDocument
from app.rag.settings import RagSettings

# 临时表名（真库用例用；与生产表名不同，避免污染开发库里的真实语料）
TEST_CHUNKS_TABLE = "test_rag_chunks"
TEST_DOCUMENTS_TABLE = "test_rag_documents"
TEST_STATS_TABLE = "test_rag_corpus_stats"
TEST_NGRAM_FUNCTION = "test_rag_chunk_ngrams"
TEST_NGRAM_TRIGGER = "test_rag_chunks_ngram_tsv"


def pytest_configure(config) -> None:
    """本目录的标记注册 **与 `--rag-live` 开关都在根 `tests/conftest.py` 里**。

    为什么不在本文件里注册（本仓踩过）：pytest 报
    `error: unrecognized arguments: --rag-live` —— 根 conftest 的加载发生在参数解析期间，
    而子目录 conftest 要到「收集该目录的用例」时才加载。也就是说
    `uv run pytest -q -m "not llm_live" --rag-live`（全量 + 真模型）会直接启动失败，
    而 `uv run pytest -q tests/rag --rag-live` 却是好的 ——
    这种「只有某种用法可用」的开关最容易被误认为「参数没生效」。
    """
    return None


def pytest_collection_modifyitems(config, items) -> None:
    """把 `rag_live` 用例在默认情况下 skip —— 但要**说清楚怎么跑**。

    为什么默认不跑（与第 9 步 `llm_live` 同一个理由）：
    本地 CrossEncoder + bge-m3 加载一次要十几秒、跑一轮要几十秒，
    放进默认套件会让「`uv run pytest -q`」从 10 秒变成 1 分钟 —— 而它验证的是
    「真模型下检索质量」，不是「代码是否坏」。
    """
    if config.getoption("--rag-live", default=False):
        return
    skip = pytest.mark.skip(reason="需要真模型：加 `--rag-live` 才跑（见 tests/conftest.py）")
    for item in items:
        if item.get_closest_marker("rag_live"):
            item.add_marker(skip)


# ── 假 embedding（离线全链路的驱动器）────────────────────────────────────────


def make_fake_embedder(dimension: int = 1024, overrides: dict[str, Sequence[float]] | None = None):
    """造一个**确定性**假 embedding：同一个文本永远得到同一个单位向量。

    为什么必须是确定性的：增量更新的用例要断言「未变的片段不再重新 embedding」，
    而断言「向量相同」是最直接的证据。随机向量会让这条断言永远失败
    （看起来像「增量没生效」，其实是测试装置的问题）。

    语义上它是**噪声**（hash 出来的向量与文本含义无关）——这是刻意的：
    本目录的用例验证的是「管道是否接通、增量的账目对不对」，
    而「语义检索好不好」由 `test_rag_live.py` 用真模型回答。
    `overrides` 用来给特定文本指定向量（构造「向量腿挑错、关键词腿挑对」这类场景）。
    """

    async def _embed(texts, **_kwargs):
        vectors: list[list[float]] = []
        for text in texts:
            if overrides and text in overrides:
                vectors.append([float(value) for value in overrides[text]])
                continue
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [(byte - 128) / 128.0 for byte in digest]
            # 32 字节 → 循环平铺到 dimension，再归一化（单位向量：与 cosine 检索口径一致）
            values = [raw[index % len(raw)] for index in range(dimension)]
            norm = math.sqrt(sum(value * value for value in values)) or 1.0
            vectors.append([value / norm for value in values])
        return vectors

    _embed.calls = []  # type: ignore[attr-defined]

    async def _counting(texts, **kwargs):
        _embed.calls.append(list(texts))  # type: ignore[attr-defined]
        return await _embed(texts, **kwargs)

    _counting.calls = _embed.calls  # type: ignore[attr-defined]
    return _counting


@pytest.fixture
def fake_embedder():
    """默认的假 embedding（1024 维，确定性）。"""
    return make_fake_embedder(1024)


@pytest.fixture
def offline_settings() -> RagSettings:
    """离线用例的检索参数：**关掉 rerank**（不加载模型）、候选池小一点（跑得快）。"""
    return RagSettings(
        vector_dim=1024,
        candidates=10,
        rrf_k=60,
        keyword_min_ratio=0.6,
        rerank="off",
        max_top_k=20,
        chunk_max_chars=400,
        chunk_overlap=60,
    )


# ── 样本文档 ────────────────────────────────────────────────────────────────

POLICY_TEXT = """# 退货与换货政策

本政策适用于本店全部自营商品。

## 一、无理由退货

第 1 条：自签收之日起 7 天内，商品完好可申请无理由退货。

第 2 条：无理由退货的运费由买家承担；质量问题由本店承担。

## 二、不予受理

第 3 条：生鲜、定制商品、已拆封的音像制品不适用无理由退货。
"""

FAQ_TEXT = """# 常见问题

### 如何修改收货地址？

在订单详情页点击「修改地址」，仅未发货订单可改。

### 能否开具增值税专用发票？

可以，下单时填写发票抬头与税号。
"""

MANUAL_TEXT = """# 产品手册

## 1. 安装

1.1 开箱后先核对配件清单。

1.2 将电源适配器接入 220V 插座。

## 2. 保养

2.1 每三个月清洁一次滤网。

2.2 长期不用时请断开电源。
"""


@pytest.fixture
def policy_document() -> ParsedDocument:
    return ParsedDocument(source="tests-fixtures/退货政策.md", text=POLICY_TEXT, kind="markdown")


@pytest.fixture
def faq_document() -> ParsedDocument:
    return ParsedDocument(source="tests-fixtures/常见问题.md", text=FAQ_TEXT, kind="markdown")


@pytest.fixture
def manual_document() -> ParsedDocument:
    return ParsedDocument(source="tests-fixtures/产品手册.md", text=MANUAL_TEXT, kind="markdown")


# ── 真库装置（懒加载：只有声明了才连库）─────────────────────────────────────


def _database_ready() -> tuple[bool, str]:
    """库可用吗？返回 `(可用, 原因)`（不可用时给一句能照着修的话）。"""
    try:
        from tests.db_helpers import database_available

        if not database_available():
            return False, (
                "PostgreSQL 不可达：检查 .env 的 DATABASE_URL host"
                "（本机无 Docker，依赖栈在 VMware-Linux 上；见 3.db-runbook.md §1）"
            )
    except Exception as exc:  # noqa: BLE001 - 装置不该因为探测本身出错而炸
        return False, f"数据库探测失败：{type(exc).__name__}: {exc}"
    return True, ""


def rag_tables_ready() -> tuple[bool, str]:
    """向量表建好了吗（没跑迁移时 skip 真库用例）。"""
    ok, reason = _database_ready()
    if not ok:
        return ok, reason
    from sqlalchemy import text

    from app.db.session import get_read_engine

    try:
        with get_read_engine().connect() as conn:
            exists = conn.execute(
                text("select to_regclass('public.rag_chunks') is not null")
            ).scalar()
    except Exception as exc:  # noqa: BLE001
        return False, f"向量表探测失败：{type(exc).__name__}: {exc}"
    if not exists:
        return False, "向量表不存在：先跑 `uv run alembic upgrade head`（第 10 步的迁移）"
    return True, ""


@pytest.fixture(scope="session")
def pg_ready() -> bool:
    """真库是否可用（不可用时用例自己 skip，而不是让整组报错）。"""
    ok, reason = rag_tables_ready()
    if not ok:
        pytest.skip(reason)
    return True


@pytest.fixture
def scratch_store(pg_ready):
    """一张**同构的临时表**上的 `RagStore`（用完删表）。

    为什么不留数据给下一条用例：真库用例之间共享同一个库，
    留数据会让「命中数」这类断言互相干扰（第 9 步的演示脚本踩过同一个坑：
    场景之间共享同一份额度）。每条用例自带一套表，清理是确定的。
    """
    from app.db.session import get_write_engine
    from app.rag.schema import create_statements, drop_statements
    from app.rag.store import RagStore

    statements = create_statements(
        1024,
        chunks_table=TEST_CHUNKS_TABLE,
        documents_table=TEST_DOCUMENTS_TABLE,
        stats_table=TEST_STATS_TABLE,
        function_name=TEST_NGRAM_FUNCTION,
        trigger_name=TEST_NGRAM_TRIGGER,
    )
    drops = drop_statements(
        chunks_table=TEST_CHUNKS_TABLE,
        documents_table=TEST_DOCUMENTS_TABLE,
        stats_table=TEST_STATS_TABLE,
        function_name=TEST_NGRAM_FUNCTION,
        trigger_name=TEST_NGRAM_TRIGGER,
    )

    engine = get_write_engine()
    with engine.begin() as conn:
        for statement in drops:
            conn.exec_driver_sql(statement)
        for statement in statements:
            conn.exec_driver_sql(statement)

    store = RagStore(
        engine=engine,
        read_engine=engine,
        chunks_table=TEST_CHUNKS_TABLE,
        documents_table=TEST_DOCUMENTS_TABLE,
        stats_table=TEST_STATS_TABLE,
        dimension=1024,
    )
    try:
        yield store
    finally:
        with engine.begin() as conn:
            for statement in drops:
                conn.exec_driver_sql(statement)


@pytest.fixture
def scratch_ingest(scratch_store, offline_settings, fake_embedder):
    """往临时表里灌文档的小助手（`ingest(source=..., text=...)`）。

    它还暴露：
      · `store` —— 底层 `RagStore`（要直接查库断言时用）；
      · `embedded_chunks` —— **本次用例里真正被送去 embedding 的文本**，
        这是「增量有没有省下 embedding」的唯一直接证据（比数 SQL 次数可靠）。
    """
    import asyncio

    from app.rag.ingest import delete_document, ingest_document, reindex_document

    class Helper:
        def __init__(self) -> None:
            self.store = scratch_store
            self.settings = offline_settings
            self.embedded_batches: list[list[str]] = []

        async def _embed(self, texts, **kwargs):
            self.embedded_batches.append(list(texts))
            return await fake_embedder(texts, **kwargs)

        def ingest(self, source: str, text: str, **kwargs):
            return asyncio.run(
                ingest_document(
                    source,
                    content=text,
                    store=self.store,
                    settings=self.settings,
                    embedder=self._embed,
                    **kwargs,
                )
            )

        def reindex(self, source: str, text: str, **kwargs):
            return asyncio.run(
                reindex_document(
                    source,
                    content=text,
                    store=self.store,
                    settings=self.settings,
                    embedder=self._embed,
                    **kwargs,
                )
            )

        def delete(self, source: str, *, hard: bool = True):
            return asyncio.run(
                delete_document(source, hard=hard, store=self.store, settings=self.settings)
            )

        def retriever(self):
            """用同一个 store / embedder 造一个检索器（rerank 关掉：不加载模型）。"""
            from app.rag.rerank import NoopReranker
            from app.rag.retriever import Retriever

            return Retriever(
                store=self.store,
                settings=self.settings,
                embedder=self._embed,
                reranker=NoopReranker(),
            )

        @property
        def embedded_chunks(self) -> list[str]:
            return [text for batch in self.embedded_batches for text in batch]

    return Helper()


@pytest.fixture
def document_factory() -> Iterator[callable]:
    """造 `ParsedDocument`：`factory(text="…", source="…", kind="markdown", pages=0)`。"""

    def _make(text: str, source: str = "tests-fixtures/doc.md", kind: str = "markdown", pages: int = 0) -> ParsedDocument:
        return ParsedDocument(source=source, text=text, kind=kind, pages=pages)

    yield _make


@pytest.fixture
def local_tmp() -> Iterator[Path]:
    """**仓库内**的可写临时目录（替代 pytest 的 `tmp_path`）。

    ⚠️ 为什么不能直接用 `tmp_path`（本仓实测）：开发机在受限沙箱下，
    pytest 的基线临时目录（`%LOCALAPPDATA%\\Temp\\dsh-*\\pytest-of-*`）**不可写**，
    一旦用例声明 `tmp_path`，fixture 在 setup 阶段就抛
    `PermissionError: [WinError 5]`（`tests/test_config.py` 里那 6 条 error 就是这个）——
    表现为「用例根本没跑起来」，很容易被误读成「代码坏了」。

    工作区内的路径是可写的，所以临时目录建在工作区里；用例级唯一性用 uuid
    （而不是 `tmp_path.name`：那个 fixture 本身就会触发上面的报错）。
    目录随用例结束删除，避免脏文件进 Git。
    """
    import shutil
    import uuid

    root = Path(".pytest-tmp") / "rag" / uuid.uuid4().hex[:8]
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        if os.environ.get("RAG_KEEP_TMP"):
            return
        shutil.rmtree(root, ignore_errors=True)
