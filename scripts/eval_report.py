"""评测报告的运维工具（**默认只读**）：看一份报告 / 卡阈值 / 两份对照 / 逐条追因。

```bash
uv run python -m scripts.eval_report --list                       # 列出报告目录里的所有报告
uv run python -m scripts.eval_report --show eval-reports/xxx.json  # 看一份（汇总 + 失败的逐条判据）
uv run python -m scripts.eval_report --verify eval-reports/xxx.json # 卡阈值（CI 的等价动作，退出码 0/1）
uv run python -m scripts.eval_report --diff a.json b.json          # 两份对照（**评测真正要回答的问题**）
uv run python -m scripts.eval_report --show report.json --case return-shipping  # 追一条用例的判据明细
```

## 与 `python -m eval.run` 的分工

`eval/run` **产出**报告；本脚本**消费**已产出的报告。分开的理由有两条：

1. **CI 与人工看的是同一份产物**：`--verify` 与 CI 的阻断判据读的是同一个 JSON，
   于是不会出现「CI 说 0.68、我看着 0.71」这种每周发生一次的对话；
2. **对照要能离线做**：`--diff` 不重跑任何东西（重跑会让你无法确定差异来自改动还是来自随机性 ——
   涉及真模型时尤其如此）。两份 JSON 一摆，差异是**确定的**。

## 三条刻意的取舍

* **默认只读**：不写、不删、不改。要清理报告请自己 `Remove-Item`（与第 13 步
  `cache_report --purge` 同一条纪律：破坏性动作必须显式）。
* **`--diff` 的差异带方向与阈值**：只打印 `0.771 → 0.918` 是不够的 ——
  读的人立刻要问「这算变好了吗」，所以每一行都给出 `Δ` 与是否跨过阈值。
* **`--case` 是排障入口**：报告里的「失败样本」只列前 12 条，而`--case` 能追任意一条，
  并且把**判据明细**（每条判据的通过与否 + 人话理由）与答案原文一起打出来。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

from app.eval.report import REPORT_SCHEMA, ReportError, load_report
from app.eval.run import EXIT_BELOW_THRESHOLD, EXIT_NOT_RUNNABLE, EXIT_OK

#: 默认的报告目录（`eval/run --out-dir` 的常用值）
DEFAULT_DIR: Final[str] = "eval-reports"

#: 阈值不达标时 `--verify` 的退出码（与 `eval/run` 保持一致：CI 只认 0 与非 0）
METRIC_LABELS: Final[dict[str, str]] = {
    "recall": "Recall@K",
    "faithfulness": "Faithfulness",
    "answer_relevancy": "Answer Relevance",
}


def _fmt(value: Any, digits: int = 3) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _overall(payload: dict[str, Any]) -> dict[str, Any]:
    """从 JSON 报告里取总体指标（与 `Report.overall()` 同一口径，但只吃 JSON）。

    ⚠️ 这里**必须**与 `Report.overall()` 保持一致（包括 `compare` 套件退回第一个方案那一行）。
    两处口径分叉的后果是「`--verify` 说通过、CI 说不过」——而那是同一份 JSON。
    所以本函数只做一件事：找 `groups` 里名为「全部」的那一行，找不到就用 `comparison.variants[0]`。
    """
    for group in payload.get("groups") or []:
        if group.get("name") == "全部":
            return group
    variants = (payload.get("comparison") or {}).get("variants") or []
    if variants:
        first = dict(variants[0])
        first["name"] = "全部"
        return first
    return {}


def _thresholds(payload: dict[str, Any]) -> dict[str, float]:
    settings = payload.get("settings") or {}
    thresholds = settings.get("thresholds")
    if isinstance(thresholds, dict) and thresholds:
        return {str(key): float(value) for key, value in thresholds.items()}
    # 老报告可能没有 `thresholds`（schema 演进）：给一组保守默认值，并明确说出来
    # —— 静默用一组「很松」的默认值会让 `--verify` 变成永远通过。
    return {"recall": 0.7, "faithfulness": 0.7, "answer_relevancy": 0.6}


def find_reports(directory: str | Path) -> list[Path]:
    """列出目录里的评测报告（按修改时间倒序：最近那次在最上面）。"""
    path = Path(directory)
    if not path.exists():
        return []
    reports = sorted(path.glob("eval-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    return reports


def cmd_list(directory: str) -> int:
    reports = find_reports(directory)
    if not reports:
        print(f"{directory} 里没有评测报告（文件名形如 eval-<suite>-<UTC 时间戳>.json）。")
        print("先跑一次：`uv run python -m eval.run --suite rag --out-dir eval-reports`")
        return EXIT_OK
    print(f"{directory} 里的报告（{len(reports)} 份，最近的在前）：")
    print(f"  {'文件':<44} {'套件':<8} {'条数':>5} {'Recall':>7} {'Faith':>7} {'Relev':>7} 判官")
    for item in reports:
        try:
            payload = load_report(item)
        except ReportError as exc:
            print(f"  {item.name:<44} —— 读不了：{exc}")
            continue
        overall = _overall(payload)
        print(
            f"  {item.name:<44} {payload.get('suite', '?'):<8} {overall.get('count', 0):>5}"
            f" {_fmt(overall.get('recall')):>7} {_fmt(overall.get('faithfulness')):>7}"
            f" {_fmt(overall.get('answer_relevancy')):>7} {payload.get('judge', '?')}"
        )
    return EXIT_OK


def cmd_show(path: str, *, case_id: str = "", limit: int = 12) -> int:
    payload = load_report(path)
    overall = _overall(payload)
    thresholds = _thresholds(payload)
    dataset = payload.get("dataset") or {}

    print("=" * 78)
    print(f"报告 {path}")
    print(
        f"  套件={payload.get('suite')} 数据集={dataset.get('name')} v{dataset.get('version')}"
        f" 条数={overall.get('count')}"
    )
    print(
        f"  判官={payload.get('judge')} 合成={payload.get('answerer')}"
        f" 耗时={(payload.get('elapsed_ms') or 0) / 1000:.1f}s"
    )
    fingerprint = payload.get("corpus_fingerprint") or ""
    if fingerprint:
        print(f"  语料指纹={fingerprint}（换语料导致的差异要归因到它）")
    print("=" * 78)

    print(f"\n{'指标':<16} {'实测':>8} {'阈值':>8} 通过  说明")
    notes = {
        "recall": "该检索到的有没有检索到",
        "faithfulness": "答案是否忠于检索材料（不编造）",
        "answer_relevancy": "答案是否切题",
    }
    for metric, label in METRIC_LABELS.items():
        value = float(overall.get(metric) or 0.0)
        threshold = thresholds.get(metric)
        if metric == "recall" and overall.get("recall_applicable") == 0:
            mark, shown = "—", "不适用"
        else:
            mark = "✅" if threshold is None or value >= threshold else "✗"
            shown = _fmt(threshold) if threshold is not None else "—"
        print(f"{label:<16} {_fmt(value):>8} {shown:>8}  {mark}   {notes[metric]}")

    comparison = payload.get("comparison") or {}
    if comparison:
        print("\n单 Agent vs 多 Agent：")
        for item in comparison.get("variants") or []:
            print(
                f"  {item.get('label', item.get('name')):<34} Recall {_fmt(item.get('recall'))}"
                f" · Faithfulness {_fmt(item.get('faithfulness'))}"
                f" · Relevance {_fmt(item.get('answer_relevancy'))}"
                f" · 事实点 {_fmt(item.get('key_fact_hit_rate'))}"
            )
        if comparison.get("verdict"):
            print(f"  结论：{comparison['verdict']}")

    rows = payload.get("rows") or []
    failing = [row for row in rows if not row.get("passed")]
    if case_id:
        matched = [row for row in rows if row.get("id") == case_id]
        if not matched:
            print(f"\n报告里没有用例 {case_id!r}（可用 id：{[row.get('id') for row in rows][:20]}）")
            return EXIT_NOT_RUNNABLE
        for row in matched:
            _print_case(row)
        return EXIT_OK

    print(f"\n失败样本（{len(failing)} / {len(rows)} 条，最多显示 {limit} 条；追单条用 --case <id>）")
    for row in failing[:limit]:
        _print_case(row, compact=True)
    if len(failing) > limit:
        print(f"  （其余 {len(failing) - limit} 条省略）")
    if not failing and rows:
        print("  没有失败样本。")
    return EXIT_OK


def _print_case(row: dict[str, Any], *, compact: bool = False) -> None:
    print(f"\n  [{row.get('id')}] {row.get('question')}")
    print(
        f"    指标 Recall {_fmt(row.get('recall'), 2)} · Faithfulness {_fmt(row.get('faithfulness'), 2)}"
        f" · Relevance {_fmt(row.get('answer_relevancy'), 2)}"
    )
    if row.get("expected_sources") or row.get("retrieved_sources"):
        print(f"    期望来源 {row.get('expected_sources')}")
        print(f"    实际检索 {row.get('retrieved_sources')}（漏：{row.get('missing_sources')}）")
    if row.get("missing_facts"):
        print(f"    未命中事实点 {row['missing_facts']}")
    if row.get("unsupported_numbers"):
        print(f"    材料里没有的数字 {row['unsupported_numbers']}（**编造信号**）")
    if row.get("judge"):
        judge = row["judge"]
        print(f"    判官 {judge.get('judge')}：{judge.get('reason')}")
    for finding in row.get("findings") or []:
        mark = "✅" if finding.get("passed") else "✗"
        print(f"    {mark} {finding.get('name')}：{finding.get('detail')}")
    if compact:
        return
    answer = str(row.get("answer") or "")
    print(f"    答案（{len(answer)} 字）：")
    for line in answer.splitlines()[:8]:
        print(f"      {line}")


def cmd_verify(path: str) -> int:
    """卡阈值：**CI 的等价动作**（退出码 0 = 通过，1 = 低于阈值，2 = 报告不可用）。"""
    payload = load_report(path)
    overall = _overall(payload)
    thresholds = _thresholds(payload)
    count = int(overall.get("count") or 0)
    if count < 1:
        print(f"[不通过] 这份报告一条用例都没跑（{path}）：CI 里这是最常见的假绿。")
        return EXIT_NOT_RUNNABLE

    failed: list[str] = []
    for metric in METRIC_LABELS:
        value = float(overall.get(metric) or 0.0)
        threshold = thresholds.get(metric)
        if threshold is None:
            continue
        if value + 1e-9 < threshold:
            failed.append(f"{metric} {_fmt(value)} < {_fmt(threshold)}")
    if failed:
        print(f"[不通过] {path}：{ '；'.join(failed) }")
        return EXIT_BELOW_THRESHOLD
    print(
        f"[通过] {path}：{count} 条，"
        + "，".join(f"{metric}={_fmt(overall.get(metric))}" for metric in METRIC_LABELS)
    )
    return EXIT_OK


def cmd_diff(before: str, after: str) -> int:
    """两份报告的对照（评测真正要回答的问题：这次改动是变好还是变坏）。"""
    left = load_report(before)
    right = load_report(after)
    thresholds = _thresholds(right) or _thresholds(left)
    l_overall, r_overall = _overall(left), _overall(right)

    print(f"对照：\n  A = {before}（{left.get('dataset', {}).get('name')} v{left.get('dataset', {}).get('version')}）")
    print(f"  B = {after}（{right.get('dataset', {}).get('name')} v{right.get('dataset', {}).get('version')}）")

    # 数据集版本不同时**显式警告**：分数只有和「哪一版数据集」绑在一起才有意义
    if (left.get("dataset") or {}) != (right.get("dataset") or {}):
        print("\n⚠️ 两份报告的数据集不同（名字或版本）：分数**不可直接比较**，先确认这是有意的。")
    if (left.get("judge"), left.get("answerer")) != (right.get("judge"), right.get("answerer")):
        print(
            f"\n⚠️ 判官/合成方式不同：A = {left.get('judge')}/{left.get('answerer')}，"
            f"B = {right.get('judge')}/{right.get('answerer')} —— 差异里混进了判分口径的变化。"
        )

    print(f"\n{'指标':<16} {'A':>8} {'B':>8} {'Δ':>8}  阈值  方向")
    for metric, label in METRIC_LABELS.items():
        a = float(l_overall.get(metric) or 0.0)
        b = float(r_overall.get(metric) or 0.0)
        delta = b - a
        threshold = thresholds.get(metric)
        crossing = ""
        if threshold is not None:
            was = a >= float(threshold)
            now = b >= float(threshold)
            if was != now:
                crossing = "✅ 跨过阈值" if now else "✗ **掉出阈值**"
        arrow = "↑" if delta > 1e-9 else ("↓" if delta < -1e-9 else "=")
        print(
            f"{label:<16} {_fmt(a):>8} {_fmt(b):>8} {delta:>+8.3f}  "
            f"{_fmt(threshold) if threshold is not None else '—':>5}  {arrow} {crossing}"
        )

    # 逐条对照：**只列状态翻转的用例**（全量逐条表格没人读，而翻转的才是要看的东西）
    left_rows = {row.get("id"): row for row in left.get("rows") or []}
    right_rows = {row.get("id"): row for row in right.get("rows") or []}
    flipped = [
        (case_id, left_rows[case_id], right_rows[case_id])
        for case_id in left_rows
        if case_id in right_rows and bool(left_rows[case_id].get("passed")) != bool(right_rows[case_id].get("passed"))
    ]
    if flipped:
        print(f"\n状态翻转的用例（{len(flipped)} 条）：")
        for case_id, old, new in flipped:
            direction = "变好 ✅" if new.get("passed") else "变差 ✗"
            print(
                f"  [{case_id}] {direction}：Faithfulness {_fmt(old.get('faithfulness'), 2)} →"
                f" {_fmt(new.get('faithfulness'), 2)}；失败维度 {old.get('failed_metrics')} →"
                f" {new.get('failed_metrics')}"
            )
    else:
        print("\n没有用例的状态发生翻转。")

    only_left = sorted(set(left_rows) - set(right_rows))
    only_right = sorted(set(right_rows) - set(left_rows))
    if only_left or only_right:
        print(f"\n用例集合不同：只在 A 里 {only_left}，只在 B 里 {only_right}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.eval_report",
        description="评测报告运维工具（默认只读：看报告 / 卡阈值 / 两份对照 / 追单条）",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", nargs="?", const=DEFAULT_DIR, metavar="DIR", help=f"列出报告（默认 {DEFAULT_DIR}）")
    action.add_argument("--show", metavar="JSON", help="看一份报告")
    action.add_argument("--verify", metavar="JSON", help="卡阈值（CI 的等价动作）")
    action.add_argument("--diff", nargs=2, metavar=("A_JSON", "B_JSON"), help="两份报告的对照")
    parser.add_argument("--case", default="", help="配 `--show`：只看这一条用例的判据明细")
    parser.add_argument("--limit", type=int, default=12, help="配 `--show`：最多列几条失败样本")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.list is not None:
            return cmd_list(str(args.list))
        if args.show is not None:
            return cmd_show(str(args.show), case_id=str(args.case), limit=int(args.limit))
        if args.verify is not None:
            return cmd_verify(str(args.verify))
        if args.diff is not None:
            return cmd_diff(str(args.diff[0]), str(args.diff[1]))
    except ReportError as exc:
        print(f"[报告不可用] {exc}", file=sys.stderr)
        return EXIT_NOT_RUNNABLE
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[读报告失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_NOT_RUNNABLE
    return EXIT_NOT_RUNNABLE


if __name__ == "__main__":
    sys.exit(main())
