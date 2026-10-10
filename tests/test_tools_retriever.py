"""`retriever` 的测试：**溯源是硬约束**，且「后端没落地」要说人话。

本文件的用例全部通过 `search=` 注入假后端（第 10 步 `rag/` 还没落地）——
这正是「用注入点把能力测完」的写法：检索算法不是本模块的责任，本模块的责任是
**契约**（字段归一、溯源强制、条数收敛、审计），契约用假后端反而测得干净。

`6.tools.md` §7 的第 4 条用例（`test_retrieve_docs_has_citation`）就在下面，
另外补了它的**反面**：没有来源的命中必须被丢弃 —— 只断言「返回的都有来源」，
一个「把所有命中都丢光」的实现也能通过。
"""

from __future__ import annotations

import logging

import pytest

from app.tools.audit import audit_records
from app.tools.errors import ToolContractError, ToolNotAvailableError
from app.tools.retriever import (
    DEFAULT_TOP_K,
    MAX_TOP_K,
    RetrievedDoc,
    retrieve_docs,
)

CALLER = "test"


def fake_search(hits):
    """造一个假检索后端；记录它收到的参数，便于断言 top_k 收敛真的传下去了。"""

    calls: list[dict] = []

    def _search(*, query: str, top_k: int):
        calls.append({"query": query, "top_k": top_k})
        return hits

    _search.calls = calls  # type: ignore[attr-defined]
    return _search


def test_retrieve_docs_has_citation():
    """`6.tools.md` §7 第 4 条：每条结果的 `source` 必须非空（合成 Agent 靠它附出处）。"""
    results = retrieve_docs(
        "退货政策",
        search=fake_search(
            [
                {"text": "七天无理由退货", "source": "退货政策.md", "chunk_id": "c1", "score": 0.82},
                {"text": "运费由买家承担", "source": "FAQ.md", "page": 3, "score": 0.61},
            ]
        ),
        caller=CALLER,
    )
    assert all(doc.source for doc in results)
    assert [doc.citation() for doc in results] == ["退货政策.md #c1", "FAQ.md p.3"]


def test_sourceless_hits_are_dropped_with_warning(caplog):
    """**反面用例**：没有来源的命中一律丢弃 + WARNING（杜绝「看起来有出处」的编造）。"""
    with caplog.at_level(logging.WARNING, logger="app.tools.retriever"):
        results = retrieve_docs(
            "退货政策",
            search=fake_search(
                [
                    {"text": "有出处的片段", "source": "退货政策.md", "score": 0.9},
                    {"text": "凭空冒出来的片段", "score": 0.99},  # 最高分但没有来源
                    {"text": "   ", "source": "空文本.md", "score": 0.5},  # 空文本：同样不可引用
                    {"text": "来源是空串", "source": "", "score": 0.4},
                ]
            ),
            caller=CALLER,
        )

    assert [doc.text for doc in results] == ["有出处的片段"]
    assert any("溯源" in record.getMessage() for record in caplog.records)


def test_all_sourceless_hits_yield_empty_result_not_silence(caplog):
    """全是无源命中时返回空列表（而不是抛错）：上层应当回答「没有可引用的资料」。"""
    with caplog.at_level(logging.WARNING, logger="app.tools.retriever"):
        results = retrieve_docs("退货政策", search=fake_search([{"text": "x"}]), caller=CALLER)
    assert results == []


def test_results_are_sorted_by_score_desc():
    """统一按 score 降序：第 10 步换排序口径（RRF/rerank）时，上层看到的顺序语义不变。"""
    results = retrieve_docs(
        "退货",
        search=fake_search(
            [
                {"text": "低", "source": "a.md", "score": 0.2},
                {"text": "高", "source": "b.md", "score": 0.9},
                {"text": "中", "source": "c.md", "score": 0.5},
            ]
        ),
        caller=CALLER,
    )
    assert [doc.text for doc in results] == ["高", "中", "低"]


def test_top_k_is_clamped_and_forwarded():
    """`top_k` 收敛到 `MAX_TOP_K`：LLM 要「Top 500」时不能让上下文和成本一起爆（E2）。"""
    searcher = fake_search([{"text": "t", "source": "s.md", "score": 1.0}])
    retrieve_docs("问题", top_k=500, search=searcher, caller=CALLER)
    assert searcher.calls[-1]["top_k"] == MAX_TOP_K  # type: ignore[attr-defined]

    retrieve_docs("问题", top_k=3, search=searcher, caller=CALLER)
    assert searcher.calls[-1]["top_k"] == 3  # type: ignore[attr-defined]
    assert DEFAULT_TOP_K <= MAX_TOP_K
    assert searcher.calls[-1]["query"] == "问题"  # type: ignore[attr-defined]


def test_min_score_filters_low_relevance():
    """相似度下限：低于阈值的命中不进上下文（阈值口径属第 10 步，这里只证明过滤生效）。"""
    results = retrieve_docs(
        "退货",
        min_score=0.5,
        search=fake_search(
            [
                {"text": "相关", "source": "a.md", "score": 0.8},
                {"text": "不相关", "source": "b.md", "score": 0.1},
            ]
        ),
        caller=CALLER,
    )
    assert [doc.text for doc in results] == ["相关"]


