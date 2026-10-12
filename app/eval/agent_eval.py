"""Agent 评测：**跑完整图，判成功率 + 关键事实命中率**（`14.eval.md` §6 第 3 步）。

## 与 `rag_eval.py` 的关系（别把两件事混起来）

| | `rag_eval` | `agent_eval`（本模块） |
|---|---|---|
| 被测对象 | 检索层（`hybrid_search`） | **整张图**（`router → rag → synthesis` + guardrail） |
| 变量控制在 | 只有检索与合成两步 | 路由、权限注入、护栏、材料边界全在里头 |
| 多出来的一维 | —— | **路由准确率**（`route` 判对了没有） |
| 典型用途 | 「改了分块策略，检索变好了吗」 | 「改了 prompt，端到端答得更好了吗」 |

`14.eval.md` §4 第 2 条把「成功率与关键事实命中率」列在 Agent 层，本模块就落这两条：

* **成功率** = 「这一轮正常答完 + 逐维达到阈值」的条数占比。
  ⚠️ 它把 `route` 也算进去：**路由错的轮次不可能成功**（该查知识库却生成了 SQL），
  而只看答案分数时，一次「碰巧答对」会掩盖路由已经坏了 —— 那是第 7 步最贵的 bug。
* **关键事实命中率** = 全部用例的 `fact_coverage` 平均（`metrics.AnswerQuality`）。

## 为什么必须注入 checkpointer

`compile_graph(checkpointer=None)` 在图这一层是合法的（跑得通），但那样
`aget_state` 读不到东西、`run_turn` 的断点重放也失去意义。评测跑的是**生产同款**的图，
所以本模块强制要求调用方给一个 saver（内存 saver 就够了 —— 评测不需要跨进程恢复）。

## 为什么每轮必须换 `thread_id`

评测的每一轮是**独立**的一条用例，而 `thread_id` 就是会话边界。
复用同一个 thread 会让上一轮的 `messages` 进入这一轮的上下文 ——
症状是「后面的用例莫名其妙答得更好或更差」，而根因在评测自己的接线里。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Final

from app.eval.dataset import EvalCase, EvalSet
from app.eval.judge import Judge, build_judge
from app.eval.metrics import RECALL_NA, Aggregate, aggregate, answer_quality, retrieval_quality
from app.eval.pipeline import hit_score, hit_source
from app.eval.report import Report
from app.eval.settings import RagEvalSettings

logger = logging.getLogger(__name__)

#: 送进判分材料的 SQL 行数上限（与 `agents/synthesis.PROMPT_ROWS` 同口径：
#: 合成节点只渲染前 20 行，判分材料多了就会把「模型没看到的行」也算成它的依据）。
PROMPT_ROWS: Final[int] = 20

#: 一行的「成功」判据里包含路由（理由见模块 docstring）
SUCCESS_METRICS: Final[tuple[str, ...]] = ("route", "recall", "faithfulness", "answer_relevancy")


@dataclass
class AgentOutcome:
    """一条 agent 用例的完整结果（比 `CaseOutcome` 多三样：route / refusals / 路径）。"""

    case_id: str
    question: str
    role: str = "customer"
    expected_route: str = "rag"
    actual_route: str = ""
    route_ok: bool = True
    expected_sources: list[str] = field(default_factory=list)
    retrieved_sources: list[str] = field(default_factory=list)
    missing_sources: list[str] = field(default_factory=list)
    recall: float = 0.0
    answer: str = ""
    faithfulness: float = 0.0
    answer_relevancy: float = 0.0
    fact_coverage: float = 0.0
    matched_facts: list[str] = field(default_factory=list)
    missing_facts: list[str] = field(default_factory=list)
    unsupported_numbers: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    judge: dict[str, Any] = field(default_factory=dict)
    refusals: list[str] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    blocked: bool = False
    #: 这一步是否「答完了」（不是被兜底话术收场、不是空回答）
    delivered: bool = False
    success: bool = False
    failed_metrics: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    tags: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "question": self.question,
            "role": self.role,
            "expected_route": self.expected_route,
            "actual_route": self.actual_route,
            "route_ok": bool(self.route_ok),
            "expected_sources": list(self.expected_sources),
            "retrieved_sources": list(self.retrieved_sources),
            "missing_sources": list(self.missing_sources),
            "recall": round(self.recall, 4),
            "answer": self.answer,
            "faithfulness": round(self.faithfulness, 4),
            "answer_relevancy": round(self.answer_relevancy, 4),
            "fact_coverage": round(self.fact_coverage, 4),
            "matched_facts": list(self.matched_facts),
            "missing_facts": list(self.missing_facts),
            "unsupported_numbers": list(self.unsupported_numbers),
            "findings": list(self.findings),
            "judge": dict(self.judge),
            "refusals": list(self.refusals),
            "visited": list(self.visited),
            "blocked": bool(self.blocked),
            "delivered": bool(self.delivered),
            "success": bool(self.success),
            "failed_metrics": list(self.failed_metrics),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "tags": list(self.tags),
            "note": self.note,
        }


def material_for_judging(state: dict[str, Any]) -> str:
    """**这一轮合成时真正拿到的全部材料**（Faithfulness 的判据来源）。

    ## ⚠️ 为什么不能只拼 `docs`（本仓实测踩到的第三类坑）

    第一版只把 `state["docs"]`（知识库命中）当作材料。于是**所有 `data` 分支的用例**
    Faithfulness 恒为 0.6 —— 因为答案里的订单数、退款额来自 **SQL 查询结果**
    （`state["rows"]`），而它们不在 `docs` 里（`data` 分支根本不检索知识库）。
    症状极具误导性：报告说「模型编造了数字」，而实际上模型逐字引用了查询结果。

    合成节点（`agents/synthesis.py::_material_block`）看到的材料有**五类**，
    这里必须与它对齐（少了任何一类，对应分支的 Faithfulness 都会假性偏低）：

    | 来源 | 谁写 | 出现在哪条分支 |
    |---|---|---|
    | `docs`（知识库片段，带出处） | `rag_node` | rag |
    | `rows`（SQL 查询结果） | `data_node` | data / analysis |
    | `analysis`（对内解读的结论与要点） | `analysis_node` | analysis |
    | `refusals`（没拿到的材料） | 各节点 / 图级兜底 | 全部 |
    | `sql`（内部参考，仅内部模板） | `data_node` | data / analysis（对客不出现） |

    ⚠️ 与 `agents/synthesis.py` 的**唯一差别**：这里**不区分角色**，一律把 SQL 也算进材料。
    那是刻意的：判分要看的是「答案有没有依据」，而依据是不是允许展示给客户，
    是 `synthesis` 的**材料过滤**该管的事（对客模板根本不渲染 SQL）——
    评测在这里复刻过滤，只会让「对客答案里出现了内部字段」这个**真问题**判不出来。
    """
    from app.agents.agent_llm import format_rows

    parts: list[str] = []
    docs = list(state.get("docs") or [])
    for doc in docs:
        text = getattr(doc, "text", None)
        if text is None and isinstance(doc, dict):
            text = doc.get("text")
        parts.append(str(text or ""))

    rows = list(state.get("rows") or [])
    if rows:
        parts.append(format_rows(rows, limit=PROMPT_ROWS))

    analysis = state.get("analysis") or {}
    if isinstance(analysis, dict) and analysis:
        head = str(analysis.get("headline") or "")
        bullets = [str(item) for item in (analysis.get("bullets") or [])]
        caveats = [str(item) for item in (analysis.get("caveats") or [])]
        parts.append("\n".join([head, *bullets, *caveats]))

    for reason in state.get("refusals") or []:
        parts.append(str(reason))
    if state.get("sql"):
        parts.append(str(state["sql"]))
    return "\n\n".join(part for part in parts if part.strip())


def score_agent_row(
    *,
    case: EvalCase,
    state: dict[str, Any],
    visited: Sequence[str],
    settings: RagEvalSettings,
    judge: Judge | None = None,
    judgment: Any = None,
    elapsed_ms: float = 0.0,
) -> AgentOutcome:
    """把**一轮图的结果**判成分数（纯函数：给定 state 就能复算，不必重跑图）。

    这是本模块最值钱的一处拆分：图的运行有副作用（写 checkpoint、调模型），
    而判分必须是可复算的 —— 否则「这条为什么算失败」只能靠重跑，而重跑结果会变。
    """
    from app.eval.rag_eval import _combine  # 同一套合并口径（两处各写一份必然漂移）

    docs = list(state.get("docs") or [])
    answer = str(state.get("answer") or "")
    route = str(state.get("route") or "")
    refusals = [str(item) for item in (state.get("refusals") or [])]

    # ⚠️ Faithfulness 的判据材料是**五类来源合起来的**（docs/rows/analysis/refusals/sql），
    # 不是只有 `docs` —— 只拼 `docs` 会让所有 `data` 分支的用例假性掉分（见 `material_for_judging`）。
    context = material_for_judging(state)

    retrieval = retrieval_quality(
        case=case,
        retrieved=[hit_source(doc) for doc in docs],
        scores=[hit_score(doc) for doc in docs],
        k=settings.top_k,
        elapsed_ms=elapsed_ms,
    )
    quality = answer_quality(case=case, answer=answer, context=context)
    faithful = quality.faithfulness if judgment is None else _combine(quality.faithfulness, judgment.faithful)
    relevant = quality.relevance if judgment is None else _combine(quality.relevance, judgment.relevant)

    route_ok = not case.expected_route or route == case.expected_route
    # 「答完了」= 有回答 + 没被护栏/兜底拦下 + 没有 refusal 记录。
    # 三者都要：「被拦下但有话术」与「没答上但没报错」是评测里最容易混成一类的两种失败。
    delivered = bool(answer.strip()) and not bool(state.get("blocked")) and not refusals

    thresholds = settings.thresholds()
    # Recall 只在「这条用例本来就应该检索到东西」时参与判分（见 `metrics.recall_for`）：
    # `data` / `final` 分支与 `expect_empty` 的用例没有期望来源，把它们算成「Recall 0 分」
    # 会让一次全绿的运行显示成 50% 成功率 —— 本仓实测就是这样，见 runbook §7.3。
    recall_applicable = bool(case.expected_sources) and not case.expect_empty
    failed = [
        name
        for name, ok in (
            ("route", route_ok),
            ("recall", (not recall_applicable) or retrieval.recall >= thresholds["recall"]),
            ("faithfulness", faithful >= thresholds["faithfulness"]),
            ("answer_relevancy", relevant >= thresholds["answer_relevancy"]),
        )
        if not ok
    ]
    return AgentOutcome(
        case_id=case.id,
        question=case.question,
        role=case.role,
        expected_route=case.expected_route,
        actual_route=route,
        route_ok=route_ok,
        expected_sources=list(case.expected_sources),
        retrieved_sources=list(retrieval.retrieved),
        missing_sources=list(retrieval.missing),
        recall=retrieval.recall,
        answer=answer,
        faithfulness=faithful,
        answer_relevancy=relevant,
        fact_coverage=quality.fact_coverage,
        matched_facts=list(quality.matched_facts),
        missing_facts=list(quality.missing_facts),
        unsupported_numbers=list(quality.unsupported_numbers),
        findings=[
            {"name": name, "passed": passed, "detail": detail} for name, passed, detail in quality.findings
        ],
        judge=(judgment.as_dict() if judgment is not None else {}),
        refusals=refusals,
        visited=[str(item) for item in visited],
        blocked=bool(state.get("blocked")),
        delivered=delivered,
        success=(not failed) and delivered,
        failed_metrics=failed,
        elapsed_ms=elapsed_ms,
        tags=list(case.tags),
        note=case.note,
    )


async def evaluate_agent(
    dataset: EvalSet,
    *,
    graph: Any,
    settings: RagEvalSettings | None = None,
    judge: Judge | None = None,
    thread_prefix: str = "eval",
) -> tuple[Report, list[AgentOutcome]]:
    """跑完一份 agent golden set，返回 `(报告, 逐条结果)`。

    `graph` 是**已编译**的图（`compile_graph(checkpointer=...)`）；本模块不负责建图，
    因为「用哪个 checkpointer、注入哪些依赖」是调用方的语义选择（与第 8 步同口径）。
    """
    from app.graph.run import run_turn

    settings = settings or RagEvalSettings(suite="agent")
    settings.validate()
    active_judge = judge if judge is not None else build_judge(settings.judge)

    started = time.time()
    outcomes: list[AgentOutcome] = []
    for index, case in enumerate(dataset.cases):
        t0 = time.perf_counter()
        turn = await run_turn(
            graph,
            {
                "messages": [{"role": "user", "content": case.question}],
                "user_id": case.user_id,
                "role": case.role,
            },
            thread_id=f"{thread_prefix}-{index}-{case.id}",
        )
        state = dict(turn.state or {})
        # 一条 INFO 摘要：它是「为什么这条失败」的第一现场（`-v` 时还能看到节点路径）。
        # 只打计数与布尔，不打答案正文 —— 答案可能含 PII（订单号/手机号），
        # 而评测日志经常被贴进 issue（与第 11 步「prompt 全文绝不进 trace」同一条纪律）。
        logger.info(
            "用例 %s：路径=%s route=%s 命中=%d 拒答=%d 拦截=%s 答案=%d 字",
            case.id,
            "→".join(turn.visited) or "(空)",
            state.get("route"),
            len(state.get("docs") or []),
            len(state.get("refusals") or []),
            bool(state.get("blocked")),
            len(str(state.get("answer") or "")),
        )
        if state.get("refusals"):
            # 拒答原因**必须**打出来：它是「检索没接上」与「知识库里确实没有」的唯一分水岭，
            # 而这两种情况的处置完全不同（前者是接线 bug，后者是数据事实）。
            # 原因文本里不会有 PII（它由 `rag_node` 与工具层生成），可以进日志。
            logger.info("  拒答原因：%s", "；".join(str(item) for item in state["refusals"]))
        context = material_for_judging(state)
        judgment = await _maybe_await(
            active_judge(
                question=case.question,
                reference=case.reference,
                context=context,
                answer=str(state.get("answer") or ""),
                key_facts=case.key_facts,
            )
        )
        outcome = score_agent_row(
            case=case,
            state=state,
            visited=turn.visited,
            settings=settings,
            judge=active_judge,
            judgment=judgment,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )
        outcomes.append(outcome)

    rows = [outcome.as_dict() for outcome in outcomes]
    thresholds = settings.thresholds()
    groups = _groups(outcomes, thresholds=thresholds)
    success_rate = (
        sum(1 for outcome in outcomes if outcome.success) / len(outcomes) if outcomes else 0.0
    )
    fact_rate = (
        sum(outcome.fact_coverage for outcome in outcomes) / len(outcomes) if outcomes else 0.0
    )
    report = Report(
        suite="agent",
        kind=dataset.kind,
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        judge=settings.judge,
        settings={
            **settings.as_dict(),
            "success_rate": round(success_rate, 4),
            "key_fact_hit_rate": round(fact_rate, 4),
        },
        rows=rows,
        groups=groups,
        started_at=started,
        elapsed_ms=(time.time() - started) * 1000,
        corpus_fingerprint=settings.corpus_fingerprint,
    )
    report.notes.append(
        f"成功率 {success_rate:.3f}（{sum(1 for o in outcomes if o.success)}/{len(outcomes)} 条；"
        "成功 = 路由判对 + 逐维达阈值 + 正常答完）；关键事实命中率 {:.3f}".format(fact_rate)
    )
    return report, outcomes


def _groups(outcomes: list[AgentOutcome], *, thresholds: dict[str, float]) -> list[Aggregate]:
    """总体 + 按预期 route 的分组（agent 套件的分组维度是 route，不是标签）。

    Recall 用 `metrics.recall_for` 的口径：`data` / `final` 这类**没有期望来源**的用例
    在这 一维上「不适用」（哨兵值），而不是 0 分 —— 否则一组「本来就不该检索」的用例
    会把整体 Recall 拉下来，看起来像检索坏了（见 runbook §7.3）。
    """
    def triplet(row: AgentOutcome) -> tuple[float, float, float]:
        recall = RECALL_NA if not row.expected_sources else row.recall
        return (recall, row.faithfulness, row.answer_relevancy)

    groups = [aggregate("全部", [triplet(row) for row in outcomes], thresholds=thresholds)]
    by_route: dict[str, list[tuple[float, float, float]]] = {}
    for row in outcomes:
        by_route.setdefault(row.expected_route or "(未指定)", []).append(triplet(row))
    for route in sorted(by_route):
        groups.append(aggregate(f"route:{route}", by_route[route], thresholds=thresholds))
    return groups


async def _maybe_await(value: Any) -> Any:
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


__all__ = [
    "SUCCESS_METRICS",
    "AgentOutcome",
    "evaluate_agent",
    "score_agent_row",
]
