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

from app.agents.state import GraphState, last_user_message, messages_of, role_of
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
    cache: Any = None,
) -> dict[str, Any]:
    """图节点：检索知识库，写 `docs` + `citations`；后端未就绪则写 `refusals`。

    ## 第 13 步：缓存接在哪一步（以及为什么是这一步）

    缓存放在**检索之前**，顺序是「查缓存 → 命中则直接返回 → 未命中才检索 → 写缓存」。
    三个理由：

    1. `13.cache.md` §4 第 5 节最后一条：「命中则整条链路（检索→生成）都不走，
       省的不只是 token 还有延迟」—— 这条只在「缓存闸在检索之前」时成立；
    2. 检索本身就要花钱：`hybrid_search` 要算一次问题 embedding（本地 bge-m3 实测 15~40ms），
       rerank 还要过一遍 CrossEncoder —— 命中时这两步一起省掉；
    3. `rag` 是**只读且无副作用**的路径，把缓存放在它前面不需要处理任何写路径的一致性问题
       （这一点与 `data` 分支相反：那里缓存的是「用户自己的数据」，
       一旦用户的订单变了，缓存就错了 —— 所以订单类问题在 `intent` 里被判成强时效、
       根本不进这条路径，见 `app/cache/core.py`）。

    ## 缓存为 `None` 时的行为**一个字都没变**

    `cache=None` 时本函数与第 10 步的实现逐行相同（这也是既有 1xxx 条用例不受影响的原因）。
    缓存是**注入点**，不是默认行为 —— 与 `search=` 同一口径。

    ## `cached_answer` 为什么要从这里返回

    命中的是**整段答案**（不是「检索结果」）：`synthesis` 看到 `state["cached_answer"]`
    就跳过模型调用。这样一次命中省掉的是「embedding + 向量检索 + 关键词检索 + rerank
    + 一次上千 token 的生成」。若只缓存检索结果，省下的是前半段，
    而成本的大头在后半段 —— 那等于把 E1 的目标落空了一半。

    异常口径（与第 10 步完全一致）：
      * `ToolNotAvailableError`（后端未就绪）→ **吞掉并写 refusals**；
      * `ToolContractError`（问题为空）→ 让它抛；
      * 其余 `ToolError` → 原样抛（可重试的错要能被第 12 步看见）。
    """
    question = last_user_message(state).strip()
    if not question:
        raise ValueError(
            "rag_node 收到空问题：state['messages'] 里没有 user 消息，无法检索知识库。"
        )

    effective_top_k = max(1, min(int(top_k), MAX_TOP_K))

    # ── 第 13 步：缓存闸（在检索之前）──────────────────────────────────────
    prepared = None
    cache_context: dict[str, Any] = {}
    if cache is not None:
        from app.cache.params import retrieval_params

        # 检索参数**只在这里构造一次**（`params.retrieval_params` 是唯一构造处），
        # 然后沿 state 传给 synthesis —— 写路径必须用**逐字相同**的一组
        # （见 `remember_answer` 的说明：漏一个就永远命中不了）。
        cache_params = retrieval_params(
            top_k=effective_top_k, min_score=min_score, search=search
        )
        prepared = await cache.semantic_get(
            messages_of(state),
            user_id=int(state.get("user_id") or 0),
            role=role_of(state),
            params=cache_params,
        )
        if prepared.hit:
            logger.info("rag 命中语义缓存（%s），跳过检索与生成", prepared.summary())
            return {
                "docs": [],
                "citations": list(prepared.citations),
                "cached_answer": prepared.answer,
                "cache_info": {
                    "outcome": prepared.log.outcome,
                    "detail": prepared.log.detail,
                    "age_seconds": round(prepared.age_seconds(), 3),
                    "similarity": prepared.log.similarity,
                },
                "cache_params": cache_params,
                "refusals": [],
            }
        # ── 把「写缓存需要的一切」变成**可序列化的字段** ────────────────────
        # ⚠️ 不能把 `Prepared` 本身放进 state：LangGraph 的 checkpointer 会
        # msgpack 序列化 state 值，dataclass 被静默丢掉、闭包直接抛 TypeError
        # （两种都在本仓实测过，症状都是「写了但永远不命中」）。
        # 所以这里只放字符串 / 整数 / dict，`synthesis` 再喂回 `cache.record(...)`。
        cache_context = {
            "question": prepared.question,
            "raw_question": prepared.raw_question,
            "scope": prepared.scope,
            "fingerprint": prepared.fingerprint,
            "revision": prepared.revision,
            "tier": prepared.decision.tier,
            "ttl_seconds": prepared.decision.ttl_seconds,
        }

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
    out: dict[str, Any] = {
        "docs": list(docs),
        "citations": citations,
        # 命中为 0 不写 refusals：那是**数据事实**（知识库里确实没有），
        # 由 synthesis 如实表述；把它写成 refusal 会与「能力未接上」混成一类。
        "refusals": [],
    }
    if prepared is not None:
        # 把这次查询的账目带上（未命中也要带：`stale` 与 `miss` 的运维动作不同）
        out["cache_info"] = {
            "outcome": prepared.log.outcome,
            "detail": prepared.log.detail,
            "age_seconds": 0.0,
            "similarity": prepared.log.similarity,
        }
        out["cache_params"] = cache_params
        out["cache_context"] = cache_context
    return out


