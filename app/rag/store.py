"""向量库的读写层：**所有 SQL 都在这一个模块里**。

分层的理由：`retriever.py` 关心「怎么融合、怎么重排」（纯算法，可脱库单测），
本模块关心「SQL 怎么写、索引有没有吃到」（必须有真库才能测）。混在一起时，
想单测 RRF 就得先起一个 PostgreSQL。

三条设计口径：

1. **关键词腿是「n-gram 词表 + tsvector + SQL 内手算 BM25」**，不是 `ts_rank`。
   为什么不用 PostgreSQL 自带的 `ts_rank_cd`：它算的是 cover density（覆盖密度），
   没有 IDF 与长度归一化的标准形式，于是「稀有词」与「满篇都有的字」权重相同 ——
   在中文 1-gram 参与评分时这会直接毁掉排序（见 `textkit.py` 的实测）。
   自己算 BM25 需要三个量：tf（从 tsvector 的 nentry 里取）、df（按词逐个 `@@` 数，
   每个词一次 GIN 索引扫描）、N/avgdl（`rag_corpus_stats` 一张单行表，入库时维护）。
2. **候选门用「bigram 覆盖率」而不是 `AND`**：`AND` 太脆（少打一个字就零召回），
   `OR` 太松（一个字相同就召回）。覆盖率阈值（默认 0.6）是这两者之间唯一可调的旋钮，
   且分母会剔除「库里根本没有的词」（错字不该把覆盖率打到 0）。
3. **所有写操作在一个事务里**（增量更新要么整体生效、要么整体回滚）：
   半途失败会留下「文档说版本 3、片段还是版本 2」的中间态，那比重跑一次贵得多。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import get_read_engine, get_write_engine
from app.rag.chunking import TextChunk
from app.rag.schema import CHUNKS_TABLE, DOCUMENTS_TABLE, STATS_TABLE
from app.rag.textkit import index_text, index_tokens, query_terms

logger = logging.getLogger(__name__)

# 关键词腿一次最多数多少个词的 df —— 每个词一次索引扫描，词太多会把检索变慢。
# 查询侧的词表上限是 64（textkit.MAX_QUERY_TERMS），这里再收一道。
MAX_DF_TERMS = 40

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class StoreError(RuntimeError):
    """库层错误（表不存在、维度不符…）：消息必须能照着修。"""


@dataclass(frozen=True)
class VectorHit:
    """向量腿的一条命中。"""

    id: int
    source: str
    chunk_id: str
    text: str
    section: str | None
    page: int | None
    score: float
    updated_at: Any = None
    category: str = "general"


@dataclass(frozen=True)
class KeywordHit:
    """关键词腿的一条命中。`matched` 是命中的词元数（可解释性用）。"""

    id: int
    source: str
    chunk_id: str
    text: str
    section: str | None
    page: int | None
    score: float
    matched: int = 0
    updated_at: Any = None


@dataclass
class UpsertReport:
    """一次文档入库的结果（增量更新的证据）。"""

    doc_id: int = 0
    version: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deactivated: int = 0
    purged: int = 0
    chunks_total: int = 0
    doc_changed: bool = False
    purged_chunk_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "version": self.version,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deactivated": self.deactivated,
            "purged": self.purged,
            "chunks_total": self.chunks_total,
            "doc_changed": self.doc_changed,
        }


class RagStore:
    """向量库读写（表名可覆盖：测试用同构的临时表跑真 SQL）。

    连接策略：**写走写引擎、读走读引擎**（与 `3.db.md` 的读写分离口径一致）。
    读路径复用的是给 Text-to-SQL 的只读角色 `shopsage_readonly`：
    向量检索是**读**，走只读连接既符合语义、又顺手证明了「检索不会写库」。
    代价是只读角色必须能 select 这三张新表 —— 迁移后要补一次授权，
    `scripts/db_readonly.py` 的 GRANT 列表里已经加上（见 runbook §2.3）。
    """

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        read_engine: Engine | None = None,
        chunks_table: str = CHUNKS_TABLE,
        documents_table: str = DOCUMENTS_TABLE,
        stats_table: str = STATS_TABLE,
        dimension: int | None = None,
    ) -> None:
        for name, value in (
            ("chunks_table", chunks_table),
            ("documents_table", documents_table),
            ("stats_table", stats_table),
        ):
            if not _IDENTIFIER.match(value):
                raise StoreError(f"表名只允许标识符字符，收到 {name}={value!r}")
        self._engine = engine
        self._read_engine = read_engine
        self.chunks_table = chunks_table
        self.documents_table = documents_table
        self.stats_table = stats_table
        self.dimension = dimension

    # ── 连接 ────────────────────────────────────────────────────────────────
    @property
    def engine(self) -> Engine:
        return self._engine if self._engine is not None else get_write_engine()

    @property
    def read_engine(self) -> Engine:
        if self._read_engine is not None:
            return self._read_engine
        return get_read_engine()

    def _check_error(self, exc: SQLAlchemyError) -> StoreError:
        """把驱动报错翻成能照着修的说明（最常见的一类：忘了跑迁移）。"""
        message = str(exc)
        if "does not exist" in message and self.chunks_table in message:
            return StoreError(
                f"向量表 {self.chunks_table} 不存在：先执行 `uv run alembic upgrade head`"
                "（第 10 步的迁移会建 rag_documents / rag_chunks / rag_corpus_stats 三张表）。"
                f"原始报错：{message.splitlines()[0]}"
            )
        if "expected" in message and "dimensions" in message:
            return StoreError(
                f"向量维度不符：{message.splitlines()[0]}。请把 EMBEDDING_DIM / RAG_VECTOR_DIM 与"
                "向量表的 vector(n) 统一（三处必须一起改，然后重建索引 + 重灌向量）。"
            )
        return StoreError(message.splitlines()[0])

    # ── 写：文档级 ───────────────────────────────────────────────────────────
    def change_plan(
        self,
        source: str,
        incoming: Mapping[str, str],
    ) -> tuple[int, dict[str, str]]:
        """入库**前置计划**：返回 `(doc_id, 变更的片段 → 新内容 hash)`。

        为什么需要它（本仓实测踩到的一个真问题）：向量化的代价远高于写库，
        而「哪些片段需要重嵌」这个判断如果放在 `upsert_document()` 里面，
        **必须在调用它之前**就知道 —— 否则只能先给未变的片段塞占位向量，
        既浪费一次循环、又多一条「占位向量漏写进库」的失败路径。
        抽成一次只读查询（`create` 时返回 `(0, 全部)`），让 `ingest.py` 能先算向量再写库。
        """
        with self.read_engine.connect() as conn:
            row = conn.execute(
                text(
                    f"select d.doc_id, c.chunk_id, c.content_hash "
                    f"from {self.documents_table} d "
                    f"left join {self.chunks_table} c on c.doc_id = d.doc_id and c.is_active "
                    "where d.source = :source"
                ),
                {"source": source},
            ).all()
        if not row:
            return 0, dict(incoming)

        doc_id = int(row[0].doc_id)
        existing = {
            str(item.chunk_id): str(item.content_hash)
            for item in row
            if item.chunk_id is not None
        }
        changed = {
            chunk_id: content_hash
            for chunk_id, content_hash in incoming.items()
            if existing.get(chunk_id) != content_hash
        }
        return doc_id, changed

    def upsert_document(
        self,
        *,
        source: str,
        doc_hash: str,
        category: str,
        strategy: str,
        chunks: Sequence[TextChunk],
        embeddings: Sequence[Sequence[float]],
        incoming: Mapping[str, str] | None = None,
        now: Any = None,
    ) -> UpsertReport:
        """把一份文档的片段**增量**写入（本模块最核心的一次事务）。

        调用约定（配合 `change_plan()` 的两段式）：

        * `chunks` / `embeddings` 是**需要写入的片段**（新增 + 内容变化），
          不含「内容没变」的那些 —— 调用方已经用 `change_plan()` 过滤过，
          `ingest.py` 因此**不会为未变的片段花 embedding 钱**。
        * `incoming` 是**本次解析出的全部片段**的 `chunk_id → content_hash`（含未变的）。
          它是「旧片段是否消失」的判据：不在 `incoming` 里的活跃片段会被标失效。
          默认 `None` = 只有 `chunks` 那几个（等价于「这份文档只剩这些片段」）。

        增量规则（`10.rag.md` 第 4 节第 3 条 + 第 5 节的「失效」处理）：

        | 情况 | 处理 |
        |---|---|
        | 片段内容没变（`change_plan` 已过滤，本次不在 `chunks` 里） | **一动不动**（不重嵌、不重写） |
        | 片段内容变了（同 `chunk_id`、新 `content_hash`） | 旧行标 `is_active=false`，插入新行 `version+1` |
        | 新片段 | 直接插入 |
        | 旧片段消失（不在 `incoming` 里） | 标 `is_active=false`（**不删**：可能被引用） |

        为什么不直接 `DELETE` 旧片段：`10.rag.md` 第 5 节明确要求「旧 chunk 不能直接删
        （可能被引用）」—— 会话历史/审计日志里存的是 `chunk_id`，
        删掉之后那条引用就永久断了（前端点「查看原文」会 404）。保留 + `is_active=false`
        让「引用可查、检索不可见」两件事同时成立。
        """
        if len(chunks) != len(embeddings):
            raise StoreError(
                f"片段数与向量数不一致：{len(chunks)} vs {len(embeddings)}。"
                "两者错位会让向量与文本**静默错配**（检索回来的文本不是它自己的向量），"
                "所以宁可直接失败（第 5 步的 embed() 也是这个口径）。"
            )
        # 维度先自检（本仓实测踩到）：表是 `vector(1024)` 时写 8 维向量，
        # PostgreSQL 报的是 `expected 1024 dimensions, not 8` —— 一条离「该改哪三处」
        # 很远的错。在写库前就地拦住，并把配置项名字写进消息。
        if self.dimension:
            for embedding in embeddings:
                if len(embedding) != self.dimension:
                    raise StoreError(
                        f"向量维度不符：本 store 按 {self.dimension} 维配置，收到 {len(embedding)} 维。"
                        "请把 EMBEDDING_DIM / RAG_VECTOR_DIM 与向量表的 vector(n) 统一（三处一起改），"
                        "然后重建索引 + 重灌全部向量；否则会出现「维度相同但向量空间不相容」的静默错误。"
                    )

        incoming_map = dict(incoming) if incoming is not None else {chunk.chunk_id: chunk.content_hash for chunk in chunks}
        total_chunks = len(incoming_map)
        report = UpsertReport(chunks_total=total_chunks)
        with self.engine.begin() as conn:
            existing_doc = conn.execute(
                text(
                    f"select doc_id, doc_hash, version from {self.documents_table} "
                    "where source = :source"
                ),
                {"source": source},
            ).one_or_none()

            if existing_doc is None:
                report.doc_id = int(
                    conn.execute(
                        text(
                            f"insert into {self.documents_table} "
                            "(source, category, doc_hash, chunk_strategy, chunk_count, is_active, updated_at) "
                            "values (:source, :category, :doc_hash, :strategy, :count, true, "
                            "coalesce(:now, now())) returning doc_id"
                        ),
                        {
                            "source": source,
                            "category": category,
                            "doc_hash": doc_hash,
                            "strategy": strategy,
                            "count": total_chunks,
                            "now": now,
                        },
                    ).scalar_one()
                )
                report.version = 1
                report.doc_changed = True
            else:
                report.doc_id = int(existing_doc.doc_id)
                report.doc_changed = existing_doc.doc_hash != doc_hash
                report.version = int(existing_doc.version) + (1 if report.doc_changed else 0)
                conn.execute(
                    text(
                        f"update {self.documents_table} set category = :category, "
                        "doc_hash = :doc_hash, chunk_strategy = :strategy, chunk_count = :count, "
                        "version = :version, is_active = true, updated_at = coalesce(:now, now()) "
                        "where doc_id = :doc_id"
                    ),
                    {
                        "doc_id": report.doc_id,
                        "category": category,
                        "doc_hash": doc_hash,
                        "strategy": strategy,
                        "count": total_chunks,
                        "version": report.version,
                        "now": now,
                    },
                )

            active_rows = conn.execute(
                text(
                    f"select chunk_id, version from {self.chunks_table} "
                    "where doc_id = :doc_id and is_active"
                ),
                {"doc_id": report.doc_id},
            ).all()
            version_by_chunk = {str(row.chunk_id): int(row.version) for row in active_rows}
            # 旧片段消失 = 活跃但不在本次 incoming 里（文档里被删掉的那部分）
            vanished = [chunk_id for chunk_id in version_by_chunk if chunk_id not in incoming_map]

            for chunk, embedding in zip(chunks, embeddings, strict=True):
                previous_version = version_by_chunk.get(chunk.chunk_id)
                version = 1 if previous_version is None else previous_version + 1
                if previous_version is not None:
                    # 内容变了：旧行失效（保留可查），新行插入
                    conn.execute(
                        text(
                            f"update {self.chunks_table} set is_active = false, "
                            "updated_at = coalesce(:now, now()) "
                            "where doc_id = :doc_id and chunk_id = :chunk_id and is_active"
                        ),
                        {"doc_id": report.doc_id, "chunk_id": chunk.chunk_id, "now": now},
                    )
                    report.updated += 1
                else:
                    report.added += 1

                conn.execute(
                    text(
                        f"insert into {self.chunks_table} "
                        "(doc_id, chunk_id, version, text, embedding, ngrams, ngram_tokens, source, "
                        " section, page, category, content_hash, is_active, updated_at) "
                        "values (:doc_id, :chunk_id, :version, :text, :embedding, :ngrams, "
                        "        cast(:ngram_tokens as jsonb), :source, :section, :page, :category, "
                        "        :content_hash, true, coalesce(:now, now()))"
                    ),
                    {
                        "doc_id": report.doc_id,
                        "chunk_id": chunk.chunk_id,
                        "version": version,
                        "text": chunk.text,
                        "embedding": _vector_literal(embedding),
                        "ngrams": index_text(chunk.text),
                        "ngram_tokens": _json_literal(index_tokens_for(chunk.text)),
                        "source": source,
                        "section": chunk.section,
                        "page": chunk.page,
                        "category": category,
                        "content_hash": chunk.content_hash,
                        "now": now,
                    },
                )

            report.unchanged = max(0, total_chunks - report.added - report.updated)

            # ① 消失的片段标失效（在插入之后做：中间失败会整体回滚，不存在"半失效"状态）
            if vanished:
                report.deactivated = len(vanished)
                conn.execute(
                    text(
                        f"update {self.chunks_table} set is_active = false, updated_at = coalesce(:now, now()) "
                        "where doc_id = :doc_id and is_active and chunk_id = any(:ids)"
                    ),
                    {"doc_id": report.doc_id, "ids": vanished, "now": now},
                )

            # ② 文档级片段指纹回填（下次同步靠它判「要不要重嵌」）。
            #    直接写 `incoming_map`（而不是从库里再读一遍）：它就是**本次解析的真相**，
            #    从库里读回来只会读到「刚写进去的那些」，未变的片段反而漏掉。
            conn.execute(
                text(
                    f"update {self.documents_table} set chunk_hashes = cast(:hashes as jsonb) "
                    "where doc_id = :doc_id"
                ),
                {"doc_id": report.doc_id, "hashes": _json_literal(incoming_map)},
            )

            # ③ 可选：回收更老的历史版本（默认保留 1 个失效版本以便回滚/对比）
            report.purged = self._purge_old_versions(conn, report.doc_id, now=now)
            self.refresh_stats(conn)

        return report

    def _active_hashes(self, conn: Any, doc_id: int) -> dict[str, str]:
        """当前活跃片段的 `chunk_id → content_hash`（排查用；写路径不再需要它）。"""
        rows = conn.execute(
            text(
                f"select chunk_id, content_hash from {self.chunks_table} "
                "where doc_id = :doc_id and is_active"
            ),
            {"doc_id": doc_id},
        ).all()
        return {str(row.chunk_id): str(row.content_hash) for row in rows}

    def _purge_old_versions(self, conn: Any, doc_id: int, *, keep: int = 1, now: Any = None) -> int:
        """每个 `chunk_id` 只保留最近 `keep` 个失效版本，更老的删掉（防表无限增长）。

        为什么「保留 1 个」而不是全留：全留的表会随每次编辑线性增长，
        而真正需要回滚的通常只有上一版。删掉更老的版本不影响溯源 ——
        当前活跃版本与上一版都还在，`chunk_id` 的引用断不了。
        """
        result = conn.execute(
            text(
                f"""
                with ranked as (
                    select id, row_number() over (
                               partition by chunk_id order by version desc, id desc
                           ) as rn
                    from {self.chunks_table}
                    where doc_id = :doc_id and not is_active
                )
                delete from {self.chunks_table}
                where id in (select id from ranked where rn > :keep)
                """
            ),
            {"doc_id": doc_id, "keep": keep},
        )
        return int(result.rowcount or 0)

    def refresh_stats(self, conn: Any | None = None) -> dict[str, int]:
        """重算 BM25 的两个全局量（N / avgdl）与片段总数。

        单独一张单行表而不是每次检索现算：现算要扫全表（`avg(length(ngrams))`），
        而它**每条查询都要用**。维护成本是每次入库一次 upsert —— 入库本来就是低频的。
        """
        statement = text(
            f"""
            insert into {self.stats_table} (singleton, docs, chunks, total_tokens, updated_at)
            select true,
                   (select count(*) from {self.documents_table} where is_active),
                   (select count(*) from {self.chunks_table} where is_active),
                   (select coalesce(sum(ngram_doc), 0) from {self.chunks_table} where is_active),
                   now()
            on conflict (singleton) do update set
                docs = excluded.docs,
                chunks = excluded.chunks,
                total_tokens = excluded.total_tokens,
                updated_at = now()
            """
        )
        if conn is not None:
            conn.execute(statement)
            return {}
        with self.engine.begin() as own_conn:
            own_conn.execute(statement)
        return self.corpus_stats()

    def corpus_stats(self) -> dict[str, int]:
        """读语料统计（N / avgdl / docs）。表为空时返回零值。"""
        with self.read_engine.connect() as conn:
            row = conn.execute(
                text(
                    f"select docs, chunks, total_tokens from {self.stats_table} "
                    "where singleton = true"
                )
            ).one_or_none()
        if row is None:
            return {"docs": 0, "chunks": 0, "total_tokens": 0}
        return {"docs": int(row.docs), "chunks": int(row.chunks), "total_tokens": int(row.total_tokens)}

    def delete_document(self, source: str, *, hard: bool = False, now: Any = None) -> dict[str, int]:
        """下线一份文档。

        默认**软删**（`is_active=false`）：与片段失效同一口径 —— 引用还在，检索不再可见。
        `hard=True` 才真的删（连同片段，靠外键 cascade）—— 只在「误灌了不该存在的文档」时用。
        """
        with self.engine.begin() as conn:
            row = conn.execute(
                text(f"select doc_id, version from {self.documents_table} where source = :source"),
                {"source": source},
            ).one_or_none()
            if row is None:
                return {"documents": 0, "chunks": 0}

            doc_id = int(row.doc_id)
            if hard:
                count = conn.execute(
                    text(f"select count(*) from {self.chunks_table} where doc_id = :doc_id"),
                    {"doc_id": doc_id},
                ).scalar_one()
                conn.execute(
                    text(f"delete from {self.documents_table} where doc_id = :doc_id"),
                    {"doc_id": doc_id},
                )
            else:
                # 先给片段打失效标记，再把文档本身置为无效：
                # 顺序反了的话「碎片还在飞、文档已下线」会有一个不可观测的中间态；
                # 事务其实保证了原子性，这个顺序的价值是让日志/断点更容易读。
                count = conn.execute(
                    text(
                        f"update {self.chunks_table} set is_active = false, "
                        "updated_at = coalesce(:now, now()) "
                        "where doc_id = :doc_id and is_active"
                    ),
                    {"doc_id": doc_id, "now": now},
                ).rowcount
                conn.execute(
                    text(
                        f"update {self.documents_table} set is_active = false, version = version + 1, "
                        "updated_at = coalesce(:now, now()) where doc_id = :doc_id"
                    ),
                    {"doc_id": doc_id, "now": now},
                )
            self.refresh_stats(conn)
        return {"documents": 1, "chunks": int(count or 0)}

    def document_state(self, source: str) -> dict[str, Any] | None:
        """读一份文档的元数据（同步时用来判「要不要重新解析」）。"""
        with self.read_engine.connect() as conn:
            row = conn.execute(
                text(
                    f"select doc_id, source, category, doc_hash, version, chunk_count, is_active, "
                    f"chunk_hashes, updated_at from {self.documents_table} where source = :source"
                ),
                {"source": source},
            ).one_or_none()
        return dict(row._mapping) if row is not None else None

    # ── 读：向量腿 ──────────────────────────────────────────────────────────
    def search_vector(self, embedding: Sequence[float], *, top_k: int) -> list[VectorHit]:
        """向量腿：pgvector 的余弦距离 ANN（走 HNSW 索引）。

        `1 - (embedding <=> query)` 是余弦相似度（pgvector 的 `<=>` 是余弦**距离**）。
        排序用距离升序（等价于相似度降序），这样 `order by` 能直接吃 HNSW 索引 ——
        写成 `order by 1 - (.. <=> ..) desc` 在某些计划下会退化成全表扫描。
        """
        if not embedding:
            return []
        with self.read_engine.connect() as conn:
            try:
                rows = conn.execute(
                    text(
                        f"""
                        with candidates as (
                            select id, source, chunk_id, text, section, page, category, updated_at,
                                   embedding <=> cast(:query as vector) as distance
                            from {self.chunks_table}
                            where is_active
                            order by embedding <=> cast(:query as vector)
                            limit :top_k
                        )
                        select id, source, chunk_id, text, section, page, category, updated_at,
                               1 - distance as score
                        from candidates
                        order by distance
                        """
                    ),
                    {"query": _vector_literal(embedding), "top_k": int(top_k)},
                ).all()
            except SQLAlchemyError as exc:
                raise self._check_error(exc) from exc

        return [
            VectorHit(
                id=int(row.id),
                source=row.source,
                chunk_id=row.chunk_id,
                text=row.text,
                section=row.section,
                page=row.page,
                score=float(row.score),
                updated_at=row.updated_at,
                category=row.category,
            )
            for row in rows
        ]

    # ── 读：关键词腿 ────────────────────────────────────────────────────────
    def known_terms(self, candidates: Sequence[str]) -> list[str]:
        """过滤出「库里真实存在」的词元（覆盖率的分母要对它算）。

        为什么必须做：用户打错一个字时，那个字在库里根本不存在，
        它会把覆盖率分母抬高，于是**本来能召回的文档被门槛挡掉**。
        每个词一次 `@@`（GIN 索引），词表上限 40 → 最多 40 次索引扫描，实测 2~6ms。
        """
        terms = list(dict.fromkeys(candidates))[:MAX_DF_TERMS]
        if not terms:
            return []
        with self.read_engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    select lexeme from unnest(cast(:terms as text[])) as lexeme
                    where exists (
                        select 1 from {self.chunks_table}
                        where is_active and ngram_tsv @@ to_tsquery('simple', lexeme)
                    )
                    """
                ),
                {"terms": terms},
            ).all()
        return [row.lexeme for row in rows]

    def _document_frequencies(self, terms: Sequence[str]) -> dict[str, int]:
        """每个词的 df（含它的文档数）—— 每个词一次 GIN 索引扫描。"""
        if not terms:
            return {}
        with self.read_engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    select q.lexeme, count(*) as df
                    from unnest(cast(:terms as text[])) as q(lexeme)
                    join {self.chunks_table} c
                      on c.is_active and c.ngram_tsv @@ to_tsquery('simple', q.lexeme)
                    group by q.lexeme
                    """
                ),
                {"terms": list(terms)},
            ).all()
        return {row.lexeme: int(row.df) for row in rows}

    def search_keyword(
        self,
        query: str,
        *,
        top_k: int,
        min_ratio: float = 0.6,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> list[KeywordHit]:
        """关键词腿：n-gram 覆盖率候选门 + **SQL 内手算 BM25**。

        为什么要分两步（候选门 → 打分）而不是一条 `WHERE ... @@ to_tsquery(...)`：
        打分要遍历候选行的 tsvector 取 tf（`ts_stat` 每行一次函数调用），
        先用门把候选压到几十行，打分才便宜。
        """
        primary, fallback = query_terms(query)
        if not primary:
            primary, fallback = fallback, fallback
        if not primary:
            return []

        known = self.known_terms(primary)
        if not known:
            # 主词表一个都不在库里（错字/口语化）：退到全部词元（含单字）救一次召回。
            known = self.known_terms(fallback)
            if not known:
                return []

        # 门槛：>= ceil(ratio * 已知词数)，最少 1 个。
        # 用整数运算避免浮点误差把 0.6 * 5 = 2.9999 算成 2（那会让门槛松一档）。
        scaled = int(min_ratio * 1000)
        known_count = len(known)
        required = max(1, -(-known_count * scaled // 1000))

        df = self._document_frequencies(known)
        stats = self.corpus_stats()
        total_docs = max(1, stats["chunks"])
        average_length = max(1.0, stats["total_tokens"] / total_docs) if stats["total_tokens"] else 1.0

        scored: list[KeywordHit] = []
        with self.read_engine.connect() as conn:
            try:
                rows = conn.execute(
                    text(
                        f"""
                        with q as (
                            select lexeme, df, ln(1 + (:n - df + 0.5) / (df + 0.5)) as idf
                            from unnest(cast(:terms as text[]), cast(:dfs as float8[]))
                                 as t(lexeme, df)
                        ),
                        matched as (
                            select c.id, c.source, c.chunk_id, c.text, c.section, c.page, c.updated_at,
                                   q.idf,
                                   st.nentry::float8 as tf,
                                   c.ngram_doc::float8 as dl
                            from q
                            join {self.chunks_table} c
                              on c.is_active and c.ngram_tsv @@ to_tsquery('simple', q.lexeme)
                            cross join lateral ts_stat(
                                'select ngram_tsv from {self.chunks_table} where id = ' || c.id
                            ) st
                            where st.word = q.lexeme
                        )
                        select id, source, chunk_id, text, section, page, updated_at,
                               count(*) as matched,
                               sum(idf * (tf * (:k1 + 1)) / (tf + :k1 * (1 - :b + :b * dl / :avgdl))) as score
                        from matched
                        group by id, source, chunk_id, text, section, page, updated_at
                        having count(*) >= :required
                        order by score desc
                        limit :top_k
                        """
                    ),
                    {
                        "terms": list(df),
                        "dfs": [float(value) for value in df.values()],
                        "n": float(total_docs),
                        "k1": float(k1),
                        "b": float(b),
                        "avgdl": float(average_length),
                        "required": int(required),
                        "top_k": int(top_k),
                    },
                ).all()
            except SQLAlchemyError as exc:
                raise self._check_error(exc) from exc

        for row in rows:
            scored.append(
                KeywordHit(
                    id=int(row.id),
                    source=row.source,
                    chunk_id=row.chunk_id,
                    text=row.text,
                    section=row.section,
                    page=row.page,
                    score=float(row.score),
                    matched=int(row.matched),
                    updated_at=row.updated_at,
                )
            )
        logger.debug(
            "关键词腿：query=%r 主词=%d 已知=%d 门槛=%d 命中=%d",
            query[:30],
            len(primary),
            len(known),
            required,
            len(scored),
        )
        return scored

    def fetch(self, ids: Iterable[int]) -> list[KeywordHit]:
        """按 id 取片段（融合后补全字段用；也用于测试与排障）。"""
        id_list = [int(value) for value in ids]
        if not id_list:
            return []
        with self.read_engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"select id, source, chunk_id, text, section, page, updated_at "
                    f"from {self.chunks_table} where id = any(:ids)"
                ),
                {"ids": id_list},
            ).all()
        return [
            KeywordHit(
                id=int(row.id),
                source=row.source,
                chunk_id=row.chunk_id,
                text=row.text,
                section=row.section,
                page=row.page,
                score=0.0,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    def count_active_chunks(self) -> int:
        """活跃片段数（自检与演示脚本用）。"""
        with self.read_engine.connect() as conn:
            return int(
                conn.execute(
                    text(f"select count(*) from {self.chunks_table} where is_active")
                ).scalar_one()
            )

    def health(self) -> dict[str, Any]:
        """库层自检：表在不在、建表维度与配置维度是否一致（`/health` 与演示脚本的输入）。"""
        from app.rag.schema import current_vector_dim, table_exists

        with self.read_engine.connect() as conn:
            exists = table_exists(conn, self.chunks_table)
            actual_dim = current_vector_dim(conn, self.chunks_table) if exists else None
        return {
            "table": self.chunks_table,
            "exists": exists,
            "dim": actual_dim,
            "expected_dim": self.dimension,
            "dim_ok": True if not self.dimension or actual_dim is None else actual_dim == self.dimension,
        }


# ── 小工具：字面量构造（都在这里，避免 SQL 拼接散落各处）──────────────────────

def _vector_literal(embedding: Sequence[float]) -> str:
    """向量 → pgvector 的字面量 `[1,2,3]`。

    为什么不用绑定参数 + `:query::vector`：SQLAlchemy 会把列表当成 `IN (...)` 展开，
    于是「一个 1024 维向量」变成「1024 个参数」，报错信息极难读（实测踩过）。
    代价是要自己保证元素是数字 —— 所以这里显式 `float()` 一遍，非法值当场报错。
    """
    values = ", ".join(repr(float(value)) for value in embedding)
    return f"[{values}]"


def _json_literal(value: Any) -> str:
    """JSON 字面量（`jsonb` 列用 `cast(:x as jsonb)` 配合它）。"""
    import json

    return json.dumps(value, ensure_ascii=False)


def index_tokens_for(text_value: str) -> list[str]:
    """片段入库时保留的词表（排查「为什么这个词搜不到」时要看它）。

    存下词表而不是每次用 `tsvector` 反推：`tsvector` 里的 lexeme 是**归一化后**的，
    且 1-gram 与 2-gram 混在一起（`无` 与 `无理` 都在），排查时想把它们分开看是没有依据的。
    `jsonb` 多存一份的代价（约等于词表文本的长度）换「可解释」，值。
    """
    return index_tokens(text_value)


def hits_by_id(hits: Iterable[Any]) -> Mapping[int, Any]:
    """按 id 建索引（融合时把两路命中归一）。"""
    return {int(hit.id): hit for hit in hits}


__all__ = [
    "MAX_DF_TERMS",
    "KeywordHit",
    "RagStore",
    "StoreError",
    "UpsertReport",
    "VectorHit",
    "hits_by_id",
    "index_tokens_for",
]
