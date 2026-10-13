"""知识库检索工具：**只返回带溯源的结果**（`6.tools.md` 第 4 节第 3 条）。

职责边界：本模块是 `rag/`（第 10 步）与上层（agent / MCP）之间的**适配层**。
它不做检索算法（那是 `app/rag/retriever.py::hybrid_search` 的事），只做四件上层需要的事：

1. **统一契约**：把 `rag` 的返回（`list[dict]`，含 `text/source/chunk_id/score`）归一成
   `RetrievedDoc`。上层因此不必知道第 10 步用的是 dict 还是对象、字段叫什么。
2. **强制溯源**：`source` 为空的命中**一律丢弃**并记 WARNING。
   这是「杜绝编造答案」的机制化落点：合成 Agent（第 7 步）回答时附的是 `doc.source`，
   如果这里放行了没有来源的片段，模型就会引用一段**无法核对**的内容 ——
   看起来像有出处，实际是把幻觉洗白了。宁可少给一条，也不给一条查不到出处的。
3. **参数收敛**：`top_k` 有上下限（`1..MAX_TOP_K`），避免调用方（含 LLM）要「Top 500」
   把上下文和成本一起打爆（E2 说的就是要省这个 token）。
4. **审计**：检索次数 / 命中数 / 被丢掉的无源命中数，都进审计。

⚠️ **第 10 步之前 `app/rag/` 还不存在**，此时默认后端会抛 `ToolNotAvailableError`，
消息里写明「属第 10 步、如何绕开」。这不影响本模块被完整测试：测试通过 `search=` 注入
假后端（与 `5.llm-runbook` 里「mock 响应证明代码路径通」是同一个思路）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.observability.instrument import record_retrieval
from app.tools.audit import audit_tool_call
from app.tools.errors import ToolContractError, ToolNotAvailableError

logger = logging.getLogger(__name__)

TOOL_NAME: Final[str] = "retrieve_knowledge"

# top-k 上下限：默认 5（第 10 步 `10.rag.md` 的接口默认值一致），硬上限 20
DEFAULT_TOP_K: Final[int] = 5
MAX_TOP_K: Final[int] = 20

# `rag/` 的模块坐标（第 10 步落地）。写成常量而不是散在报错文本里，方便将来改路径时一处改。
RAG_MODULE: Final[str] = "app.rag.retriever"
RAG_FUNCTION: Final[str] = "hybrid_search"

# 后端适配：入参 (query, top_k) → 命中列表（dict 或带属性的对象都能吃，见 `_to_doc`）
Searcher = Callable[..., Sequence[Any]]


@dataclass(frozen=True)
class RetrievedDoc:
    """一条带溯源的检索结果（`6.tools.md` 第 2 节要求的 `(文本, 来源文档, 页码/片段)`）。"""

    text: str
    source: str  # 来源文档（文件/政策名）——**永不为空**，空的一条进不来（见 `retrieve_docs`）
    chunk_id: str | None = None
    score: float = 0.0
    page: int | None = None
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def citation(self) -> str:
        """人可读的出处串（合成 Agent 直接拼进回答：`退货政策 p.3#c12`）。"""
        parts = [self.source]
        if self.page is not None:
            parts.append(f"p.{self.page}")
        if self.chunk_id:
            parts.append(f"#{self.chunk_id}")
        return " ".join(parts)


def _default_searcher() -> Searcher:
    """解析 `rag/` 的检索函数；未落地时抛出**能照着修**的报错。

    为什么在函数内 import（与 `app/llm/client.py` 里 `sentence_transformers` 同一理由）：
    顶层 import 会让「第 10 步还没做」变成「整个 tools 包 import 失败」，
    于是连同 `sql_executor` 一起用不了。

    为什么不让 `rag` 的缺失静默降级成「空结果」：空结果会被上层理解成
    「知识库里没有这条」，而真相是「检索能力根本还没接上」——
    这两种情况的处置完全不同（前者该据实回答，后者该报错/降级到别的路径）。
    """
    try:
        import importlib

        module = importlib.import_module(RAG_MODULE)
    except ImportError as exc:  # pragma: no cover - 第 10 步落地后此分支自然消失
        raise ToolNotAvailableError(
            f"知识库检索后端尚未就绪：{RAG_MODULE} 不存在（属第 10 步 rag 的交付物）。"
            "当前可用做法：① 传 search=<自定义检索函数> 注入后端（测试/临时实现）；"
            "② 直接回答「知识库检索暂不可用」；③ 属数据类问题请走 sql_query 工具。"
        ) from exc

    searcher = getattr(module, RAG_FUNCTION, None)
    if searcher is None:
        raise ToolNotAvailableError(
            f"{RAG_MODULE} 存在但没有 {RAG_FUNCTION}()：接口名对不上（10.rag.md 第 2 节约定的入口）。"
        )
    return searcher


def _pick(hit: Any, *names: str) -> Any:
    """从 dict 或对象上取字段（`rag` 交付前接口形态可能微调，这里不写死一种）。"""
    for name in names:
        if isinstance(hit, Mapping):
            if name in hit:
                return hit[name]
        else:
            value = getattr(hit, name, None)
            if value is not None:
                return value
    return None


def _to_doc(hit: Any) -> RetrievedDoc | str:
    """把一条命中归一成 `RetrievedDoc`；不合格的返回**原因串**（`"no_source"` / `"empty_text"`）。

    返回原因而不是 `None`，是为了让「丢了多少、为什么丢」能进审计 ——
    「检索返回 0 条」有两种完全不同的成因（库里确实没有 vs 后端没带 metadata），
    分不清就等于没有可观测性。
    """
    text = _pick(hit, "text", "content", "page_content")
    source = _pick(hit, "source", "document", "file", "doc_id")
    if not isinstance(text, str) or not text.strip():
        return "empty_text"
    if not isinstance(source, str) or not source.strip():
        return "no_source"

    raw_score = _pick(hit, "score", "similarity", "distance")
    try:
        score = float(raw_score) if raw_score is not None else 0.0
    except (TypeError, ValueError):
        score = 0.0

    page = _pick(hit, "page", "page_no")
    chunk_id = _pick(hit, "chunk_id", "id")

    extra = {
        key: value
        for key, value in (hit.items() if isinstance(hit, Mapping) else vars(hit).items())
        if key
        not in {"text", "content", "page_content", "source", "document", "file", "doc_id",
                "score", "similarity", "distance", "page", "page_no", "chunk_id", "id"}
    }
    return RetrievedDoc(
        text=text,
        source=source,
        chunk_id=str(chunk_id) if chunk_id is not None else None,
        score=score,
        page=int(page) if isinstance(page, int) else None,
        extra=extra,
    )


def retrieve_docs(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    search: Searcher | None = None,
    min_score: float | None = None,
    caller: str | None = None,
) -> list[RetrievedDoc]:
    """检索知识库，返回**带溯源**的结果（按 score 降序）——**同步版**。

    参数：
        query: 用户问题（非空）。
        top_k: 期望条数（`1..MAX_TOP_K`，超出会被夹住）。
        search: 检索后端；`None` = 用 `app/rag/retriever.py::hybrid_search`（第 10 步）。
        min_score: 相似度下限（低于此值的命中丢弃）；`None` = 不过滤。
        caller: 审计字段「谁在调用」。`None` = 用**入口声明的**调用者
            （`app/tools/audit.caller_override`，第 15 步 MCP 薄壳走这条）。

    异常：
        `ToolContractError`：`query` 为空、`top_k` 不是正整数、`min_score` 不是数值，
            或没有任何调用者信息（审计不可绕过）；
        `ToolNotAvailableError`：默认后端（`rag/`）尚未落地，且没有注入 `search=`。
        `RuntimeError`：**在事件循环线程里调用**（见下）。

    ## ⚠️ 为什么这里要显式拒绝「在事件循环里调用」

    默认后端 `app/rag/retriever.py::hybrid_search` 是**同步**入口，内部
    `asyncio.run(...)`，所以它不能在已经有事件循环的线程里跑 —— 而那正是
    FastAPI 路由与 LangGraph 节点的处境。

    第一版没有这道检查，于是第 16 步（Web 端）第一次问知识库问题时：
    检索抛 `RuntimeError: This event loop is already running`（或 `asyncio.run` 的
    等价报错），被 `rag_node` 的兜底吞成 refusals，**用户看到的是「没有来源」**。
    症状与「语料没灌」一模一样，而真相是「调用方式错了」—— 见
    `docs/runbook/16.web-runbook.md` §10.1 坑 4。

    异步调用方请用 `await retrieve_docs_async(...)`；脚本/同步调用方继续用本函数。
    """
    _reject_inside_event_loop()
    return _retrieve_docs(query, top_k, search=search, min_score=min_score, caller=caller)


async def retrieve_docs_async(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    search: Searcher | None = None,
    min_score: float | None = None,
    caller: str | None = None,
) -> list[RetrievedDoc]:
    """`retrieve_docs` 的**异步入口**（给 FastAPI 路由、LangGraph 节点用）。

    两个刻意的决定：

    1. **整段丢进 `asyncio.to_thread`**，而不是只把 `searcher(...)` 那一句丢进去 ——
       审计上下文（`audit_tool_call`）、埋点（`record_retrieval`）与过滤都是
       `with` 包着的一段，拆开会让「审计记到的耗时」与「真实检索耗时」对不上；
    2. **复用同一份同步实现**，不另写一遍：两条路径共用一份逻辑，
       「异步版少过滤一次」这种漂移就不可能发生（这也是 `9.api.md` §4 的
       「一处真相」在本层的形态）。

    `asyncio.to_thread` 会把当前 context 复制给工作线程，所以第 15 步 MCP 的
    `caller_override`（contextvar）在异步路径上同样生效（`tests/mcp` 覆盖）。
    """
    return await asyncio.to_thread(
        _retrieve_docs, query, top_k, search=search, min_score=min_score, caller=caller
    )


def _reject_inside_event_loop() -> None:
    """在事件循环线程里调用同步入口时，给一条**能照着修**的报错。

    与 `app/rag/retriever.py::Retriever.search_sync` 同一条纪律：不要让别人去猜
    `RuntimeError: This event loop is already running` 是什么意思。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "retrieve_docs() 不能在事件循环内调用（FastAPI 路由 / LangGraph 节点都是这种场景）："
        "请改用 `await retrieve_docs_async(query, top_k, ...)`（它内部走 asyncio.to_thread）。"
    )


