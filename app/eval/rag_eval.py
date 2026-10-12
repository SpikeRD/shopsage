"""RAG 评测：**检索 → 合成 → 判分**（`14.eval.md` §6 第 2 步的落点）。

## 这一层只做编排，不做算法

三件事各自在各自的地方，本模块只是把它们串起来：

| 步骤 | 谁做 | 为什么不在本模块 |
|---|---|---|
| 检索 | `app/rag/retriever.py`（经 `Searcher` 注入） | 评测若自带一份检索实现，测的就不是生产那条路 |
| 判据 | `app/eval/text.py` + `metrics.py`（纯函数） | 判据必须是纯函数才能逐条断言 |
| 判分结论 | `app/eval/judge.py`（可替换） | 规则与 LLM 两套判分的差别只在结论，不在流程 |

## 一条用例的完整流程（每一步都可单独看见）

```
case.question
   │
   ├─► search(question, top_k, rerank)        → hits（含 source/score）
   │        │
   │        └─► retrieval_quality(...)         → recall / matched / missing
   │
   ├─► answerer(question, hits)               → answer（抽取式或真模型）
   │
   ├─► context_block(hits)                    → context（Faithfulness 的判据材料）
   │
   ├─► answer_quality(case, answer, context)  → 规则分 + 判据明细
   │
   └─► judge(question, reference, context, answer, key_facts)   → LLM/严格判官结论
           │
           └─► 合并（见 `_combine`）             → 最终 faithfulness / relevance
```

## 合并口径（**这是本模块唯一的判断，所以要写清理由**）

规则分与判官结论**必须都过**才算这一维通过，但**分数**取两者里**更保守**的那一个：

| 规则 | 判官 | 分数 | 为什么 |
|---|---|---|---|
| 通过 | 通过 | 规则分 | 两边一致，没有争议 |
| 通过 | 不通过 | `min(规则分, 0.5)` | 判官看到了规则看不见的问题（同义改写之外的毛病） |
| 不通过 | 通过 | 规则分（不变） | **规则是否决项**：事实点没命中就是没命中 |
| 不通过 | 不通过 | `min(规则分, 0.5)` | 一致地差 |

「取更保守的那一个」而不是平均：平均会让「规则判 0、判官判 1」变成 0.5 ——
那看起来像「答了一半」，而真相是「事实点一个都没命中」。
`0.5` 这个折半值只用于**表达「未通过」**，它不是一个有语义的分数
（报告里分数与布尔判定是两列，读的人看得到 `✗`）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.eval.dataset import EvalCase, EvalSet
from app.eval.judge import Judge, Judgment, build_judge
from app.eval.metrics import (
    RECALL_NA,
    Aggregate,
    aggregate,
    answer_quality,
    passed_overall,
    retrieval_quality,
)
from app.eval.pipeline import (
    Answerer,
    AnswererLike,
    context_answerer,
    context_block,
    hit_score,
    hit_source,
)
from app.eval.report import Report
from app.eval.settings import RagEvalSettings

logger = logging.getLogger(__name__)

#: 检索函数签名（`app/rag/retriever.hybrid_search` 与 `vector_only_search` 都能满足）。
#: 参数名与第 10 步的门面一致（`query` / `top_k`），所以**生产实现可以直接传进来**。
Searcher = Callable[..., Any]

#: 未通过时分数被压到的上限（见模块 docstring 的合并口径表）
FAILED_SCORE_CAP: Final[float] = 0.5


@dataclass
class CaseOutcome:
    """一条用例的完整结果（报告里的 `rows`；也是 `--only` 复跑时要能对照的那份东西）。"""

    case_id: str
    question: str
    role: str = "customer"
    expected_sources: list[str] = field(default_factory=list)
    retrieved_sources: list[str] = field(default_factory=list)
    missing_sources: list[str] = field(default_factory=list)
    recall: float = 0.0
    hits: int = 0
    top_score: float = 0.0
    answer: str = ""
    faithfulness: float = 0.0
    answer_relevancy: float = 0.0
    fact_coverage: float = 0.0
    matched_facts: list[str] = field(default_factory=list)
    missing_facts: list[str] = field(default_factory=list)
    unsupported_numbers: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    judge: dict[str, Any] = field(default_factory=dict)
    passed: bool = False
    failed_metrics: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    expected_route: str = ""
    actual_route: str = "rag"
    route_ok: bool = True
    #: 这条用例是否声明了「知识库里本来就没有答案」（Recall 因此不适用，见 `metrics.recall_for`）
    expect_empty: bool = False
    tags: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "question": self.question,
            "role": self.role,
            "expected_sources": list(self.expected_sources),
            "retrieved_sources": list(self.retrieved_sources),
            "missing_sources": list(self.missing_sources),
            "recall": round(self.recall, 4),
            "hits": self.hits,
            "top_score": round(self.top_score, 4),
            "answer": self.answer,
            "faithfulness": round(self.faithfulness, 4),
            "answer_relevancy": round(self.answer_relevancy, 4),
            "fact_coverage": round(self.fact_coverage, 4),
            "matched_facts": list(self.matched_facts),
            "missing_facts": list(self.missing_facts),
            "unsupported_numbers": list(self.unsupported_numbers),
            "findings": list(self.findings),
            "judge": dict(self.judge),
            "passed": bool(self.passed),
            "failed_metrics": list(self.failed_metrics),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "expected_route": self.expected_route,
            "actual_route": self.actual_route,
            "route_ok": bool(self.route_ok),
            "expect_empty": bool(self.expect_empty),
            "tags": list(self.tags),
            "note": self.note,
        }


def _combine(rule_value: float, verdict: bool) -> float:
    """把规则分与判官结论合成一个分数（口径见模块 docstring）。"""
    if verdict:
        return rule_value
    return min(rule_value, FAILED_SCORE_CAP)


def _score_row(
    *,
    case: EvalCase,
    hits: Sequence[Any],
    answer: str,
    judgment: Judgment | None,
    settings: RagEvalSettings,
    elapsed_ms: float = 0.0,
) -> CaseOutcome:
    """一条用例的**纯判分**（不检索、不合成）—— 这样判据可以被单独测。

    `hits` 允许是 `RetrievalHit` / `RetrievedDoc` / 普通 dict：只要求它们有
    `text` + `source`（`pipeline._pick` 的口径）。评测不该因为上游换了类型就跑不起来。
    """
    context = context_block(hits)
    retrieval = retrieval_quality(
        case=case,
        retrieved=[hit_source(hit) for hit in hits],
        scores=[hit_score(hit) for hit in hits],
        k=settings.top_k,
        elapsed_ms=elapsed_ms,
    )
    quality = answer_quality(case=case, answer=answer, context=context)

    verdict_faithful = True if judgment is None else bool(judgment.faithful)
    verdict_relevant = True if judgment is None else bool(judgment.relevant)
    faithfulness = _combine(quality.faithfulness, verdict_faithful)
    relevance = _combine(quality.relevance, verdict_relevant)

    thresholds = settings.thresholds()
    # Recall 只在「这条用例本来就应该检索到东西」时参与判分（`metrics.recall_for` 的口径）：
    # `expect_empty` 与没写期望来源的用例没有分母，把它们算成「Recall 0 分」
    # 会让一次正常的运行显示成大面积失败（见 runbook §7.3）。
    recall_applicable = bool(case.expected_sources) and not case.expect_empty
    failed = [
        name
        for name, value in (
            ("recall", retrieval.recall),
            ("faithfulness", faithfulness),
            ("answer_relevancy", relevance),
        )
        if not (name == "recall" and not recall_applicable) and value < thresholds[name]
    ]
    return CaseOutcome(
        case_id=case.id,
        question=case.question,
        role=case.role,
        expected_sources=list(case.expected_sources),
        retrieved_sources=list(retrieval.retrieved),
        missing_sources=list(retrieval.missing),
        recall=retrieval.recall,
        hits=len(hits),
        top_score=retrieval.top_score,
        answer=answer,
        faithfulness=faithfulness,
        answer_relevancy=relevance,
        fact_coverage=quality.fact_coverage,
        matched_facts=list(quality.matched_facts),
        missing_facts=list(quality.missing_facts),
        unsupported_numbers=list(quality.unsupported_numbers),
        findings=[{"name": name, "passed": passed, "detail": detail} for name, passed, detail in quality.findings],
        judge=(judgment.as_dict() if judgment is not None else {}),
        passed=not failed,
        failed_metrics=failed,
        elapsed_ms=elapsed_ms,
        expected_route=case.expected_route,
        # 把 `expect_empty` 带出来，聚合时才不必去「重新推断这条适不适用于 Recall」
        # （同一份数据上算两次同一个量，是评测里最容易产生分歧的地方）
        expect_empty=bool(case.expect_empty),
        tags=list(case.tags),
        note=case.note,
    )


async def _maybe_await(value: Any) -> Any:
    """同步返回值与 awaitable 都吃（理由见 `evaluate_case` 里的注释）。"""
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


async def evaluate_case(
    case: EvalCase,
    *,
    search: Searcher,
    settings: RagEvalSettings,
    judge: Judge | None = None,
    answerer: AnswererLike | None = None,
) -> CaseOutcome:
    """跑一条用例：检索 → 合成 → 判分。"""
    started = time.perf_counter()
    # ⚠️ 检索可能是**异步**的（生产实现 `Retriever.search` 就是 async），
    # 而离线测试里的假检索是同步函数。这里两种都吃 ——
    # 强制只认一种会让「评测本身可测」这条要求落空（见 `_maybe_await` 的说明）。
    raw = await _maybe_await(search(query=case.question, top_k=settings.top_k, with_rerank=settings.rerank))
    hits = list(getattr(raw, "hits", raw) or [])
    elapsed_ms = float(getattr(raw, "elapsed_ms", 0.0) or (time.perf_counter() - started) * 1000)

    synthesizer = answerer or context_answerer()
    answer = str(await _maybe_await(synthesizer(case.question, hits)) or "")

    judgment: Judgment | None = None
    if judge is not None:
        judgment = await _maybe_await(
            judge(
                question=case.question,
                reference=case.reference,
                context=context_block(hits),
                answer=answer,
                key_facts=case.key_facts,
            )
        )
    return _score_row(
        case=case,
        hits=hits,
        answer=answer,
        judgment=judgment,
        settings=settings,
        elapsed_ms=elapsed_ms,
    )


def _groups(rows: list[CaseOutcome], *, thresholds: dict[str, float]) -> list[Aggregate]:
    """总体 + 按标签 + 按预期 route 三组聚合（报告 §2 的分组表）。"""
    triplet = _triplet
    groups = [aggregate("全部", [triplet(row) for row in rows], thresholds=thresholds)]

    by_tag: dict[str, list[tuple[float, float, float]]] = {}
    for row in rows:
        for tag in row.tags or ["(无标签)"]:
            by_tag.setdefault(tag, []).append(triplet(row))
    for tag in sorted(by_tag):
        if len(by_tag[tag]) < 2:
            # 只出现一次的标签不单列：一行一条的分组表没有信息量，只会把报告撑长
            continue
        groups.append(aggregate(f"标签:{tag}", by_tag[tag], thresholds=thresholds))
    return groups


def _triplet(row: CaseOutcome) -> tuple[float, float, float]:
    """一条结果的三元组（Recall 用哨兵值表达「不适用」，见 `metrics.recall_for`）。

    这里不再重算 Recall，而是看 `CaseOutcome.expect_empty`（它在判分时已经写好了）——
    在**同一份数据上算两次同一个量**是评测里最容易产生分歧的地方。
    """
    recall = RECALL_NA if row.expect_empty or not row.expected_sources else row.recall
    return (recall, row.faithfulness, row.answer_relevancy)


async def evaluate_rag(
    dataset: EvalSet,
    *,
    search: Searcher,
    settings: RagEvalSettings | None = None,
    judge: Judge | None = None,
    answerer: AnswererLike | None = None,
) -> Report:
    """跑完一份 RAG golden set，返回 `Report`（`14.eval.md` §2 的 `--suite rag`）。"""
    settings = settings or RagEvalSettings()
    settings.validate()
    active_judge = judge if judge is not None else build_judge(settings.judge)

    started = time.time()
    rows: list[CaseOutcome] = []
    for case in dataset.cases:
        rows.append(
            await evaluate_case(case, search=search, settings=settings, judge=active_judge, answerer=answerer)
        )

    thresholds = settings.thresholds()
    groups = _groups(rows, thresholds=thresholds)
    report = Report(
        suite="rag",
        kind=dataset.kind,
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        judge=settings.judge,
        settings=settings.as_dict(),
        rows=[row.as_dict() for row in rows],
        groups=groups,
        started_at=started,
        elapsed_ms=(time.time() - started) * 1000,
        corpus_fingerprint=settings.corpus_fingerprint,
    )
    overall = report.overall()
    if not passed_overall(overall, thresholds=thresholds):
        report.notes.append(
            f"未达阈值：{overall.as_dict()}（阈值 {thresholds}）—— "
            "逐条原因见「失败样本」一节，判据口径见 runbook §4。"
        )
    return report


__all__ = [
    "FAILED_SCORE_CAP",
    "AnswererLike",
    "CaseOutcome",
    "Searcher",
    "evaluate_case",
    "evaluate_rag",
]
