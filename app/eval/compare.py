"""单 Agent vs 多 Agent 对比报告（`14.eval.md` §4 第 3 条、§6 第 4 步）。

## 两条路的**机制差异**在哪（这是「量化收益」四个字的全部含量）

`14.eval.md` §4 第 3 条要求「同一 golden set 跑『一个大 prompt 全干』vs『多 Agent 图』，
产出量化差距」。要让这个差距**是机制带来的、而不是运气**，两条路必须只差在机制上：

| | 多 Agent（本仓的图） | 单 Agent（对照） |
|---|---|---|
| 路由 | `router` 节点先判意图，再进对应分支 | **没有路由**：一个大 prompt「判断并回答」 |
| 材料 | `rag` 节点**先检索**，把命中片段作为唯一材料喂给合成 | **不给材料**：模型凭参数记忆直接答 |
| 权限 | `router` 注入权限点，`synthesis` 按角色选模板 | 没有身份概念 |
| 护栏 | 输入/输出两侧横切 | 没有 |
| 缓存 | 语义缓存闸在检索之前 | 没有 |

⚠️ **「单 Agent 不给材料」是刻意的，也是最容易被质疑的一处**，所以理由必须写在这里：

一个「单 Agent」如果也能自己调检索工具，那它就不是「一个大 prompt 全干」了 ——
它已经拥有了多 Agent 的**核心机制**（工具调用本身就是一种分工）。
本仓第 7 步给这条路定的名字是最诚实的：**单 Agent = 一个大 prompt 直接回答**
（`7.agents_module.md` §5 第一条描述的正是这种「意图模糊、出问题分不清哪一环坏了」的形态）。

于是对照实验测的其实是**「把检索材料交给模型」这件事值多少钱**：

* 单 Agent 的答案来自**参数记忆** → 政策里的天数、比例、条款号会漂移或编造
  （本仓语料是自建的政策文档，模型从未见过它，所以它的 Faithfulness 会很低 ——
  这不是「我们选了一个笨模型」，而是**任何**模型对未见过的内部语料都只能编）；
* 多 Agent 的答案来自**检索片段** → 事实点在材料里，逐字可核对。

这个差距就是「RAG 值多少钱」的直接量化，而它正是 `14.eval.md` §8 第 2 条要的那句话。

## 两条纪律

1. **两条路必须跑同一批用例、同一套判据、同一个判官**。
   任何一处不同，差距就不再归因于机制（`--only` 的子集要整份传给两边）。
2. **两条路都不许读「自己属于哪一组」**：判官不知道分组（见 `judge.strict_llm_judge`），
   单 Agent 那条路也不知道 golden set 的标准答案 —— 否则就是自己给自己打分。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.eval.dataset import EvalCase, EvalSet
from app.eval.judge import Judge, build_judge
from app.eval.metrics import Aggregate, aggregate, answer_quality, mean, retrieval_quality
from app.eval.pipeline import (
    SINGLE_AGENT_SYSTEM,
    AnswererLike,
    build_single_agent_answerer,
    context_block,
    hit_score,
    hit_source,
)
from app.eval.rag_eval import Searcher, _combine, _maybe_await
from app.eval.settings import RagEvalSettings

logger = logging.getLogger(__name__)

#: 两个方案的规范名（报告、JSON、`--baseline` 的键都用它们）
MULTI_AGENT: Final[str] = "multi_agent"
SINGLE_AGENT: Final[str] = "single_agent"


@dataclass
class Variant:
    """一个方案的对照结果（报告里的 `variants[*]`）。"""

    name: str
    label: str
    count: int
    recall: float
    faithfulness: float
    answer_relevancy: float
    key_fact_hit_rate: float
    refusals: int = 0
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "count": self.count,
            "recall": round(self.recall, 4),
            "faithfulness": round(self.faithfulness, 4),
            "answer_relevancy": round(self.answer_relevancy, 4),
            "key_fact_hit_rate": round(self.key_fact_hit_rate, 4),
            "refusals": self.refusals,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "notes": list(self.notes),
        }


@dataclass
class Comparison:
    """一次对照实验的结论（`14.eval.md` §8 第 2 条要的「量化收益」）。"""

    dataset_name: str
    dataset_version: str
    variants: list[Variant] = field(default_factory=list)
    delta: dict[str, float] = field(default_factory=dict)
    verdict: str = ""
    passed: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": {"name": self.dataset_name, "version": self.dataset_version},
            "variants": [item.as_dict() for item in self.variants],
            "delta": {key: round(value, 4) for key, value in self.delta.items()},
            "verdict": self.verdict,
            "passed": bool(self.passed),
            "notes": list(self.notes),
        }


async def run_single_agent(
    case: EvalCase,
    *,
    llm: Any,
    answerer: AnswererLike | None = None,
    settings: RagEvalSettings | None = None,
) -> str:
    """单 Agent 基线：**一个大 prompt，直接答，不给任何材料**（理由见模块 docstring）。

    真模型那一侧由 `build_single_agent_answerer(llm=...)` 提供；
    `llm=None` 时它是「零成本、可复现」的等价物 —— 抽取式回答器拿到**空的命中列表**
    会回一句「知识库里没有可引用的条款」，而那句**正是在如实报告这条路拿不到材料**。
    这不是「把基线写弱」：一个没有材料的回答器无论用多强的模型，
    都只能凭参数记忆答自建语料里的政策 —— 那正是本节要量化的东西。

    ⚠️ 判断「基线是不是被写得过弱」的正确方法是看**它有没有少给机制**，
    而不是看它答得好不好：这里少给的只有「检索材料」这一项，
    而那一项就是对照实验的自变量（见模块 docstring 的机制表）。
    """
    del settings
    synthesizer = answerer or build_single_agent_answerer(llm=llm)
    return str(await _maybe_await(synthesizer(case.question, [])) or "")

async def run_multi_agent(
    case: EvalCase,
    *,
    search: Searcher,
    answerer: AnswererLike | None = None,
    settings: RagEvalSettings,
    llm: Any = None,
) -> tuple[str, list[Any]]:
    """多 Agent 一侧：**先检索，再只据材料合成**（本仓 `rag_node` 的两段式）。

    返回 `(答案, 命中列表)`；命中列表要原样带出来，因为 Recall 判的是它 ——
    对比表里两边的 Recall 必须是**同一次检索**算出来的（再检一次结果可能不同）。

    ⚠️ 这里刻意**不 import `app/agents/rag.py`**：评测要能在 agent 层坏掉时
    独立说出「检索层是好的」（`14.eval.md` §4 第 2 条的指标分层就是为此）。
    合成 prompt 由 `pipeline.build_llm_answerer` 提供，它带着与 `agents/synthesis.py`
    同一套「材料边界」纪律（例如「没有材料时明确说出来」）。
    """
    from app.eval.pipeline import (  # 延迟 import：避开 `app.eval.__init__` 的循环导入
        build_llm_answerer,
    )

    raw = await _maybe_await(
        search(query=case.question, top_k=settings.top_k, with_rerank=settings.rerank)
    )
    hits = list(getattr(raw, "hits", raw) or [])
    synthesizer = answerer or build_llm_answerer(llm=llm)
    answer = str(await _maybe_await(synthesizer(case.question, hits)) or "")
    return answer, hits


async def _score_variant(
    name: str,
    *,
    dataset: EvalSet,
    settings: RagEvalSettings,
    judge: Judge,
    search: Searcher | None,
    llm: Any,
    answerer: AnswererLike | None,
) -> Variant:
    """跑一个方案的全部用例并聚合（两个方案共用，保证判据与判官完全一致）。"""
    started = time.time()
    triplets: list[tuple[float, float, float]] = []
    fact_scores: list[float] = []
    refusals = 0

    for case in dataset.cases:
        if name == SINGLE_AGENT:
            answer = await run_single_agent(case, llm=llm, answerer=answerer, settings=settings)
            hits: list[Any] = []
        else:
            assert search is not None, "多 Agent 一侧必须给检索函数"
            answer, hits = await run_multi_agent(
                case, search=search, answerer=answerer, settings=settings, llm=llm
            )

        context = context_block(hits)
        judgment = await _maybe_await(
            judge(
                question=case.question,
                reference=case.reference,
                context=context,
                answer=answer,
                key_facts=case.key_facts,
            )
        )
        quality = answer_quality(case=case, answer=answer, context=context)
        retrieval = retrieval_quality(
            case=case,
            retrieved=[hit_source(hit) for hit in hits],
            scores=[hit_score(hit) for hit in hits],
            k=settings.top_k,
        )
        faithful = _combine(quality.faithfulness, judgment.faithful)
        relevant = _combine(quality.relevance, judgment.relevant)
        triplets.append((retrieval.recall, faithful, relevant))
        fact_scores.append(quality.fact_coverage)
        # 「拒答条数」按**答案文本**判，不解析判官的理由文本：
        # 判词的措辞会随判官/模型变化，而 `is_hedged` 是本仓自己的判据（`text.py`）。
        if quality.hedge:
            refusals += 1

    agg: Aggregate = aggregate(name, triplets, thresholds=settings.thresholds())
    notes: list[str] = []
    if name == SINGLE_AGENT:
        notes.append("这条路**不给检索材料**（一个大 prompt 直接答，见 compare.py docstring）")
    else:
        notes.append("本仓的真实链路：检索命中片段 → 只据此合成（`rag_node` + 合成模板）")
    return Variant(
        name=name,
        label="单 Agent（一个大 prompt）" if name == SINGLE_AGENT else "多 Agent（router→rag→synthesis）",
        count=agg.count,
        recall=agg.recall,
        faithfulness=agg.faithfulness,
        answer_relevancy=agg.answer_relevancy,
        key_fact_hit_rate=mean(fact_scores),
        refusals=refusals,
        elapsed_ms=(time.time() - started) * 1000,
        notes=notes,
    )


async def compare_single_vs_multi(
    dataset: EvalSet,
    *,
    search: Searcher,
    settings: RagEvalSettings | None = None,
    judge: Judge | None = None,
    llm: Any = None,
    answerer: AnswererLike | None = None,
) -> Comparison:
    """跑「单 Agent vs 多 Agent」对照（`14.eval.md` §2 的 `--suite compare`）。"""
    settings = settings or RagEvalSettings(suite="compare")
    settings.validate()
    active_judge = judge if judge is not None else build_judge(settings.judge)

    multi = await _score_variant(
        MULTI_AGENT,
        dataset=dataset,
        settings=settings,
        judge=active_judge,
        search=search,
        llm=llm,
        answerer=answerer,
    )
    single = await _score_variant(
        SINGLE_AGENT,
        dataset=dataset,
        settings=settings,
        judge=active_judge,
        search=search,
        llm=llm,
        answerer=answerer,
    )

    delta = {
        "faithfulness": multi.faithfulness - single.faithfulness,
        "answer_relevancy": multi.answer_relevancy - single.answer_relevancy,
        "key_fact_hit_rate": multi.key_fact_hit_rate - single.key_fact_hit_rate,
        "recall": multi.recall - single.recall,
    }
    # 判据只看「质量维」的增益：Recall 是检索层独有的量（单 Agent 那条路没有检索，
    # 它的 recall 恒为 0 —— 那不是「多 Agent 的检索比单 Agent 好」，
    # 那是定义上的差别，拿它当成功判据等于用一条同义反复卡 CI）。
    gain = max(delta["faithfulness"], delta["key_fact_hit_rate"])
    threshold = float(settings.min_multi_agent_gain)
    passed = gain >= threshold
    verdict = (
        f"多 Agent 在质量维上的最大增益 {gain:+.3f}"
        f"（Faithfulness {delta['faithfulness']:+.3f}，关键事实命中率 {delta['key_fact_hit_rate']:+.3f}）"
        f"，阈值 {threshold:.3f} → {'通过' if passed else '未通过'}"
    )
    return Comparison(
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        variants=[multi, single],
        delta=delta,
        verdict=verdict,
        passed=passed,
        notes=[
            "两边的判据、判官、用例集完全相同；唯一差别是「消息里有没有检索材料」。",
            "Recall 一列对单 Agent 是**定义上的 0**（它没有检索环节），不要当成检索质量的对比。",
        ],
    )


__all__ = [
    "MULTI_AGENT",
    "SINGLE_AGENT",
    "SINGLE_AGENT_SYSTEM",
    "Comparison",
    "Variant",
    "compare_single_vs_multi",
    "run_multi_agent",
    "run_single_agent",
]