def _retrieve_docs(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    search: Searcher | None = None,
    min_score: float | None = None,
    caller: str | None = None,
) -> list[RetrievedDoc]:
    """真正的实现（同步、无事件循环检查）；对外由上面两个入口包着。"""
    if not isinstance(query, str) or not query.strip():
        raise ToolContractError("query 不能为空（知识库检索必须带问题文本）")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ToolContractError(f"top_k 必须是 >= 1 的整数，收到 {top_k!r}")
    if min_score is not None and not isinstance(min_score, (int, float)):
        raise ToolContractError(f"min_score 必须是数值或 None，收到 {min_score!r}")

    effective_top_k = min(top_k, MAX_TOP_K)
    searcher = search if search is not None else _default_searcher()

    # 第 11 步：埋点用 `with` 包住**整个调用**（含后端检索与溯源过滤）——
    # 对调用方来说「检索花了多久」就是这一段，而 `RETRIEVAL_DURATION` 要回答的
    # 正是「用户等了多久」这个问题（把过滤排除掉会低估真实等待）。
    with record_retrieval(hits=0) as call:
        with audit_tool_call(
            TOOL_NAME, caller=caller, query=query, top_k=effective_top_k, min_score=min_score
        ) as handle:
            raw_hits = list(searcher(query=query, top_k=effective_top_k))

            docs: list[RetrievedDoc] = []
            dropped_reasons: dict[str, int] = {}
            for hit in raw_hits:
                doc = _to_doc(hit)
                if isinstance(doc, str):
                    dropped_reasons[doc] = dropped_reasons.get(doc, 0) + 1
                    continue
                if min_score is not None and doc.score < float(min_score):
                    dropped_reasons["below_min_score"] = dropped_reasons.get("below_min_score", 0) + 1
                    continue
                docs.append(doc)

            # 排序放最后：`rag` 交付时可能返回 RRF 融合分或 rerank 分，
            # 这里统一按 score 降序，让「第 10 步换排序口径」不会静默改变上层看到的顺序。
            docs.sort(key=lambda doc: doc.score, reverse=True)

            if dropped_reasons.get("no_source"):
                # 用 WARNING 而不是静默丢弃：这条日志是「rag 侧有片段没带 metadata」的唯一信号，
                # 第 10 步的数据质量问题应当在这里被看见，而不是等合成 Agent 答不出出处才发现。
                logger.warning(
                    "检索命中里有 %d 条没有 source 溯源，已按「只返回带溯源的结果」丢弃"
                    "（调用方 %s，query=%r）",
                    dropped_reasons["no_source"],
                    handle.caller,
                    query[:50],
                )

            handle.rows = len(docs)
            handle.details.update(
                {
                    "hits_raw": len(raw_hits),
                    "hits_returned": len(docs),
                    "dropped": dict(dropped_reasons),
                    "top_k": effective_top_k,
                    "backend": getattr(searcher, "__module__", "unknown"),
                }
            )
            # 第 11 步：给埋点补上「候选池 → 命中 → 是否降级」这条链（耗时由 `with` 自己量）。
            # 「检索慢」与「检索不准」是两类问题，前者看 P99，
            # 后者要看 hits_raw 与 hits_returned 的比值（第 10 步的 E2 结论就是从这三个数里算出来的）。
            # ⚠️ 查询文本**不进埋点**（问题本身可能含 PII，见 instrument.record_retrieval）。
            call.set_attributes(
                {
                    "hits": len(docs),
                    "candidates": len(raw_hits),
                    "degraded": bool(dropped_reasons),
                }
            )
            call.outcome("degraded" if dropped_reasons else "hits", rows=len(docs))
            return docs


__all__ = [
    "DEFAULT_TOP_K",
    "MAX_TOP_K",
    "RAG_FUNCTION",
    "RAG_MODULE",
    "RetrievedDoc",
    "Searcher",
    "retrieve_docs",
    "retrieve_docs_async",
]