def test_hits_can_be_objects_not_only_dicts():
    """后端交付形态可能微调（dict 或对象）：适配层两种都吃，上层不必跟着改。"""
    class Hit:
        def __init__(self, text, source, score):
            self.text = text
            self.source = source
            self.score = score

    results = retrieve_docs("退货", search=fake_search([Hit("七天无理由", "政策.md", 0.7)]), caller=CALLER)
    assert results[0].text == "七天无理由" and results[0].source == "政策.md"


def test_extra_metadata_is_preserved():
    """额外 metadata 原样保留（第 10 步的 `updated_at`/`section` 等供前端与评测使用）。"""
    results = retrieve_docs(
        "退货",
        search=fake_search(
            [{"text": "t", "source": "s.md", "score": 1.0, "updated_at": "2026-09-01", "section": "3.2"}]
        ),
        caller=CALLER,
    )
    assert results[0].extra == {"updated_at": "2026-09-01", "section": "3.2"}


def test_missing_rag_backend_is_actionable(monkeypatch):
    """`rag/` 未落地时报错要能照着修：说明属第 10 步、并给出三种当下可走的做法。

    ⚠️ 第 10 步（rag）落地后，**默认路径不再报错** —— 所以本用例改成显式模拟
    「模块不存在」这一种情况（把 `RAG_MODULE` 指向一个不存在的模块）。
    这样两件事都被钉住：
      ① 报错话术仍然可照做（新环境/裁剪过的部署里仍会遇到）；
      ② 默认路径**确实**已经接通（见 `test_default_backend_is_the_real_rag_module`）。
    """
    from app.tools import retriever as tools_retriever

    monkeypatch.setattr(tools_retriever, "RAG_MODULE", "app.rag.retriever_not_here")
    with pytest.raises(ToolNotAvailableError) as excinfo:
        retrieve_docs("退货政策", caller=CALLER)
    message = str(excinfo.value)
    assert "app.rag.retriever_not_here" in message
    assert "第 10 步" in message
    assert "search=" in message  # 注入点


def test_default_backend_is_the_real_rag_module():
    """第 10 步落地后，默认后端必须解析到 `app.rag.retriever.hybrid_search`。

    这条断言的意义：**防止有人把默认后端悄悄改回"抛错"或"返回空"**。
    返回空比抛错更危险 —— 空结果会被上层理解成「知识库里没有这条」。
    """
    from app.rag.retriever import hybrid_search
    from app.tools.retriever import RAG_FUNCTION, RAG_MODULE, _default_searcher

    import importlib

    module = importlib.import_module(RAG_MODULE)
    searcher = _default_searcher()
    assert callable(searcher)
    assert getattr(module, RAG_FUNCTION) is hybrid_search
    assert searcher.__module__ == RAG_MODULE


def test_invalid_arguments_raise_contract_error():
    """空问题 / 非法 top_k / 非法 min_score：契约错误早炸，别等后端返回空列表。"""
    with pytest.raises(ToolContractError, match="query"):
        retrieve_docs("   ", search=fake_search([]), caller=CALLER)
    with pytest.raises(ToolContractError, match="top_k"):
        retrieve_docs("退货", top_k=0, search=fake_search([]), caller=CALLER)
    with pytest.raises(ToolContractError, match="top_k"):
        retrieve_docs("退货", top_k=True, search=fake_search([]), caller=CALLER)
    with pytest.raises(ToolContractError, match="min_score"):
        retrieve_docs("退货", min_score="high", search=fake_search([]), caller=CALLER)


def test_retrieval_is_audited_with_drop_counts(caplog):
    """审计要能回答「这次检索给了几条、丢了几条无源命中」（数据质量问题的唯一信号源）。"""
    with caplog.at_level(logging.INFO, logger="app.tools.audit"):
        retrieve_docs(
            "退货政策",
            search=fake_search(
                [{"text": "a", "source": "s.md", "score": 1.0}, {"text": "b", "score": 0.9}]
            ),
            caller="agent:rag",
        )

    record = audit_records(caplog.records)[-1]
    assert record["tool"] == "retrieve_knowledge"
    assert record["caller"] == "agent:rag"
    assert record["rows"] == 1
    assert record["details"]["hits_raw"] == 2
    assert record["details"]["dropped"] == {"no_source": 1}
    assert record["args"]["query"] == "退货政策"


def test_doc_dataclass_is_frozen_and_citation_is_stable():
    """`RetrievedDoc` 冻结（防止上层顺手改掉溯源字段）+ `citation()` 格式稳定。"""
    doc = RetrievedDoc(text="t", source="政策.md", chunk_id="c9", score=0.5, page=7)
    with pytest.raises(Exception):
        doc.source = "改掉试试"  # type: ignore[misc]
    assert doc.citation() == "政策.md p.7 #c9"
    assert RetrievedDoc(text="t", source="s.md").citation() == "s.md"
