"""失效策略（`13.cache.md` §5 的**三策略**在这里收成三条可调用的动作）。

| 策略 | 函数 | 谁调 | 特点 |
|---|---|---|---|
| ① 时间过期（TTL） | 每条条目自己的 `ttl_seconds`（写入时定） | 时间本身 | 无需动作，`judge` 每次查询都判 |
| ② 主动失效（数据变更时删 key） | `invalidate_document` / `invalidate_scope` / `purge_expired` | 入库、人工、cron | **精确**，但要知道该删哪一条 |
| ③ 版本失效（知识库/prompt 变了全局失效） | `bump_after_ingest`（`corpus.py`） | `app/rag/ingest.py` | 全局、可回滚、不需知道细节 |

## 为什么②与③都要有（而不是只留一个）

它们的**代价与精度**正好互补：

* ③ 精度低（一改文档就整批失效）但**绝不会漏** —— 它不依赖「谁知道该删哪条」。
  所以它是**兜底**：入库路径只要调它，就不可能出现「知识库更新了、缓存还在讲老政策」。
* ② 精度高但**依赖调用方知道详情**（「这条缓存引用了哪份文档」）。
  它的用途是那些③覆盖不到的场景：
  * 人工发现某条答案是错的 → `invalidate_document`（按问题精确删一条）；
  * 用户注销 / 数据主体删除请求 → `invalidate_scope`（把某个 scope 全清）；
  * 定时回收 → `purge_expired`。

## 一条纪律：**失效动作要有返回值**

三个函数都返回「删了几条」。理由是运维时最先问的问题就是「到底删掉了没有」——
一个返回 `None` 的失效函数，在「缓存还是旧的」这一刻无法回答自己是否生效
（而它最可能的失败原因是**没生效**：scope 拼错了、指纹对不上）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.cache.corpus import RevisionStore, bump_after_ingest
from app.cache.stores import EntryStore, ScopeContext, scope_for

logger = logging.getLogger(__name__)


def invalidate_document(
    store: EntryStore,
    *,
    scope: str,
    question: str,
    fingerprint: str = "",
) -> int:
    """按「scope + 问题」精确失效一条（`13.cache.md` §7 的 `test_invalidate_on_change` 用它）。

    ⚠️ `fingerprint` 默认为空串是**有意**的：`deactivate` 在存储层的实现里
    只按 `(scope, fingerprint, question_hash)` 定位，而传空串就等于「谁来都删」——
    对内存与 Postgres 实现来说，这意味着它删的是「这个 scope 下这句话」，
    跨版本也一起删（这正是「主动失效」想要的语义：**不再需要它了**）。

    返回真正被删掉的条数（0 = 那条本来就不在，或者 scope/问题拼错了）。
    """
    context = ScopeContext(scope=scope, fingerprint=fingerprint)
    try:
        removed = int(bool(store.deactivate(context, question)))
    except Exception as exc:  # noqa: BLE001 - 失效失败要报出来，但不该让调用方崩
        logger.warning("按问题失效缓存失败：%s: %s", type(exc).__name__, exc)
        return 0
    logger.info("缓存主动失效：scope=%s question=%r → 删掉 %d 条", scope, question[:30], removed)
    return removed


def invalidate_scope(store: EntryStore, *, user_id: int, role: str) -> int:
    """把一个身份的缓存整批失效（数据主体删除请求、越权修复的落点）。"""
    scope = scope_for(user_id, role)
    try:
        removed = int(store.deactivate_scope(scope) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("按 scope 失效缓存失败：%s: %s", type(exc).__name__, exc)
        return 0
    logger.info("缓存主动失效：scope=%s → 删掉 %d 条", scope, removed)
    return removed


def purge_expired(store: EntryStore, *, limit: int | None = None) -> int:
    """回收过期条目（定时任务；`cleanup_stale` 是它在演示脚本里的名字）。

    「过期条目为什么还要删」：它们已经不会被命中了（`judge` 判过期），
    但**行还在**（版本失效靠的就是「旧行留着」）。所以回收是一个纯存储动作，
    频率低、可中断、不影响正确性 —— 这正是它适合放在 cron 里的原因。
    """
    try:
        removed = int(store.purge(limit=limit, only_expired=True) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("回收过期缓存失败：%s: %s", type(exc).__name__, exc)
        return 0
    if removed:
        logger.info("回收过期缓存 %d 条", removed)
    return removed


def invalidate_on_kb_change(
    *,
    source: str = "",
    document_version: int | None = None,
    revisions: RevisionStore | None = None,
    store: EntryStore | None = None,
) -> dict[str, Any]:
    """知识库变更的统一入口（**版本失效 + 可选的行级回收**）。

    为什么合成一个函数：入库路径只有一个调用点（`app/rag/ingest.py` 的 `on_change`），
    而它要做的事不止「+1 版本」—— 顺手回收一批过期行能让表不随版本增长无界膨胀。
    两者都**不该**让入库失败（入库成功了、缓存回收失败不是一回事），所以整段吞异常。
    """
    outcome: dict[str, Any] = {"source": source, "revision": 0, "purged": 0}
    try:
        outcome["revision"] = bump_after_ingest(
            source=source, document_version=document_version, store=revisions
        )
    except Exception as exc:  # noqa: BLE001 - 缓存侧的问题不该让入库失败
        logger.warning("推进知识库版本失败（缓存将只靠 TTL 失效）：%s: %s", type(exc).__name__, exc)
        return outcome
    if store is not None:
        outcome["purged"] = purge_expired(store, limit=200)
    return outcome


def make_ingest_callback(
    *,
    store: EntryStore | None = None,
    revisions: RevisionStore | None = None,
    purge_limit: int = 0,
) -> Any:
    """造一个给 `app/rag/ingest.py::ingest_document(on_change=...)` 用的回调。

    为什么回调收的是**报告对象**而不是几个数字（见 `ingest_document` 的说明）：
    报告是「这次入库的结论」，让回调自己从里面取 `source` / `version`，
    就不可能出现「调用方判错了算不算变」的第二处判断。

    `purge_limit=0`（默认）表示**不顺手回收**：回收是一个运维动作，
    它的频率该由 cron 决定，而不是由「恰好有人灌了一份文档」决定。

    ## ⚠️ `revisions=None` 的含义是「**进程默认的那一个**」，不是「一个临时的」

    第一版在 `revisions is None` 时调了 `bump_after_ingest(store=None)`，
    而那条路径会**懒建一个进程默认版本表**；读路径（`cache.lookup`）在读不到显式
    inject 的 store 时也走同一个默认 —— 看起来两边一致，但如果调用方
    **先**用默认读、**后**在别处换了默认（或反过来），两次就落在不同的表上，
    症状是「推了版本但缓存没失效」（本仓实测踩到：演示脚本里回调解到的是一份
    进程内的内存版本表，而读路径读的是 Postgres 的版本表）。
    现在这里**显式解析成同一个对象**：没有显式 `revisions` 就用
    `corpus` 模块的默认实现，不另造一份。
    """
    from app.cache.corpus import bump_after_ingest, default_revision_store

    active_revisions = revisions if revisions is not None else default_revision_store()

    def on_change(report: Any) -> dict[str, Any]:
        outcome: dict[str, Any] = {
            "source": str(getattr(report, "source", "")),
            "revision": 0,
            "purged": 0,
        }
        outcome["revision"] = bump_after_ingest(
            source=outcome["source"],
            document_version=int(getattr(report, "version", 0) or 0) or None,
            store=active_revisions,
        )
        if store is not None and purge_limit > 0:
            outcome["purged"] = purge_expired(store, limit=int(purge_limit))
        return outcome

    return on_change


#: 别名：`13.cache.md` §6 第 3 步的措辞是「知识库/数据变更时主动删 key」，
#: 演示脚本与文档里叫 `invalidate_knowledge_base` 更贴题。两个名字指向同一个实现。
invalidate_knowledge_base = invalidate_on_kb_change


__all__ = [
    "invalidate_document",
    "invalidate_knowledge_base",
    "invalidate_on_kb_change",
    "invalidate_scope",
    "make_ingest_callback",
    "purge_expired",
]
