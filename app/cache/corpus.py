"""知识库版本（**第三级失效：版本失效**）的唯一真相。

`13.cache.md` §5 列了三种失效策略，本模块落的是第三条：

| 策略 | 落点 | 触发者 |
|---|---|---|
| 时间过期（TTL） | `stores.CacheEntry.is_expired` | 时间本身 |
| 主动失效（数据变更时删 key） | `invalidate.py` 的 `invalidate_document` / `purge_expired` | 人工 / 定时 |
| **版本失效（知识库/prompt 变了全局失效）** | **本模块** | `app/rag/ingest.py` 每次真正改动时 |

## 为什么是「一个单调递增的整数」而不是「删掉所有缓存行」

版本失效有两种实现方式，本仓选后者：

* **删行**（`DELETE FROM cache_semantic_entries`）—— 一行代码，但它有三个问题：
  ① 删完就**没有证据**了（「缓存被清了」这件事在库里查不到，只剩日志）；
  ② 删是一个**不可回滚**的动作（发现知识库更新错了，缓存也回不来了）；
  ③ 一次全表删除会把缓存命中率打出一个明显的大坑，而「为什么掉」要看很久。
* **递增版本号**（本实现）—— 旧行留着（`is_active=true`、TTL 也还在），
  只是 `fingerprint` 里的版本变了，于是 `core.judge` 一眼就判「失效」。
  代价是**存储会多留一份旧版本**，靠 `purge_expired` 或 TTL 自然回收 ——
  这笔交换换来「可解释、可回滚、命中率曲线连续」。

⚠️ 「全局失效」在本实现里是**精确到知识库版本**的：所有问题的缓存一起失效，
而不是「只有那些引用了被改文档的问题失效」。这是刻意的保守口径 ——
「哪条缓存引用了哪份文档」需要缓存里存文档清单并在每次查询时逐个比对，
而它的收益（少清几条）远小于它的风险（漏清一条就是旧答案出网）。
第 10 步的 `rag_documents.version` 已经记着「哪份文档改过」，
所以将来要做**按文档失效**，接口不需要变（`fingerprint_for` 换成按 source 取版本即可）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: 未知版本（读不到库时用它）。`core.judge` 把 `revision=0` 当作「不知道」，
#: 那时只靠 TTL 兜底 —— **不能**当作「版本不匹配」：一次读库失败会把整份缓存清空。
UNKNOWN_REVISION = 0


class RevisionStore(Protocol):
    """版本表的读写（内存与 Postgres 两套实现）。"""

    def current(self) -> int: ...

    def bump(self, *, by: str = "", version: int | None = None) -> int: ...

    def health(self) -> dict[str, Any]: ...


@dataclass
class MemoryRevisionStore:
    """进程内存里的版本（离线用例与演示用）。

    ⚠️ 它在**多副本部署下是错的**（每个副本各有一份版本，于是「知识库更新了」
    只对收到那个请求的副本生效）。所以它只配当测试装置与降级回落，
    生产版本必须是 `PostgresRevisionStore`。
    """

    revision: int = 1
    bumped_by: str = "init"
    bumps: int = 0

    def current(self) -> int:
        return int(self.revision)

    def bump(self, *, by: str = "", version: int | None = None) -> int:
        """无条件 +1（与 `PostgresRevisionStore.bump` 同语义，见那里的说明）。

        ⚠️ 旧版也有「version 与当前版本相同就不动」的短路 —— 那是 P1-1 的 bug，
        `version` 与 `revision` 量纲不同，不能比较。现在只用于日志。
        """
        del version  # 仅供调用方标注「哪一版文档触发的」，不参与判断
        self.revision = int(self.revision) + 1
        self.bumps += 1
        self.bumped_by = str(by or "manual")
        return int(self.revision)

    def health(self) -> dict[str, Any]:
        return {"store": "memory", "revision": self.revision, "bumps": self.bumps, "bumped_by": self.bumped_by}


class PostgresRevisionStore:
    """`cache_corpus_versions` 单行表的实现（生产路径）。

    `bump` 用一条 `UPDATE ... RETURNING` 完成「读 + 写」——
    两个并发的入库请求各自 `+1`，得到两个不同的版本号（而不是都读到 2 再都写 3）。
    这一点在「入库是并发的」这个现实下很重要：版本号**必须单调**，
    否则一次更早的入库可能拿到一个更小的版本，而缓存会以为「没变」。
    """

    def __init__(self, *, engine: Any = None, table: str = "cache_corpus_versions") -> None:
        self._engine = engine
        self.table = table

    @property
    def engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        from app.db.session import get_write_engine

        return get_write_engine()

    def current(self) -> int:
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        try:
            with self.engine.connect() as conn:
                value = conn.execute(
                    text(f"select corpus_revision from {self.table} where singleton = true")
                ).scalar()
        except SQLAlchemyError as exc:
            # 读不到版本**不是**致命错误：缓存仍然工作，只是「版本失效」这道闸降级为
            # 「只靠 TTL」。所以要打日志，但绝不能抛 —— 抛了就等于「库抖一下，问答全挂」。
            logger.warning(
                "读知识库版本失败（缓存仍可用，只是版本失效这道闸失效）：%s。"
                "先确认迁移跑过：`uv run alembic upgrade head`",
                str(exc).splitlines()[0],
            )
            return UNKNOWN_REVISION
        return int(value or UNKNOWN_REVISION)

    def bump(self, *, by: str = "", version: int | None = None) -> int:
        """把版本 +1（返回新版本）。**无条件推进**。

        ## ⚠️ 为什么这里曾经有个短路，以及它为什么是错的（issue 003 的 P1-1）

        旧实现有一句「`version` 与当前版本相同就不 bump」，本意是
        「**同一份文档**重复入库不算变化，别把缓存白清一遍」。
        但它比较的两个量**量纲不同**：

        * `version` 是调用方传进来的 `rag_documents.version`（**每份文档各自的**版本号）；
        * `current()` 是全局知识库版本（`cache_corpus_versions.corpus_revision`）。

        于是只要某份文档的版本号**恰好等于**当前的全局版本号，这次真实变更就**不会推进版本**
        —— 首次全量灌库尤其容易撞上（新文档的 version 普遍是 1，而初始
        `corpus_revision` 也是 1）。实测：

        ```text
        corpus_revision = 1
        bump(version=1) -> 1     # 新文档入库，但版本没动 → 缓存不失效
        bump(version=2) -> 2     # 只有碰巧不等时才推进
        ```

        后果是「推了新文档但缓存不失效」：用户在**知识库为空时**问过的问题，
        灌库之后仍然拿到那条无出处的旧答案（实测 `hit_count=10`，答案一直没更新）。
        这类症状极难归因 —— 接口 200、`/health` 全绿、日志无异常，只是答案悄悄不对。

        ## 现在为什么可以无条件推进

        「重复入库不要白清缓存」这件事**已经由上游闸门负责**：
        `app/rag/ingest.py` 只在 `report.doc_changed` 为真时才调 `on_change`
        （`_is_noop()` 已经把「什么都没变」挡在外面）。所以走到这里就意味着
        **确实有变更**，无条件 +1 才是正确语义。

        `version` 参数保留下来**只用于日志**（说清是哪一版文档触发的），
        不再参与任何判断 —— 把两个不同量纲的数字放在一起比较，正是这个 bug 的根源。
        """
        from sqlalchemy import text

        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    f"update {self.table} set corpus_revision = corpus_revision + 1, "
                    "bumped_at = now(), bumped_by = :by where singleton = true "
                    "returning corpus_revision"
                ),
                {"by": str(by or "manual")},
            ).one_or_none()
            if row is None:
                # 表在但那一行没了（人工清过）：补一行再 +1，让调用方仍拿到一个可用的版本号。
                conn.execute(
                    text(
                        f"insert into {self.table} (singleton, corpus_revision, bumped_by) "
                        "values (true, 2, :by) on conflict (singleton) do nothing"
                    ),
                    {"by": str(by or "manual")},
                )
                row = conn.execute(
                    text(f"select corpus_revision from {self.table} where singleton = true")
                ).one()
        new_revision = int(row[0])
        logger.info(
            "知识库版本 → %d（by=%s%s）",
            new_revision,
            by or "manual",
            f", document_version={int(version)}" if version is not None else "",
        )
        return new_revision

    def health(self) -> dict[str, Any]:
        from sqlalchemy import text

        from app.cache.schema import table_exists

        try:
            with self.engine.connect() as conn:
                exists = table_exists(conn, self.table)
                revision = (
                    int(
                        conn.execute(
                            text(f"select corpus_revision from {self.table} where singleton = true")
                        ).scalar()
                        or 0
                    )
                    if exists
                    else 0
                )
        except Exception as exc:  # noqa: BLE001 - 自检不该抛
            return {"store": "postgres", "table": self.table, "error": f"{type(exc).__name__}: {exc}"}
        return {"store": "postgres", "table": self.table, "exists": exists, "revision": revision}


def bump_after_ingest(
    *,
    source: str,
    document_version: int | None = None,
    store: RevisionStore | None = None,
) -> int:
    """入库之后的版本推进（`app/rag/ingest.py` 的 `on_change` 回调就调它）。

    为什么这段逻辑单独一个函数（而不是直接写 `store.bump()`）：
    「入库了什么」→「版本怎么变」这个映射要被**一条用例**钉住，
    而回调是在 RAG 侧被调用的，放一个函数在这里让 RAG 侧只负责「什么时候调」。

    ⚠️ **「什么时候调」由 RAG 侧负责，这里只负责「调了就推进」**：
    `app/rag/ingest.py` 已经用 `report.doc_changed`（`_is_noop()`）把
    「跑了一次同步但什么都没变」挡在外面，所以走到这里就是**确实有变更**。
    本函数**不再**拿 `document_version` 与全局 `corpus_revision` 比较 ——
    那两个量纲不同，比较会让真实变更漏掉（issue 003 的 P1-1，见 `bump` 的说明）。
    """
    active = store or _default_store()
    return active.bump(by=f"ingest:{source}", version=document_version)


# ── 进程默认（懒建：生产用 Postgres，测试可替换）────────────────────────────

_DEFAULT: RevisionStore | None = None


def _default_store() -> RevisionStore:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = PostgresRevisionStore()
    return _DEFAULT


def default_revision_store() -> RevisionStore:
    """取**进程默认**的版本表（生产是 `PostgresRevisionStore`）。

    单独暴露它（而不是让调用方各自 `PostgresRevisionStore()`）是为了让
    「默认」只有一个对象：两处各造一个 store 时，`set_revision_store(...)`
    的替换只对其中一处生效 —— 症状是「推了版本但缓存没失效」
    （本仓实测踩到：演示脚本的回调解到了一份进程内的内存版本表，
    而读路径读的是 Postgres 的那张）。
    """
    return _default_store()


def set_revision_store(store: RevisionStore | None) -> None:
    """替换进程默认版本表（测试与演示用；`None` = 丢弃，下次访问重建）。"""
    global _DEFAULT
    _DEFAULT = store


def current_revision(store: RevisionStore | None = None) -> int:
    """取当前知识库版本（热路径上会调它，所以它必须是**一次主键查询**）。"""
    active = store or _default_store()
    try:
        return int(active.current())
    except Exception as exc:  # noqa: BLE001 - 版本读不到不该让请求失败
        logger.warning("读取知识库版本失败，本次按「未知版本」处理（只靠 TTL 兜底）：%s", exc)
        return UNKNOWN_REVISION


__all__ = [
    "UNKNOWN_REVISION",
    "MemoryRevisionStore",
    "PostgresRevisionStore",
    "RevisionStore",
    "bump_after_ingest",
    "current_revision",
    "default_revision_store",
    "set_revision_store",
]
