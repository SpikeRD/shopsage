"""离线评测演示（**五个场景**，零依赖、零成本、完全可复现）。

```bash
uv run python -m scripts.eval_demo               # 跑全部五个场景
uv run python -m scripts.eval_demo --scenario compare
```

## 这个脚本回答什么问题（与 `eval/run` 的分工）

| | `python -m eval.run` | **`scripts/eval_demo.py`（本脚本）** |
|---|---|---|
| 依赖 | 真库 + 真 embedding/rerank（+ 真模型） | **零依赖**（假检索 + 假合成 + 固定判官） |
| 分数 | 真实链路的分数（要记账、会波动） | **机制是否成立**（固定答案，永远同一结果） |
| 用途 | CI 闸门 / 验收证据 | 上手、排障、给人解释「这套评测怎么工作」 |

⚠️ **两个脚本的分数不可互相比较**：它们的 `answerer` / `judge` 不同
（本脚本一律是 `fake` 合成 + `strict` 判官）。报告里那两列就是为此而设。

## 为什么值得单独一个脚本（而不是「跑一次 eval/run 就行」）

`14.eval.md` §4 第 3 条要的「单 Agent vs 多 Agent 的量化差距」如果只能用真模型跑，
它就会变成一个**不可复现的数字**（同一个提交跑两次能差 0.2），于是没人敢把它写进简历、
也没人敢拿它当回归基线。本脚本用「只据材料作答」的假合成器把机制钉死：

* 多 Agent 那条路把检索命中塞进 prompt → 假合成器逐字引用 → 高分；
* 单 Agent 那条路不给材料 → 假合成器只能给一句通用话术 → 低分。

差距因此来自**机制**（有没有检索），而不是运气。真模型那一侧的对照见
`docs/runbook/14.eval-runbook.md` §6.3（那里也有实测数字）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.eval.agent_eval import evaluate_agent
from app.eval.compare import MULTI_AGENT, SINGLE_AGENT, compare_single_vs_multi
from app.eval.dataset import DATASET_FILES, DatasetError, EvalSet, load_dataset
from app.eval.judge import strict_llm_judge
from app.eval.metrics import passed_overall
from app.eval.pipeline import context_answerer, extractive_answerer
from app.eval.rag_eval import evaluate_rag
from app.eval.report import Report
from app.eval.run import EXIT_OK, build_default_searcher, build_sync_searcher
from app.eval.settings import RagEvalSettings

logger = logging.getLogger(__name__)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: 五个场景（名字 → 说明）。`all` 是默认值。
SCENARIOS: Final[dict[str, str]] = {
    "rag": "RAG 三指标（离线合成 + 严格判官）：Recall / Faithfulness / Relevance",
    "agent": "端到端图（router → rag → synthesis）：成功率 + 关键事实命中率",
    "compare": "单 Agent vs 多 Agent 的量化差距（本脚本的核心场景）",
    "extract": "挑句合成 vs 片段原文合成：**同一份检索**下两种合成的分数差",
    "offline": "全部离线 + 假图：不连库也能看到完整报告长什么样",
}


@dataclass
class DemoContext:
    """一次演示运行用到的全部依赖（真/假两种都从这里取）。"""

    dataset: EvalSet
    search: Any
    graph: Any = None
    offline: bool = False


# ── 假合成器（对照实验的关键：只据 prompt 里的材料作答）──────────────────


def _materials_from_prompt(body: str) -> list[str]:
    """从合成/单 Agent 的 prompt 里抠出「材料句」（含数字的正文行）。

    ⚠️ 排除出处行（`[1]（出处：…）`）与说明行是必需的：它们也含数字（片段编号），
    不排除时假 LLM 会把出处抄成答案 —— 那不是机制差异，是**假模型太笨**
    （本仓实测踩到，见 runbook §10.2）。
    """
    sentences = [item.strip() for item in re.split(r"[。\n]+", body) if item.strip()]
    return [
        item
        for item in sentences
        if re.search(r"\d", item)
        and "用户问题" not in item
        and not item.startswith("[")
        and "出处：" not in item
        and "要求：" not in item
        and "知识库片段" not in item
        and "能力" not in item
    ]


def build_single_call_llm(
    *, max_sentences: int = 4, fallback: str = "我们一般按平台规则处理，具体请您咨询人工客服。"
):
    """「一次调用给一段完整回答」的假 LLM —— **一次生成**，而不是只抄一句。

    ## 为什么不能只抄最长的一句（本仓实测踩到，见 runbook §10.2）

    `material_only_llm` 只抄**一句**，于是多 Agent 那一侧的事实点覆盖率只有 **0.37**：
    一条政策问题的答案常常需要「条件句 + 数字句」（比如「7 天内可退」与「运费由买家承担」
    是两个事实点），一句话天然答不全。于是对照实验量到的差距里，
    有一大半是「假模型太懒」，而不是「机制差异」。

    改用「把材料里最相关的 N 句拼成一段」之后，多 Agent 那一侧的事实点覆盖率会接近满分，
    而单 Agent 那一侧仍然只能给一句通用话术（它没有材料）—— 这才是干净的对照。
    """

    async def fake_llm(messages, **kwargs):
        del kwargs
        body = str(messages[-1].get("content") or "")
        candidates = _materials_from_prompt(body)
        if not candidates:
            return {"answer": fallback, "cited": []}
        picked = sorted(candidates, key=len, reverse=True)[:max_sentences]
        # 按在材料里出现的顺序还原（读起来像人写的回答，而不是按长度排的清单）
        ordered = [item for item in candidates if item in picked]
        return {"answer": "".join(item + "。" for item in ordered), "cited": list(range(1, len(ordered) + 1))}

    return fake_llm


def material_only_llm(*, fallback: str = "我们一般按平台规则处理，具体请您咨询人工客服。"):
    """一个**只据 prompt 里的材料作答**的假 LLM（真链路对照实验用）。

    它不知道自己是单 Agent 还是多 Agent，也不知道标准答案 —— 它只做一件事：
    把 user 消息里最长的那条含数字正文句抄进 answer，抄不到就回一句通用话术。
    这正是真模型在「有材料 / 没材料」两种处境下的核心差别。
    """

    async def fake_llm(messages, **kwargs):
        del kwargs
        body = str(messages[-1].get("content") or "")
        candidates = _materials_from_prompt(body)
        if candidates:
            return {"answer": max(candidates, key=len) + "。", "cited": [1]}
        return {"answer": fallback, "cited": []}

    return fake_llm


def offline_llm(*, default_route: str = "rag", fallback: str = "我们一般按平台规则处理，具体请您咨询人工客服。"):
    """**离线场景的三合一假模型**：按 system prompt 认出这是哪个环节，分别给出该环节要的 JSON。

    为什么要一个「三合一」的假模型（而不是三个各管一段的假模型拼起来）：
    真实链路里**只有一个模型出口**（`llm_call` 门面），图的每个节点都调它。
    离线场景如果给每个节点配一个不同的假模型，它就不再像真链路了 ——
    而离线场景的全部价值就是「让人一眼看懂真链路长什么样」。

    三个环节的识别标记都取自各自的 system prompt（改 prompt 就会在这里红，
    这正是我们想要的：prompt 是契约的一部分）：

    | 环节 | 识别标记 | 返回 |
    |---|---|---|
    | 输入护栏 | `内容安全` | `{"allowed": true}` |
    | 路由 | `意图分流器` | `{"route": …, "certainty": …}` |
    | 输出护栏 | `合规审查` | `{"allowed": true}` |
    | 合成 | 其余 | 从材料里抠一句（即「只据材料作答」） |
    """
    rules = (
        (r"订单|快递|物流到|发货|几单|状态|退款", "data"),
        (r"趋势|占比|排行|同比|环比", "analysis"),
        (r"^你好|^谢谢|^在吗", "final"),
    )

    async def fake_llm(messages, **kwargs):
        del kwargs
        system = str(messages[0].get("content") or "")
        body = str(messages[-1].get("content") or "")

        if "内容安全" in system or "合规审查" in system:
            return {"allowed": True, "reason": "离线演示：默认放行"}
        if "意图分流器" in system:
            question = body.split("当前问题：")[-1].strip() or body.strip()
            for pattern, route in rules:
                if re.search(pattern, question):
                    return {"route": route, "certainty": 0.8, "reason": "关键词规则（离线）"}
            return {"route": default_route, "certainty": 0.6, "reason": "默认（离线）"}

        candidates = _materials_from_prompt(body)
        if candidates:
            return {"answer": max(candidates, key=len) + "。", "cited": [1]}
        return {"answer": fallback, "cited": []}

    return fake_llm


def offline_search(bank: dict[str, str]):
    """按关键词返回**固定语料**的假检索器（与 `eval/run` 的检索器**同一套调用契约**）。

    ⚠️ 返回值的形状必须与真实现一致（有 `.hits` / `.elapsed_ms`）：
    本仓实测第一版返回了**裸列表**，结果 `app.tools.retriever` 的
    `list(searcher(...))` 拿到一个对象 → `TypeError: 'Result' object is not iterable`，
    被 `rag_node` 落成一条拒答（症状看起来像「检索坏了」，见 runbook §10.1 坑 4）。
    离线假件最容易犯的错就是**形状与真件不一致** —— 这条注释就是为此而写。
    """

    def search(*, query: str, top_k: int = 5, with_rerank: bool | None = None):
        del with_rerank
        hits = [
            {"text": text, "source": source, "score": round(0.9 - 0.05 * index, 4)}
            for index, (source, text) in enumerate(bank.items())
            if any(token in query for token in _TOKENS.get(source, ()))
        ]
        if not hits:
            source = next(iter(bank))
            hits = [{"text": bank[source], "source": source, "score": 0.4}]
        return type("Result", (), {"hits": hits[:top_k], "elapsed_ms": 0.5})()

    return search


#: 离线语料（与 `docs/kb/` 里的原文一致；关键词 → 文档）
OFFLINE_CORPUS: Final[dict[str, str]] = {
    "docs/kb/退货政策.md": (
        "第 1 条：自签收之日起 7 天内，商品完好、不影响二次销售的，可申请无理由退货。\n"
        "第 2 条：无理由退货的运费由买家承担；因商品质量问题发起的退货，往返运费由本店承担。"
    ),
    "docs/kb/物流政策.md": "2.1 单笔订单实付满 99 元免运费；未满 99 元收取 10 元运费。",
    "docs/kb/会员积分.md": "3.3 积分有效期为获得之日起 24 个月，过期自动清零，不可恢复。",
    "docs/kb/产品保修.md": "第 1 条：整机类商品自签收之日起提供 12 个月保修。",
}

_TOKENS: Final[dict[str, tuple[str, ...]]] = {
    "docs/kb/退货政策.md": ("退货", "运费", "换货"),
    "docs/kb/物流政策.md": ("运费", "物流", "破损", "免运费"),
    "docs/kb/会员积分.md": ("积分",),
    "docs/kb/产品保修.md": ("保修", "延保"),
}


# ── 五个场景 ────────────────────────────────────────────────────────────


def _print_report(report: Report) -> None:
    overall = report.overall()
    print(f"    {report.summary_line()}")
    thresholds = report.settings.get("thresholds") or {}
    verdict = "✅ 达标" if passed_overall(overall, thresholds=thresholds) else "✗ 未达标"
    print(
        f"    条数={overall.count}  Recall={overall.recall:.3f}"
        f"  Faithfulness={overall.faithfulness:.3f}  Relevance={overall.answer_relevancy:.3f}  {verdict}"
    )
    for note in report.notes:
        print(f"    · {note}")


async def scenario_rag(context: DemoContext, settings: RagEvalSettings, judge) -> int:
    report = await evaluate_rag(
        context.dataset,
        search=context.search,
        settings=settings,
        judge=judge,
        answerer=context_answerer(),
    )
    print("  场景 1：RAG 三指标（片段原文合成 + 严格判官）")
    _print_report(report)
    return EXIT_OK


async def scenario_extract(context: DemoContext, settings: RagEvalSettings, judge) -> int:
    """同一份检索、两种合成：把「合成方式会改变分数」这件事摆出来。"""
    context_report = await evaluate_rag(
        context.dataset, search=context.search, settings=settings, judge=judge, answerer=context_answerer()
    )
    extract_report = await evaluate_rag(
        context.dataset, search=context.search, settings=settings, judge=judge, answerer=extractive_answerer()
    )
    print("  场景 4：同一份检索下，两种合成的分数（**不可互相比较**）")
    print(f"    {'合成方式':<10} {'Recall':>8} {'Faithfulness':>14} {'Relevance':>11}")
    for label, report in (("context", context_report), ("extract", extract_report)):
        overall = report.overall()
        print(
            f"    {label:<10} {overall.recall:>8.3f} {overall.faithfulness:>14.3f} {overall.answer_relevancy:>11.3f}"
        )
    gap = context_report.overall().faithfulness - extract_report.overall().faithfulness
    print(f"    → Faithfulness 差 {gap:+.3f}：**那部分不是检索质量**，是挑句启发式自己的得分")
    return EXIT_OK


async def scenario_compare(context: DemoContext, settings: RagEvalSettings, judge) -> int:
    comparison = await compare_single_vs_multi(
        context.dataset,
        search=context.search,
        settings=settings,
        judge=judge,
        llm=build_single_call_llm(),
    )
    print("  场景 3：单 Agent vs 多 Agent（同一批用例、同一套判据、同一个判官）")
    print(f"    {'方案':<34} {'Recall':>8} {'Faithful':>9} {'Relevance':>10} {'事实点':>8} {'拒答':>6}")
    for item in comparison.variants:
        print(
            f"    {item.label:<34} {item.recall:>8.3f} {item.faithfulness:>9.3f}"
            f" {item.answer_relevancy:>10.3f} {item.key_fact_hit_rate:>8.3f} {item.refusals:>6}"
        )
    print(f"    → {comparison.verdict}")
    for note in comparison.notes:
        print(f"    · {note}")
    return EXIT_OK if comparison.passed else 1


async def scenario_agent(context: DemoContext, settings: RagEvalSettings, judge) -> int:
    if context.graph is None:
        print("  场景 2：agent 套件需要一张图（离线场景下用假图；真链路请用 `python -m eval.run --suite agent`）")
        return EXIT_OK
    report, _ = await evaluate_agent(context.dataset, graph=context.graph, settings=settings, judge=judge)
    print("  场景 2：端到端图（成功率 + 关键事实命中率）")
    _print_report(report)
    return EXIT_OK


async def scenario_offline(context: DemoContext, settings: RagEvalSettings, judge) -> int:
    """不连库也能跑：假检索 + 三合一假模型（router / 护栏 / 合成）走一遍**完整的图**。

    两条纪律（都是本仓实测踩出来的）：

    ① 用哪份数据集？—— **agent 那份**。它含 `data` / `analysis` / `final` 四条分支，
       才能让「按关键词路由」这个假 router 有东西可判；用 rag 那份的话所有用例
       都会命中 `rag` 分支，图的路由部分等于没跑。
    ② 假件必须与真件**形状一致**（`.hits` 而不是裸列表，见 `offline_search` 的 ⚠️）。

    它的用途是**上手**（新同学 clone 下来就能看到完整报告长什么样）
    与**排障分诊**（「评测坏了」与「依赖没起」是两类事，这个场景能证明评测代码本身是好的）。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from app.graph.builder import compile_graph
    from app.graph.config import GraphRuntime

    search = offline_search(OFFLINE_CORPUS)
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        runtime=GraphRuntime(llm=offline_llm(), search=search),
    )
    report, _ = await evaluate_agent(context.dataset, graph=graph, settings=settings, judge=judge)
    print("  场景 5：全部离线（假检索 + 关键词 router + 只据材料的假合成）")
    print("    ⚠️ 这一场的分数**没有意义**（data/analysis 分支在离线图里没有真数据源），")
    print("       它证明的是「整条评测链路能跑完并产出一份形状正确的报告」。")
    _print_report(report)
    return EXIT_OK


