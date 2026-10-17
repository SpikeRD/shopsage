"""「空结果不写缓存」（issue 010 第 1 条修法，产品已确认）。

这两条用例钉的是**同一条判据的两侧**：

| 场景 | 本轮产出 | 该不该写缓存 |
|---|---|---|
| 知识库分支，但检索回 0 条（`citations=[]`） | 「我没查到…」 | **不写**（瞬态，不能固化 7 天） |
| 知识库分支，真检索到片段 | 「7 天内可退货[1]」 | **照写**（E1 的收益全在这里） |
| 退款分支，`citations=[]` | 「已提交退款单 28」 | **本来就不写**（它没有 `cache_context`） |

⚠️ 第三行是这次修法**最容易写错**的地方。我一开始的担心是「只看 `citations == []`
会误伤退款路径」，还照着这个担心写了一条会失败的用例 —— 实测证明担心是多余的：
`remember_answer` 的第一句就是 `if not context: return None`，而 `cache_context`
**只有 `rag_node` 会写**，退款路径从来没有它。

**这个纠正本身值得留在文件里**：它说明「空结果」这个判据真正的作用域是
**知识库分支**，而不是「所有 `citations` 为空的轮次」。判据写成
`cache_context and citations` 是**显式**表达这一点，而不是碰巧靠下游的短路生效 ——
将来 `remember_answer` 一旦放宽 `context` 的要求，显式判据仍然正确。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.synthesis import synthesis_node
from tests.graph_helpers import AgentFakeLLM


def _state(
    *,
    question: str = "退货政策 7天无理由退货",
    docs: list[Any] | None = None,
    cache_context: dict[str, Any] | None = None,
    refund: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """造一个 synthesis 看到的 state（只放本节点读的那几个键）。"""
    from app.agents.state import inject_identity

    state: dict[str, Any] = {
        "messages": [{"role": "user", "content": question}],
        **inject_identity(2, "customer"),
        "docs": list(docs or []),
        "citations": [],
        "refusals": [],
        "rows": [],
        "row_meta": {},
        "analysis": {},
    }
    if cache_context is not None:
        state["cache_context"] = cache_context
    if refund is not None:
        state["refund"] = refund
    return state


class RecordingCache:
    """记录 `remember_answer` 到底有没有被调用（而不是去查存储）。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> Any:
        self.records.append(kwargs)

        class _Write:
            def summary(self) -> str:
                return "已写缓存（测试）"

        return _Write()


def _llm(answer: str, cited: list[int] | None = None) -> AgentFakeLLM:
    return AgentFakeLLM(
        synthesis={"answer": answer, "cited": cited or []},
    )


@pytest.mark.asyncio
async def test_empty_retrieval_on_the_rag_branch_is_not_cached() -> None:
    """★知识库分支 + 0 条出处 → **不写**缓存。

    这正是用户报告的那类答案：「本次知识库检索无命中」。
    它最常见的成因是瞬时的（语料刚被全量回归清空、检索后端抖了一下），
    而政策类 TTL 是 **7 天** —— 把它写进去等于把一次故障固化成一周的答案。
    """
    cache = RecordingCache()
    state = _state(
        docs=[],                      # 检索回 0 条
        cache_context={"question": "退货政策 7天无理由退货", "fingerprint": "fp"},  # 但走了知识库
    )

    out = await synthesis_node(
        state,
        llm=_llm("本次知识库检索没有命中，我这边看不到相关条款。"),
        cache=cache,
    )

    assert cache.records == [], "空结果不该进缓存：它把一次瞬时故障固化成了 7 天的答案"
    assert out["cache_info"]["write"] == "skipped:empty_citations"


@pytest.mark.asyncio
async def test_retrieval_with_citations_is_still_cached() -> None:
    """有出处的答案照常写缓存（**别把修法做成「干脆不缓存」**）。"""

    class _Doc:
        source = "docs/kb/退货政策.md"

        def citation(self) -> str:
            return "docs/kb/退货政策.md #c1"

    cache = RecordingCache()
    state = _state(docs=[_Doc()], cache_context={"question": "q", "fingerprint": "fp"})

    out = await synthesis_node(
        state,
        llm=_llm("自签收之日起 7 天内可申请无理由退货[1]。", cited=[1]),
        cache=cache,
    )

    assert len(cache.records) == 1, "有出处的答案必须照常写缓存，否则这次修法把 E1 的收益清零了"
    assert out["citations"] == ["docs/kb/退货政策.md #c1"]
    assert out["cache_info"]["write"] == "已写缓存（测试）"


@pytest.mark.asyncio
async def test_refund_answer_without_citations_is_not_affected() -> None:
    """退款分支：`citations=[]`，但**本来就不写缓存**（它没走知识库）。

    ⚠️ 这条用例最初写反了，记录下来因为它纠正了一个**看起来很像真的**担心：

    我原以为「退款路径 `citations` 恒为空」会被「空结果不写缓存」误伤。
    实测不是 —— 因为 `remember_answer` 的第一句就是
    `if not context: return None`，而 `cache_context` **只有 `rag_node` 会写**。
    退款走的是 `refund_node`，从来没有 `cache_context`，所以它**一向**不产生缓存条目
    （这本身是对的：退款结果带用户身份与时效，本来就不该被语义缓存复用）。

    所以这条用例真正的价值是**把边界钉死**：以后有人想给退款路径接缓存时，
    会先在这里看到「它为什么不写」，而不是顺手放开后踩到「陈旧退款结果」。
    """
    cache = RecordingCache()
    state = _state(
        question="帮我申请一笔退款",
        docs=[],
        cache_context=None,           # ← 没走 rag_node，本来就不会写
        refund={
            "status": "submitted",
            "refund_id": 28,
            "order_id": 12,
            "amount": "12346.75",
        },
    )

    await synthesis_node(
        state,
        llm=_llm("您的退款申请已提交，退款单号 28，等待平台审核。"),
        cache=cache,
    )

    assert cache.records == [], "退款路径没有 cache_context，从来就不写缓存（这是预期语义）"


@pytest.mark.asyncio
async def test_no_cache_context_means_no_write_regardless() -> None:
    """没走知识库分支（`cache_context` 为空）时，即便有 docs 也不写。

    这条钉的是既有语义：`remember_answer` 靠 `cache_context` 重建写入所需的一切，
    没有它本来就不该写（`rag_node` 之外的路径不该产生缓存条目）。
    """

    class _Doc:
        source = "docs/kb/x.md"

        def citation(self) -> str:
            return "docs/kb/x.md #c1"

    cache = RecordingCache()
    state = _state(docs=[_Doc()], cache_context=None)

    out = await synthesis_node(state, llm=_llm("好的。", cited=[1]), cache=cache)

    assert cache.records == []
    assert out["cache_info"]["write"] == "skipped:empty_citations"
