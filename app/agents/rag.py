"""RAG Agent：**知识库混合检索 + rerank + 溯源**（`7.agents_module.md` §1、§6 第二批第 5 条）。

本模块薄得几乎只剩接线 —— 这是**故意的**。检索质量（chunk 策略、向量+关键词混合、
RRF 融合、rerank）全部属第 10 步 `rag/`，本节点的职责只有四件：

1. **把问题交给检索工具**：`retrieve_docs(query, top_k, search=..., caller="agent:rag")`；
2. **让 rerank 有地方发生**：排序与阈值过滤由工具层统一口径完成
   （第 10 步换成 rerank 分时，上层看到的顺序语义不变，见 `tools/retriever.py` §5.3）；
3. **保住溯源**：只把 `RetrievedDoc` 交给 synthesis，`citation()` 直接进回答，
   无源的命中在工具层就被丢了（`6.tools-runbook.md` §5.2）——本节点**不做二次过滤**，
   因为过滤只该有一处真相；
4. **未就绪时不假装没有**：`app/rag/` 属第 10 步，此刻默认后端会抛
   `ToolNotAvailableError`。本节点把它落成 `refusals`（如实回答「知识库暂不可用」），
   **不落成空结果** —— 空结果会被上层理解成「知识库里没有这条」，那是两回事。

⚠️ 为什么 `search=` 一路透传到节点参数里：第 8 步在此之前要跑通
`router → rag → synthesis` 的骨架，而第 10 步还没做；注入点让骨架能先接临时实现，
也让本节点的测试完全不依赖语料与向量库。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final

from app.agents.state import GraphState, last_user_message
from app.tools import retrieve_docs
from app.tools.errors import ToolError, ToolNotAvailableError
from app.tools.retriever import DEFAULT_TOP_K, MAX_TOP_K, Searcher

logger = logging.getLogger(__name__)

#: 审计里的 caller（见 `6.tools-runbook.md` §3.3）
CALLER: Final[str] = "agent:rag"

#: 相似度下限的**默认值**：不设（`None`）。
#: 为什么不在节点层拍一个阈值：阈值口径属第 10 步（它与向量模型、语料分布强相关），
#: 在这一层写死一个数字，等于把「检索质量」的调参权藏进 agent ——
#: 第 10 步调阈值时要来改 agent，那就说明分层错了。需要时由调用方 `/ config` 传。
DEFAULT_MIN_SCORE: Final[float | None] = None


def citations_of(docs: Sequence[Any]) -> list[str]:
    """把命中列表转成出处串列表（`[doc.citation() for doc in docs]`）。

    单独一个函数是为了让「回答里附什么出处」只有一处实现：
    第 9 步的接口、第 16 步的 Web 端、评测都调它，格式不会各写一份。
    """
    out: list[str] = []
    for doc in docs:
        citation = getattr(doc, "citation", None)
        if callable(citation):
            out.append(str(citation()))
        elif getattr(doc, "source", None):
            out.append(str(doc.source))
    return out


def docs_to_prompt_block(docs: Sequence[Any], *, max_chars: int = 2400) -> str:
    """把命中渲染成 synthesis 用的文本块（每条带**显式编号与出处**）。

    编号的意义：synthesis 的回答要靠编号引用（`[1]`），而不是靠复述文档名 ——
    模型的复述会漂移（把「退货政策.md」写成「退货政策文档」），
    而这些编号最后会被 mapping 回真实出处（`citations`），所以引用必须可机器解析。

    `max_chars` 是上下文硬上限（E2）：20 条命中 × 每条上千字会顶爆 prompt，
    截断时**明确写出**「已截断」，否则模型会以为后面没有了。
    """
    if not docs:
        return "（知识库没有返回任何片段）"
    lines: list[str] = []
    used = 0
    for index, doc in enumerate(docs, start=1):
        text = str(getattr(doc, "text", "")).strip()
        citation = citations_of([doc])
        cite = citation[0] if citation else "（无出处）"
        block = f"[{index}]（出处：{cite}）\n{text}"
        if used + len(block) > max_chars:
            lines.append(f"…（共 {len(docs)} 条命中，因上下文上限只展示了前 {index - 1} 条）")
            break
        lines.append(block)
        used += len(block)
    return "\n\n".join(lines)


async def rag_node(
    state: GraphState,
    *,
    search: Searcher | None = None,
    top_k: int = DEFAULT_TOP_K,
    min_score: float | None = DEFAULT_MIN_SCORE,
) -> dict[str, Any]:
    """图节点：检索知识库，写 `docs` + `citations`；后端未就绪则写 `refusals`。

    异常口径：
      * `ToolNotAvailableError`（第 10 步未落地）→ **吞掉并写 refusals**：
        这是「能力还没接上」，用户该看到一句实话而不是 500；
      * `ToolContractError`（问题为空）→ 让它抛：参数错误必须尽早暴露；
      * 其余 `ToolError` → 原样抛（可重试的错要能被第 12 步看见，理由同 data 节点）。

    ⚠️ 本节点是**异步**的，虽然内部全是同步调用。理由：第 10 步的 `hybrid_search`
    会调 `embed()`（async），届时节点内要 `await`；先把签名定成 async
    可以免掉「第 10 步落地时所有调用方都要改成 await」的连锁改动。
    """
    question = last_user_message(state).strip()
    if not question:
        raise ValueError(
            "rag_node 收到空问题：state['messages'] 里没有 user 消息，无法检索知识库。"
        )

    effective_top_k = max(1, min(int(top_k), MAX_TOP_K))
    try:
        docs = retrieve_docs(
            question,
            effective_top_k,
            search=search,
            min_score=min_score,
            caller=CALLER,
        )
    except ToolNotAvailableError as exc:
        reason = f"知识库检索暂不可用：{exc}"
        logger.warning("rag 后端未就绪，落成 refusals 而不是空结果：%s", exc)
        return {"docs": [], "citations": [], "refusals": [reason]}
    except ToolError:
        raise

    citations = citations_of(docs)
    logger.info(
        "rag 完成：query=%r top_k=%d 命中=%d（全部带溯源）",
        question[:50],
        effective_top_k,
        len(docs),
    )
    return {
        "docs": list(docs),
        "citations": citations,
        # 命中为 0 不写 refusals：那是**数据事实**（知识库里确实没有），
        # 由 synthesis 如实表述；把它写成 refusal 会与「能力未接上」混成一类。
        "refusals": [],
    }


__all__ = [
    "CALLER",
    "DEFAULT_MIN_SCORE",
    "citations_of",
    "docs_to_prompt_block",
    "rag_node",
]