# ── CLI ─────────────────────────────────────────────────────────────────


def build_context(scenario: str, *, offline: bool) -> DemoContext:
    if offline:
        # 离线场景用 **agent 那份数据集**（含四条路由分支，见 `scenario_offline`）
        return DemoContext(
            dataset=load_dataset("agent", root=REPO_ROOT),
            search=offline_search(OFFLINE_CORPUS),
            offline=True,
        )
    if scenario == "agent":
        dataset = load_dataset("agent", root=REPO_ROOT)
        return DemoContext(
            dataset=dataset,
            search=build_sync_searcher(rerank=True),
            graph=_graph_for(scenario),
        )
    return DemoContext(dataset=load_dataset("rag", root=REPO_ROOT), search=build_default_searcher(rerank=True))


def _graph_for(scenario: str) -> Any:
    """agent 场景的图：真检索 + 真模型（与 `eval/run --suite agent` 同一条装配）。"""
    from app.eval.run import build_default_graph

    del scenario
    return build_default_graph(search=build_sync_searcher(rerank=True))


def _judge_for(scenario: str):
    """判官的选择（**每个场景显式选一个，并说明为什么**）。

    | 场景 | 判官 | 为什么 |
    |---|---|---|
    | `rag` / `extract` / `compare` | `strict`（离线、可复现） | 它们量的是**机制**，判官不能引入随机性 |
    | `agent` | `rules`（与 `eval/run --suite agent` 一致） | 它量的是**真链路**，判官口径必须与 CI 那条命令一致，否则两处分数不可比 |
    | `offline` | `rules` | 同上（离线图只是形状演示） |
    """
    from app.eval.judge import rules_judge

    return rules_judge() if scenario in ("agent", "offline") else strict_llm_judge()