async def remember_answer(
    state: GraphState,
    answer: str,
    *,
    cache: Any,
    citations: Sequence[Any] = (),
) -> Any:
    """把一次**真正生成出来**的答案写进缓存（由 `synthesis` 在生成之后调用）。

    ## 为什么写这个动作落在 `synthesis` 而不是 `rag_node`

    答案还没生成。而 `synthesis` 手里没有「读那一刻」的上下文（问题串、指纹、版本），
    所以 `rag_node` 把两样东西写进 state 交给它：

    | state 键 | 是什么 | 不带会怎样 |
    |---|---|---|
    | `cache_params` | 那一组检索参数（`top_k` / `min_score` / `embedder_key`） | 指纹不同 → 写了永远读不到 |
    | `cache_context` | 问题串 / scope / 指纹 / 版本 / 分档 / TTL（**全可序列化**） | 键不同 → 写了永远读不到 |

    ## ⚠️ 为什么是「一堆显式字段」而不是 `Prepared` 或闭包（本仓实测）

    图会在 `synthesis` **之后**把这一轮的问答追加进 `messages`，所以写缓存时
    state 里的消息已经不是读的那一串了。想绕过它、把「读那一刻的上下文」
    整个带过去，试过两条路**都失败**：

    * 把 `Prepared`（dataclass）放进 state → **被 LangGraph 静默丢掉**
      （checkpointer 的 msgpack 序列化不接受它；表现是「写了但永远不命中」）；
    * 把上下文封成一个闭包放进 state →
      `TypeError: Type is not msgpack serializable: function`。

    结论：**跨节点传递的只能是可序列化的值**。所以 `rag_node` 只放
    「重建一次写操作所需的全部字段」，本函数把它们喂给 `Cache.record(...)`
    （那个方法会重建成 `Prepared` 再走与读路径完全相同的写入逻辑）。

    写失败一律不影响本次回答（fail-open + 一条 WARNING）。
    """
    if cache is None:
        return None
    context = dict(state.get("cache_context") or {})
    if not context:
        # 没有缓存上下文 = 这一轮没走缓存闸（`cache=None`，或命中了直接返回）
        return None
    try:
        return cache.record(
            answer=answer,
            citations=list(citations),
            meta={"kind": "rag_answer"},
            **context,
        )
    except Exception as exc:  # noqa: BLE001 - 写缓存失败不该影响本次回答
        logger.warning("写缓存失败（已忽略，不影响本次回答）：%s: %s", type(exc).__name__, exc)
        return None


__all__ = [
    "CALLER",
    "DEFAULT_MIN_SCORE",
    "citations_of",
    "docs_to_prompt_block",
    "rag_node",
    "remember_answer",
]
