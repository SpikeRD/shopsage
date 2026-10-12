"""评测报告：**数据模型 + 落盘 + Markdown 渲染**（`14.eval.md` §2 的 `eval/report`）。

## 为什么报告要落成文件（而不是只打印到终端）

`14.eval.md` §8 的验收是「低于阈值即阻断合并」+「证据链接 `eval/report#__`」。
两条都要求**这一次运行**有一份可被别的东西引用的产物：

* CI 的阻断判据要和人工看的那份东西**是同一份**（否则「CI 说 0.68、我看着 0.71」
  这种对话每两周发生一次）；
* 报告要能被 diff —— 「这次比上次掉了哪几维」是评测真正要回答的问题，
  而它需要两份可比较的 JSON。

所以 `Report` 同时有两副面孔：**JSON**（机器读，含逐条明细）与
**Markdown**（人读，含汇总 + 失败样本 + 判据明细）。两者从同一个对象渲染出来，
不可能不一致 —— 这正是把「渲染」和「计算」分成两个模块的理由。

## 报告里必须有而容易漏掉的五样

1. **数据集版本与用例条数**：`0.93` 在 v1 与 v2 上是两个结论（见 `dataset.py`）；
2. **阈值**：读报告的人必须知道分是怎么判的（否则「通过了」没有意义）；
3. **判官身份**：`rules` / `strict` / `llm:<模型>` —— 同一份答案在不同判官下分数不同；
4. **失败样本的判据明细**：只给分数时，掉分要复跑才知道为什么（而那一次结果可能不同）；
5. **知识库指纹**：`recall` 掉分的第一嫌疑是语料变了，而不是代码变了。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from app.eval.metrics import Aggregate
from app.eval.settings import RagEvalSettings

#: 报告 schema 版本（外部工具消费 JSON 时用它判断字段有没有变）
REPORT_SCHEMA: Final[str] = "shopsage.eval-report/v1"

#: Markdown 里逐条明细的**失败样本上限**（报告给人看，200 条明细没人读）
MAX_FAILING_ROWS: Final[int] = 12


class ReportError(ValueError):
    """报告写入/读取失败（目录不可写、文件不是本 schema）。"""


@dataclass
class Report:
    """一次评测运行的完整结果。

    `rows` 是**逐条明细**（`CaseRow`），`groups` 是聚合（总体 / 按标签 / 按预期route）。
    两个都留着：聚合回答「变好了还是变坏了」，明细回答「哪一条坏了、为什么」。
    """

    suite: str
    kind: str
    dataset_name: str
    dataset_version: str
    judge: str
    settings: dict[str, Any]
    #: 答案从哪来（`context` = 片段原文、`extract` = 挑句、`llm:<模型>` = 真生成）。
    #: ⚠️ 必须进报告：`Faithfulness` 在三种合成方式下的含义**完全不同**
    #: （见 `pipeline.py` docstring 的对照表），不写出来就会有人拿不可比的分数做对比。
    answerer: str = "context"
    rows: list[dict[str, Any]] = field(default_factory=list)
    groups: list[Aggregate] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    elapsed_ms: float = 0.0
    corpus_fingerprint: str = ""
    notes: list[str] = field(default_factory=list)
    #: 对照实验（单 Agent vs 多 Agent）的结果；没有跑对照时为空
    comparison: dict[str, Any] = field(default_factory=dict)
    schema: str = REPORT_SCHEMA

    # ── 取值 ────────────────────────────────────────────────────────────
    def overall(self) -> Aggregate:
        """总体聚合。

        ⚠️ `compare` 套件**没有 `rows`**（它只产出一张对照表，不产出逐条明细）——
        这时退回「第一个方案那一行」（本仓是 `multi_agent`）。
        不退回的后果本仓实测过：对照报告的第 1 节汇总表全是 0.000 与「用例条数 0」，
        而第 3 节的对照表里明明写着一串 1.000 —— 同一份报告自相矛盾，
        读的人第一反应是「评测坏了」（见 runbook §10.1 坑 6）。
        """
        for group in self.groups:
            if group.name == "全部":
                return group
        variants = list(self.comparison.get("variants") or [])
        if variants:
            first = variants[0]
            thresholds = dict(self.settings.get("thresholds") or {})
            count = int(first.get("count") or 0)
            return Aggregate(
                name="全部",
                count=count,
                recall=float(first.get("recall") or 0.0),
                faithfulness=float(first.get("faithfulness") or 0.0),
                answer_relevancy=float(first.get("answer_relevancy") or 0.0),
                passed={
                    "recall": count if float(first.get("recall") or 0.0) >= thresholds.get("recall", 0.0) else 0,
                    "faithfulness": (
                        count
                        if float(first.get("faithfulness") or 0.0) >= thresholds.get("faithfulness", 0.0)
                        else 0
                    ),
                    "answer_relevancy": (
                        count
                        if float(first.get("answer_relevancy") or 0.0)
                        >= thresholds.get("answer_relevancy", 0.0)
                        else 0
                    ),
                },
            )
        return Aggregate(name="全部", count=0, recall=0.0, faithfulness=0.0, answer_relevancy=0.0)

    def failing_rows(self) -> list[dict[str, Any]]:
        """没通过任何一维的样本（**报告的主角**：通过率只用来卡 CI，样本才用来改东西）。"""
        return [row for row in self.rows if not row.get("passed")]

    def summary_line(self) -> str:
        overall = self.overall()
        return (
            f"{self.suite}（{self.dataset_name} v{self.dataset_version}）"
            f" {overall.count} 条 | Recall {overall.recall:.3f}"
            f" | Faithfulness {overall.faithfulness:.3f}"
            f" | Relevance {overall.answer_relevancy:.3f}"
            f" | 判官={self.judge} 合成={self.answerer}"
        )

    # ── 落盘 ────────────────────────────────────────────────────────────
    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "suite": self.suite,
            "kind": self.kind,
            "dataset": {"name": self.dataset_name, "version": self.dataset_version},
            "judge": self.judge,
            "answerer": self.answerer,
            "settings": dict(self.settings),
            "started_at": self.started_at,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "corpus_fingerprint": self.corpus_fingerprint,
            "groups": [group.as_dict() for group in self.groups],
            "comparison": dict(self.comparison),
            "notes": list(self.notes),
            "rows": list(self.rows),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2, default=str)

    def to_markdown(self) -> str:
        return render_markdown(self)


def report_filename(suite: str, *, stamp: float | None = None, suffix: str = "md") -> str:
    """报告文件名：`eval-<suite>-<UTC 时间戳>.<后缀>`。

    时间戳进文件名而不是覆盖同一个文件：评测的价值一半在**对照**，
    而覆盖式落盘会让「上一次的分数是多少」这个问题永远答不出来
    （CI 里尤其如此 —— 每一次运行都在一个新容器里）。
    """
    moment = time.gmtime(stamp if stamp is not None else time.time())
    return f"eval-{suite}-{time.strftime('%Y%m%dT%H%M%SZ', moment)}.{suffix}"


def write_report(report: Report, *, out_dir: str | Path, fmt: str = "markdown") -> Path:
    """把报告写进 `out_dir`，返回真实路径（CLI 会把它打印出来）。"""
    directory = Path(out_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReportError(f"报告目录不可写：{directory}（{exc}）") from exc

    if fmt == "json":
        path = directory / report_filename(report.suite, stamp=report.started_at, suffix="json")
        payload = report.to_json()
    else:
        path = directory / report_filename(report.suite, stamp=report.started_at, suffix="md")
        payload = report.to_markdown()
    try:
        path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"报告写入失败：{path}（{exc}）") from exc
    return path


def load_report(path: str | Path) -> dict[str, Any]:
    """读一份 JSON 报告（`scripts/eval_report.py` 的 `--show` 用）。"""
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReportError(f"报告读不到：{target}（{exc}）") from exc
    except ValueError as exc:
        raise ReportError(f"报告不是合法 JSON：{target}（{exc}）") from exc
    if not isinstance(payload, dict) or payload.get("schema") != REPORT_SCHEMA:
        raise ReportError(
            f"{target} 不是本仓的评测报告（schema={payload.get('schema') if isinstance(payload, dict) else '?'}，"
            f"期望 {REPORT_SCHEMA}）"
        )
    return payload


# ── Markdown 渲染 ────────────────────────────────────────────────────────


def _fmt(value: Any, digits: int = 3) -> str:
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: Report) -> str:
    """渲染人读的报告（结构见模块 docstring 的「五样必须有的东西」）。"""
    overall = report.overall()
    thresholds = dict(report.settings.get("thresholds") or {})
    lines: list[str] = []

    lines.append(f"# 评测报告 — {report.suite}（{report.dataset_name} v{report.dataset_version}）")
    lines.append("")
    lines.append(
        f"> 生成于 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report.started_at))}"
        f"（UTC+8 本地时区），耗时 {report.elapsed_ms / 1000:.1f}s，"
        f"判官 `{report.judge}`，合成 `{report.answerer}`。"
        "阈值与判据口径见 `docs/runbook/14.eval-runbook.md` §4。"
    )
    lines.append("")

    # ① 汇总表（阈值与被测值并排 —— 只写分数时没人知道算不算通过）
    lines.append("## 1. 汇总")
    lines.append("")
    lines.append("| 指标 | 实测 | 阈值 | 通过 | 说明 |")
    lines.append("|---|---|---|---|---|")
    rows = (
        ("Recall@K", overall.recall, thresholds.get("recall"), "该检索到的有没有检索到"),
        ("Faithfulness", overall.faithfulness, thresholds.get("faithfulness"), "答案是否忠于检索材料（不编造）"),
        ("Answer Relevance", overall.answer_relevancy, thresholds.get("answer_relevancy"), "答案是否切题"),
    )
    for name, value, threshold, note in rows:
        ok = threshold is None or value >= float(threshold)
        lines.append(
            f"| {name} | {_fmt(value)} | {_fmt(threshold) if threshold is not None else '—'} | "
            f"{'✅' if ok else '✗'} | {note} |"
        )
    lines.append("")
    lines.append(f"用例条数 **{overall.count}**；逐维通过条数 "
                 f"Recall {overall.passed.get('recall', 0)} "
                 f"/ Faithfulness {overall.passed.get('faithfulness', 0)} "
                 f"/ Relevance {overall.passed.get('answer_relevancy', 0)}。")
    if report.corpus_fingerprint:
        lines.append("")
        lines.append(f"知识库指纹：`{report.corpus_fingerprint}`（换语料后分数变化要归因到它，而不是代码）。")
    if report.notes:
        lines.append("")
        for note in report.notes:
            lines.append(f"- {note}")
    lines.append("")

    # ② 分组（按标签 / 按预期 route）
    if len(report.groups) > 1:
        lines.append("## 2. 分组")
        lines.append("")
        lines.append("| 分组 | 条数 | Recall | Faithfulness | Relevance |")
        lines.append("|---|---|---|---|---|")
        for group in report.groups:
            lines.append(
                f"| {group.name} | {group.count} | {_fmt(group.recall)} | "
                f"{_fmt(group.faithfulness)} | {_fmt(group.answer_relevancy)} |"
            )
        lines.append("")

    # ③ 对照实验（单 Agent vs 多 Agent）
    if report.comparison:
        lines.append("## 3. 单 Agent vs 多 Agent")
        lines.append("")
        lines.append(_render_comparison(report.comparison))
        lines.append("")

    # ④ 失败样本（判据明细 —— 这一节才是「改哪里」的入口）
    failing = report.failing_rows()
    has_comparison = bool(report.comparison)
    lines.append(f"## {4 if has_comparison else 3}. 失败样本（{len(failing)} 条）")
    lines.append("")
    if not failing:
        lines.append("本次没有失败样本（逐维全部达到阈值）。")
        lines.append("")
    else:
        for row in failing[:MAX_FAILING_ROWS]:
            lines.append(f"### `{row.get('id')}` {row.get('question', '')}")
            lines.append("")
            lines.append(
                f"- 指标：Recall {_fmt(row.get('recall'))} · Faithfulness {_fmt(row.get('faithfulness'))}"
                f" · Relevance {_fmt(row.get('answer_relevancy'))}"
            )
            if row.get("missing_sources"):
                lines.append(f"- 漏检文档：{row['missing_sources']}（检索到：{row.get('retrieved_sources')}）")
            if row.get("missing_facts"):
                lines.append(f"- 未命中事实点：{row['missing_facts']}")
            if row.get("unsupported_numbers"):
                lines.append(f"- 材料里没有的数字：{row['unsupported_numbers']}（**编造信号**）")
            judgement = row.get("judge") or {}
            if judgement.get("reason"):
                lines.append(f"- 判官：{judgement['reason']}（{judgement.get('judge', '?')}）")
            if row.get("answer"):
                lines.append("")
                lines.append("```text")
                lines.append(str(row["answer"])[:600])
                lines.append("```")
            lines.append("")
        if len(failing) > MAX_FAILING_ROWS:
            lines.append(f"（其余 {len(failing) - MAX_FAILING_ROWS} 条见 JSON 报告的 `rows`。）")
            lines.append("")

    # ⑤ 逐条明细一览（只给阈值边界附近的样本，避免报告变成长表格）
    lines.append(f"## {5 if has_comparison else 4}. 逐条一览")
    lines.append("")
    lines.append("| 用例 | Recall | Faithfulness | Relevance | 事实点 |")
    lines.append("|---|---|---|---|---|")
    for row in report.rows:
        lines.append(
            f"| `{row.get('id')}` | {_fmt(row.get('recall'), 2)} | {_fmt(row.get('faithfulness'), 2)}"
            f" | {_fmt(row.get('answer_relevancy'), 2)} | "
            f"{len(row.get('matched_facts') or [])}/{len(row.get('matched_facts') or []) + len(row.get('missing_facts') or [])} |"
        )
    lines.append("")
    lines.append(
        "> 报告由 `app/eval/report.py` 从一份对象渲染出来 —— JSON 与 Markdown 不可能不一致。"
        "阈值、判据公式与三指标的解释见 `docs/runbook/14.eval-runbook.md`。"
    )
    return "\n".join(lines)


def _render_comparison(comparison: dict[str, Any]) -> str:
    """渲染对照表（单 Agent vs 多 Agent）。"""
    lines: list[str] = []
    variants = list(comparison.get("variants") or [])
    if not variants:
        return "（没有对照数据）"
    lines.append("| 方案 | Recall | Faithfulness | Relevance | 关键事实命中率 | 拒答条数 |")
    lines.append("|---|---|---|---|---|---|")
    for item in variants:
        lines.append(
            f"| {item.get('name')} | {_fmt(item.get('recall'))} | {_fmt(item.get('faithfulness'))}"
            f" | {_fmt(item.get('answer_relevancy'))} | {_fmt(item.get('key_fact_hit_rate'))}"
            f" | {item.get('refusals', 0)} |"
        )
    delta = comparison.get("delta") or {}
    lines.append("")
    lines.append(
        f"增益（多 Agent − 单 Agent）：Faithfulness {_fmt(delta.get('faithfulness'))}"
        f" · Relevance {_fmt(delta.get('answer_relevancy'))}"
        f" · 关键事实命中率 {_fmt(delta.get('key_fact_hit_rate'))}"
    )
    verdict = comparison.get("verdict", "")
    if verdict:
        lines.append("")
        lines.append(f"**结论**：{verdict}")
    return "\n".join(lines)


__all__ = [
    "MAX_FAILING_ROWS",
    "REPORT_SCHEMA",
    "Report",
    "ReportError",
    "load_report",
    "render_markdown",
    "report_filename",
    "write_report",
]
