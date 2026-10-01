"""RAG Agent 的用例：检索接线、溯源保留、未就绪的诚实口径（`7.agents_module.md` §6 第二批第 5 条）。

⚠️ 第 10 步（`app/rag/`）尚未落地，所以本文件**全部**用注入的假后端
（与 `tests/test_tools_retriever.py` 同一做法）。这不影响覆盖度：
本节点该做的（调用工具、保留溯源、区分「没命中」与「没就绪」）全都能测。
"""

from __future__ import annotations

import logging

import pytest

from app.agents.rag import (
    CALLER,
    citations_of,
    docs_to_prompt_block,
    rag_node,
)
from app.tools.errors import ToolContractError, ToolNotAvailableError
from app.tools.retriever import RetrievedDoc
from tests.agents_helpers import state_of


def _fake_search(**hits):  # noqa: ANN003, ANN202
    """造一个检索后端：返回 dict 形态的命中（与第 10 步的契约一致）。"""
    payload = hits.get("hits", [])

    def search(*, query: str, top_k: int):  # noqa: ANN202
        search.calls.append({"query": query, "top_k": top_k})  # type: ignore[attr-defined]
        return payload

    search.calls = []  # type: ignore[attr-defined]
    return search


DOCS = [
    {"text": "七天无理由退货，自签收次日起算。", "source": "退货政策.md", "chunk_id": "c12", "page": 2, "score": 0.88},
    {"text": "凭空冒出来的片段（没有 source）", "score": 0.95},
    {"text": "运费由买家承担", "source": "FAQ.md", "chunk_id": "c3", "score": 0.42},
]


async def test_rag_node_returns_docs_with_citations() -> None:
    search = _fake_search(hits=DOCS)
    out = await rag_node(state_of("退货政策是什么"), search=search)

    assert [doc.source for doc in out["docs"]] == ["退货政策.md", "FAQ.md"]
    assert out["citations"] == ["退货政策.md p.2 #c12", "FAQ.md #c3"]
    assert out["refusals"] == []
    assert search.calls == [{"query": "退货政策是什么", "top_k": 5}]


async def test_rag_node_drops_hits_without_source() -> None:
    """无溯源的命中被**工具层**丢弃（本节点不做二次过滤，过滤只该有一处真相）。"""
    search = _fake_search(hits=DOCS)
    out = await rag_node(state_of("退货政策是什么"), search=search)
    assert len(out["docs"]) == 2, "3 条命中里那 1 条无 source 的必须被丢掉"


async def test_rag_node_clamps_top_k() -> None:
    """`top_k` 被夹到 1..20：LLM 要「Top 500」不能把上下文与成本一起打爆（E2）。"""
    search = _fake_search(hits=DOCS)
    await rag_node(state_of("退货"), search=search, top_k=500)
    assert search.calls[0]["top_k"] == 20

    await rag_node(state_of("退货"), search=search, top_k=0)
    assert search.calls[1]["top_k"] == 1


async def test_rag_node_without_backend_records_refusal(caplog) -> None:
    """第 10 步未落地：落 `refusals`（而不是空结果）。"""
    with caplog.at_level(logging.WARNING, logger="app.agents.rag"):
        out = await rag_node(state_of("退货政策是什么"))

    assert out["docs"] == [] and out["citations"] == []
    assert len(out["refusals"]) == 1
    assert "知识库检索暂不可用" in out["refusals"][0]
    assert "app.rag.retriever" in out["refusals"][0], "报错要能照着修（说清缺哪一步的交付物）"
    assert any("后端未就绪" in r.getMessage() for r in caplog.records)


async def test_rag_node_empty_hits_is_not_a_refusal() -> None:
    """命中 0 条是**数据事实**，不是「能力未接上」——两者处置完全不同。"""
    out = await rag_node(state_of("退货政策是什么"), search=_fake_search(hits=[]))
    assert out["docs"] == [] and out["refusals"] == []


async def test_rag_node_rejects_empty_question() -> None:
    """空问题抛参数错误（契约问题要早暴露，不许降级成「查不到」）。"""
    with pytest.raises(ValueError, match="空问题"):
        await rag_node({"messages": [], "user_id": 1}, search=_fake_search(hits=[]))


async def test_rag_node_propagates_contract_error() -> None:
    """工具层抛的契约错误原样上抛（不吞成 refusals）。"""
    with pytest.raises((ToolContractError, ToolNotAvailableError)):
        await rag_node(state_of("退货"), search=lambda **kw: (_ for _ in ()).throw(ToolContractError("top_k 非法")))


def test_citations_of_handles_plain_objects() -> None:
    """`citation()` 缺失时退回 `source`（第 10 步交付前接口形态可能微调）。"""

    class Plain:
        source = "手册.md"

    assert citations_of([Plain()]) == ["手册.md"]
    assert citations_of([RetrievedDoc(text="x", source="A.md")]) == ["A.md"]


def test_docs_to_prompt_block_numbers_and_truncates() -> None:
    docs = [RetrievedDoc(text=f"片段 {i}" * 30, source=f"doc{i}.md") for i in range(1, 6)]
    block = docs_to_prompt_block(docs, max_chars=400)

    assert block.startswith("[1]（出处：doc1.md）")
    assert "…（共 5 条命中，因上下文上限只展示了前" in block, "截断必须写出来，否则模型以为后面没有了"


def test_docs_to_prompt_block_handles_empty() -> None:
    assert docs_to_prompt_block([]) == "（知识库没有返回任何片段）"


def test_rag_caller_is_declared() -> None:
    """审计 caller 用约定取值 `agent:rag`（`6.tools-runbook.md` §3.3 的约定表）。"""
    assert CALLER == "agent:rag"