async def run(scenario: str, *, offline: bool) -> int:
    # ⚠️ `agent` 场景必须用 `suite="agent"`：`score_agent_row` 会按它读阈值，
    # 而 suite 写错时**不报错**，只是把 agent 的口径用 rag 的默认值跑（本仓实测踩到，
    # 分数看起来"偏低"却找不到原因）。见 runbook §10.1 坑 8。
    settings = RagEvalSettings(
        suite="agent" if scenario in ("agent", "offline") else ("compare" if scenario == "compare" else "rag"),
        top_k=5,
    )
    judge = _judge_for(scenario)
    context = build_context(scenario, offline=offline)

    print("=" * 78)
    print(f"离线评测演示：{SCENARIOS[scenario]}")
    print(f"数据集：{context.dataset.summary()}")
    print(f"依赖：{'全部离线（假检索 + 假合成）' if offline or scenario == 'offline' else '真检索（embedding + rerank + pgvector）'}")
    print("=" * 78)

    if scenario == "compare":
        return await scenario_compare(context, settings, judge)
    if scenario == "agent":
        return await scenario_agent(context, settings, judge)
    if scenario == "extract":
        return await scenario_extract(context, settings, judge)
    if scenario == "offline" or offline:
        return await scenario_offline(context, settings, judge)
    return await scenario_rag(context, settings, judge)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShopSage 评测的离线演示（零依赖、可复现）")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["all", *SCENARIOS],
        help="跑哪个场景（all = 全部；offline = 不连库）",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    scenarios = list(SCENARIOS) if args.scenario == "all" else [str(args.scenario)]

    exit_code = EXIT_OK
    for name in scenarios:
        try:
            code = asyncio.run(run(name, offline=name == "offline"))
        except DatasetError as exc:
            print(f"[数据集有问题] {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - 演示脚本要给出可读的失败原因
            print(f"[场景 {name} 无法运行] {type(exc).__name__}: {exc}", file=sys.stderr)
            print(
                "  提示：真依赖场景需要 ① DATABASE_URL 可达；② 已灌语料 "
                "`uv run python -m scripts.load_knowledge_base`。"
                "想零依赖看一遍请加 `--scenario offline`。",
                file=sys.stderr,
            )
            return 2
        exit_code = max(exit_code, code)
        print()

    print("=" * 78)
    print(f"演示结束：退出码 {exit_code}（0 = 全部通过；1 = 有场景未达标；2 = 环境/数据问题）")
    print(f"数据集逻辑名：{sorted(DATASET_FILES)}；单/多 Agent 对照的详情见 SCENARIOS['compare']")
    print("=" * 78)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
