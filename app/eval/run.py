"""评测入口（`14.eval.md` §2 的 `python -m eval.run --suite rag|agent`）。

## 一次运行做了什么（按顺序，**每一步失败的口径都不同**）

| 步骤 | 失败时 | 为什么这么定 |
|---|---|---|
| 读数据集 | **直接失败**（`DatasetError`） | 数据错了就什么都别算：跑到一半报错会让人以为是代码坏了 |
| 校验阈值 | **直接失败**（`ValueError`） | 阈值配错（如负增益）会让结果不可解释，而它花的是钱 |
| 构建检索器 | **直接失败** + 一句可照做的提示 | 「连不上库」与「评测不通过」必须区分（前者不是质量问题） |
| 跑用例 | **逐条容错**：某条崩了就记成失败样本 | 一条用例的异常不该让 100 条的结果全丢（评测很贵，尤其带真模型时） |
| 判分与聚合 | —— | 纯函数，没有失败路径 |
| 落盘 | 失败就报错并返回非 0 | 「跑完了但没留下产物」等于没跑（§8 第 4 条要证据链接） |

## 退出码（CI 的阻断落点，`14.eval.md` §4 第 4 条）

| 码 | 含义 |
|---|---|
| 0 | 逐维达标（且没有 `--baseline`，或对照也达标） |
| 1 | **跑完了但不达标** —— 这就是「低于阈值阻断合并」 |
| 2 | 没跑起来（数据集/配置/依赖问题）—— 与「质量不达标」严格区分 |

⚠️ 把「跑不起来」与「不达标」分成两个码是刻意的：CI 上两者都要红，
但值班人的动作完全不同（一个去查环境，一个去看评测报告）。
混成一个码时，最典型的浪费是「评测环境连不上库」被当成「模型变差了」排查一整晚。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Sequence

from app.eval.agent_eval import AgentOutcome, evaluate_agent
from app.eval.compare import Comparison, compare_single_vs_multi
from app.eval.dataset import DATASET_FILES, DatasetError, EvalSet, load_dataset
from app.eval.judge import Judge, build_judge
from app.eval.pipeline import AnswererLike, build_llm_answerer
from app.eval.rag_eval import Searcher, evaluate_rag
from app.eval.report import Report, ReportError, write_report
from app.eval.settings import JUDGE_MODES, REPORT_FORMATS, RagEvalSettings

logger = logging.getLogger(__name__)

#: 退出码（见模块 docstring）
EXIT_OK: Final[int] = 0
EXIT_BELOW_THRESHOLD: Final[int] = 1
EXIT_NOT_RUNNABLE: Final[int] = 2

#: `--suite` 的取值域（多一个 `compare`：它是「同一份数据跑两条路」）
SUITES: Final[tuple[str, ...]] = ("rag", "agent", "compare")

#: `--answer` 的取值域（三种合成方式，分数不可互相比较 —— 见 `build_default_answerer`）
ANSWER_MODES: Final[tuple[str, ...]] = ("context", "extract", "llm")

#: 缺依赖时给的提示（**必须能照着修**，与 `app/llm/client.py` 同一条纪律）
_RAG_HINT: Final[str] = (
    "无法构建检索器：请确认 ①`.env` 的 DATABASE_URL 指向真实的 PostgreSQL；"
    "② 已跑 `uv run alembic upgrade head`；③ 已灌语料 `uv run python -m scripts.load_knowledge_base`。"
    "详见 docs/runbook/14.eval-runbook.md §1。"
)


@dataclass
class RunResult:
    """一次 `run_suite` 的结果（报告 + 对照 + 退出码）。"""

    exit_code: int = EXIT_OK
    report: Report | None = None
    comparison: Comparison | None = None
    written: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


# ── 语料指纹（报告里的「这次跑的是哪一版语料」）────────────────────────────


def corpus_fingerprint() -> str:
    """当前知识库的指纹（`chunks=<n> rev=<r> hash=<8位>`）。

    为什么值得一个函数：`recall` 掉分的第一嫌疑是**语料变了**（有人改了一份政策文档），
    而不是代码变了。没有指纹时，两次报告只能靠日期猜，而「上周二那次是多少」没人记得。
    指纹包含三样：活跃片段数、缓存侧的语料版本号、以及「每份文档多少片段」的哈希
    （第三样才能抓住「文档换了内容但片段数没变」）。
    """
    try:
        from app.cache.corpus import current_revision
        from app.rag.settings import RagSettings
        from app.rag.store import RagStore

        settings = RagSettings.from_settings()
        store = RagStore(dimension=settings.vector_dim)
        stats = store.corpus_stats()
        chunks = int(stats.get("active_chunks") or stats.get("chunks") or 0)
        revision = int(current_revision() or 0)
        digest = hashlib.sha256(
            repr(sorted((k, v) for k, v in stats.items())).encode("utf-8")
        ).hexdigest()[:8]
        return f"chunks={chunks} rev={revision} hash={digest}"
    except Exception as exc:  # noqa: BLE001 - 指纹是旁路信息，取不到就不记
        logger.debug("语料指纹取不到（不影响评测）：%s: %s", type(exc).__name__, exc)
        return ""


# ── 默认依赖装配（全部可注入：这是 `run_suite` 能被离线测的前提）──────────


# ── 默认搜索器装配（同步/异步两种口径，见各自的说明）──────────────────────


def build_sync_searcher(*, rerank: bool = True, top_k_default: int = 5) -> Searcher:
    """**同步**检索器 —— 给 `GraphRuntime.search=` 用（`app.tools.retriever` 的口径）。

    ## 为什么这里必须有两个检索器（本仓实测踩到的接线坑）

    `app/tools/retriever.py::retrieve_docs` 是**同步**函数，它做 `list(searcher(...))`；
    而 `Retriever.search` 是 async。第一版把 async 的那一个直接塞进 `GraphRuntime.search`，
    结果是 `rag` 节点在 state 里写下一条拒答：

    ```
    知识库检索这一步没做成（TypeError）：'coroutine' object is not iterable
    ```

    —— 而它的**症状极具误导性**：图跑完了、答案也生成了（模型看到「检索不可用」就如实说
    拿不到材料），Recall / Faithfulness 双双为 0，看起来像「检索质量崩了」。
    真相是接线口的同步/异步不匹配。详见 runbook §10.1 坑 4。

    两种修法，本仓**两个都留着**（各有各的用途）：

    | 修法 | 用在哪 | 代价 |
    |---|---|---|
    | `prefetch_searcher`（推荐，评测用） | `agent_eval` 每轮预取一次 | 检索发生在开线程之外，零额外线程 |
    | `build_sync_searcher`（本函数） | 需要「图自己决定什么时候检索」的场景（多轮、重放） | 每次检索多一次线程与事件循环 |

    ⚠️ 不要在**已经在跑事件循环**的线程里调 `asyncio.run`：本函数把调用丢进
    `asyncio.to_thread` 开的工作线程里跑（那里没有运行中的循环），
    所以从图节点（async 上下文）调用它是安全的。
    """
    import asyncio
    import concurrent.futures

    async_searcher = build_default_searcher(rerank=rerank, top_k_default=top_k_default)

    def search(*, query: str, top_k: int | None = None, **kwargs: Any) -> Any:
        del kwargs  # 工具层可能多传 `min_score`；rerank 由闭包固定

        async def _run() -> Any:
            return await async_searcher(query=query, top_k=top_k)

        # `ThreadPoolExecutor` + `asyncio.run`：在工作线程里新建一个事件循环（见上面的 ⚠️）
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(_run())).result()

    return search


def prefetch_searcher(hits: list[Any], *, query: str) -> Searcher:
    """**预取式**检索器：先（异步）检好，再返回一个回放结果的同步函数。

    ⚠️ 只回答**同一个问题**的检索（`query` 不匹配时明确报错，而不是返回空/错的结果）：
    返回空会让「检索失败」与「问题不匹配」混成一类，而后者是评测自己的接线 bug。
    """
    expected = str(query)

    def search(*, query: str, top_k: int | None = None, **kwargs: Any) -> list[Any]:
        del kwargs, top_k
        if str(query) != expected:
            raise RuntimeError(
                f"预取式检索器只为 {expected!r} 准备了结果，却收到了 {query!r}。"
                "评测的每一轮必须重新预取（见 docs/runbook/14.eval-runbook.md §7.2）。"
            )
        return list(hits)

    return search


def build_default_searcher(*, rerank: bool = True, top_k_default: int = 5) -> Searcher:
    """生产检索函数（`app/rag/retriever.Retriever.search`）。

    ## 为什么包一层（而不是直接把 `hybrid_search` 传进去）——本仓实测踩到

    `hybrid_search(query, top_k, *, min_score, retriever)` **不接受** `with_rerank`，
    而「关掉 rerank 跑一次对照」是评测的常规动作（`14.eval.md` §5 第二条要的就是对照）。
    第一版直接把 `hybrid_search` 传进来，第一轮就撞上
    `TypeError: hybrid_search() got an unexpected keyword argument 'with_rerank'`。

    修法**不是**在评测里 if/else 挑两个入口（那会让「评测跑的检索」与
    「生产跑的检索」出现两条代码路径，而评测量出来的就不再是生产那条），
    而是显式地 `replace` 一份 `RagSettings` 后交给**同一个** `Retriever.search`：

    * `rerank=True` → 用配置里的 rerank 后端（生产口径）；
    * `rerank=False` → `rerank="off"`（第 10 步为对照实验预留的取值，见 `10.rag-runbook.md` §6）。

    ⚠️ `rerank=False` 时**不要**指望 `hybrid_search(query, min_score=...)`：
    那是另一个门面，它没有对照开关。
    """
    try:
        from app.rag.retriever import Retriever
        from app.rag.settings import RagSettings
        from app.rag.store import RagStore
    except ImportError as exc:  # pragma: no cover - 第 10 步之后不会发生
        raise RuntimeError(f"检索层尚未就绪（app.rag 不可 import）：{exc}。{_RAG_HINT}") from exc

    settings = RagSettings.from_settings()
    # ⚠️ `with_rerank` 是**每次调用**的开关（`Retriever.search` 的具名参数），
    # 所以**只建一个检索器**：两份实例会让「关掉 rerank 的那次对照」多付一次
    # embedding/rerank 模型加载（实测单次 ~36s），而它省钱的地方恰恰是「少跑一遍精排」。
    retriever = Retriever(
        settings=settings,
        store=RagStore(dimension=settings.vector_dim),
    )

    async def search(*, query: str, top_k: int | None = None, with_rerank: bool | None = None) -> Any:
        effective = int(top_k or top_k_default)
        return await retriever.search(
            query,
            effective,
            with_rerank=(rerank if with_rerank is None else bool(with_rerank)),
        )

    search.retriever = retriever  # type: ignore[attr-defined]
    return search


def build_default_answerer(mode: str = "context", llm: Any = None) -> tuple[AnswererLike | None, str]:
    """按 `--answer` 造合成器，返回 `(answerer, 报告里那一列显示的名字)`。

    三种取值（`ANSWER_MODES`）：

    | `--answer` | 答案来自 | 报告里的 `answerer` |
    |---|---|---|
    | `context`（默认） | 检索命中的**片段原文** | `context` |
    | `extract` | 从命中里挑最相关的几句 | `extract` |
    | `llm` | 经 `llm_call` 门面打真模型 | `llm:<模型名>` |

    ⚠️ 三种方式的分数**不可互相比较**（`pipeline.py` 的 docstring 解释得很细）：
    Faithfulness 在 `context` 下是定义上的 1.0，而在 `llm` 下才真正在量模型。
    报告里的 `answerer` 一列就是为此而设 —— 少了它，两份报告放在一起会得出错误结论。
    """
    normalized = str(mode or "context").strip().lower()
    if normalized in ("context", "extract"):
        if normalized == "extract":
            from app.eval.pipeline import extractive_answerer

            return extractive_answerer(), "extract"
        from app.eval.pipeline import context_answerer

        return context_answerer(), "context"
    if normalized == "llm":
        from app.config import get_settings

        model = get_settings().llm_primary
        return build_llm_answerer(llm=llm), f"llm:{model}"
    raise ValueError(f"未知的合成方式 {mode!r}（可选：{ANSWER_MODES}）")


def build_default_graph(*, llm: Any = None, search: Searcher | None = None) -> Any:
    """编译一张**评测用**的图：内存 checkpointer + 注入的模型与检索。

    ⚠️ 三条刻意的选择：
      ① 用 `InMemorySaver`（评测不需要跨进程恢复，而连真库会让 105 条用例慢一个量级）；
      ② `cache=None`（**缓存会改变被测对象**：命中时答案来自缓存而不是模型 ——
         那会让「改了 prompt 之后评分变了吗」这个问题失去意义，见 runbook §7.2）；
      ③ `interrupts` 默认关（闸门会让 `ticket` 分支挂起等人工批准，评测里没有人会来点）；
        需要测人工审批时用 `--interrupts`（runbook §8.3 有一个专门的场景）。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from app.graph.builder import compile_graph
    from app.graph.config import GraphRuntime

    runtime = GraphRuntime(llm=llm, search=search)
    return compile_graph(checkpointer=InMemorySaver(), runtime=runtime)


# ── 主流程 ─────────────────────────────────────────────────────────────────


async def run_suite(
    dataset: EvalSet,
    *,
    settings: RagEvalSettings | None = None,
    searcher: Searcher | None = None,
    judge: Judge | None = None,
    answerer: AnswererLike | None = None,
    answer_label: str = "context",
    graph: Any = None,
) -> RunResult:
    """跑一个套件并返回结果（**本模块唯一的主流程**）。

    全部依赖都可注入 —— 这是「评测本身也要能被测试」的前提（`tests/eval/` 用
    假的检索与假的判官跑完整流程，不需要库、不需要密钥、不花钱）。
    """
    settings = settings or RagEvalSettings()
    try:
        settings.validate()
    except ValueError as exc:
        return RunResult(exit_code=EXIT_NOT_RUNNABLE, error=f"配置不可用：{exc}")

    if settings.suite == "agent" and graph is None:
        try:
            graph = build_default_graph(search=searcher)
        except Exception as exc:  # noqa: BLE001
            return RunResult(exit_code=EXIT_NOT_RUNNABLE, error=f"无法编译评测图：{exc}")

    if settings.suite == "compare":
        # `--baseline` 是「顺便跑一次单 Agent 对照」；对照需要检索器（多 Agent 那一侧用）
        if searcher is None:
            return RunResult(exit_code=EXIT_NOT_RUNNABLE, error=f"compare 需要检索器。{_RAG_HINT}")
        active_judge = judge if judge is not None else build_judge(settings.judge)
        # 对照的两条路都用各自缺省的真模型合成器（`answerer=None`）：
        # 多 Agent 用 `build_llm_answerer`（只据检索材料），单 Agent 用
        # `build_single_agent_answerer`（不给材料）。见 `compare.py` 的机制表。
        comparison = await compare_single_vs_multi(
            dataset,
            search=searcher,
            settings=settings,
            judge=active_judge,
            answerer=answerer,
        )
        report = Report(
            suite="compare",
            kind=dataset.kind,
            dataset_name=dataset.name,
            dataset_version=dataset.version,
            judge=settings.judge,
            answerer=f"graph+{answer_label}",
            settings=settings.as_dict(),
            rows=[],
            groups=[],
            corpus_fingerprint=settings.corpus_fingerprint,
            comparison=comparison.as_dict(),
            notes=list(comparison.notes),
        )
        return RunResult(
            exit_code=EXIT_OK if comparison.passed else EXIT_BELOW_THRESHOLD,
            report=report,
            comparison=comparison,
        )

    if settings.suite == "agent":
        report, _ = await evaluate_agent(dataset, graph=graph, settings=settings, judge=judge)
        # agent 套件的答案由**图**生成（`router → rag → synthesis`），
        # 与 `--answer` 那个开关无关 —— 所以标签是 `graph+<合成方式>`：
        # 写清「答案来自图」这件事，否则读报告的人会以为 `--answer context` 也管这一路。
        report.answerer = f"graph+{answer_label}"
        return RunResult(exit_code=_code_for(report, settings), report=report)

    if searcher is None:
        return RunResult(exit_code=EXIT_NOT_RUNNABLE, error=f"rag 套件需要检索器。{_RAG_HINT}")
    report = await evaluate_rag(
        dataset, search=searcher, settings=settings, judge=judge, answerer=answerer
    )
    report.answerer = answer_label
    return RunResult(exit_code=_code_for(report, settings), report=report)


def _code_for(report: Report, settings: RagEvalSettings) -> int:
    """报告 → 退出码（CI 的阻断落点）。"""
    from app.eval.metrics import passed_overall

    overall = report.overall()
    if overall.count < 1:
        # 一条都没跑：**必须**判失败。这是 CI 里最容易发生的假绿
        # （数据集路径写错、`--only` 挑空），而它看起来像一次成功的运行。
        return EXIT_NOT_RUNNABLE
    return EXIT_OK if passed_overall(overall, thresholds=settings.thresholds()) else EXIT_BELOW_THRESHOLD


# ── CLI ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.run",
        description="ShopSage 评测（RAG 三指标 / Agent 成功率 / 单 vs 多 Agent 对比）",
    )
    parser.add_argument(
        "--suite",
        default="rag",
        help=f"{'|'.join(SUITES)}（rag / agent）或数据集路径（默认 rag；已知数据集：{sorted(DATASET_FILES)}）",
    )
    parser.add_argument("--only", nargs="*", default=[], help="只跑这些用例 id")
    parser.add_argument("--limit", type=int, default=None, help="最多跑多少条（冒烟用）")
    parser.add_argument("--top-k", type=int, default=5, help="检索条数（也是 Recall@K 的 K）")
    parser.add_argument(
        "--no-rerank", action="store_true", help="关掉 rerank（对照：RRF 融合顺序）"
    )
    parser.add_argument("--judge", default="rules", choices=list(JUDGE_MODES), help="判分后端")
    parser.add_argument(
        "--answer",
        default="context",
        choices=list(ANSWER_MODES),
        help="答案从哪来（context = 片段原文、extract = 挑句、llm = 真模型；见 pipeline.py）",
    )
    parser.add_argument("--baseline", action="store_true", help="同时跑单 Agent 对照")
    parser.add_argument("--out-dir", default=None, help="报告落盘目录（默认只打印）")
    parser.add_argument("--format", dest="report_format", default="markdown", choices=list(REPORT_FORMATS))
    parser.add_argument("--recall-threshold", type=float, default=None)
    parser.add_argument("--faithfulness-threshold", type=float, default=None)
    parser.add_argument("--relevance-threshold", type=float, default=None)
    parser.add_argument("--min-multi-agent-gain", type=float, default=None)
    parser.add_argument("--corpus-fingerprint", action="store_true", help="记录知识库指纹（要连库）")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def settings_from_args(args: argparse.Namespace) -> RagEvalSettings:
    """CLI 参数 → `RagEvalSettings`（**唯一映射处**）。

    命令行只覆盖显式给出的项：`None` 的阈值表示「用默认值」，
    而不是「用 0」—— 后者会让一次忘记带参数的运行把所有指标都判通过。
    """
    defaults = RagEvalSettings()
    raw_suite = str(args.suite)
    # `--baseline` 的语义是「顺便跑一次单 Agent 对照」：它把主流程切到 `compare` 套件。
    # 做成「覆盖 suite」而不是「跑完 rag 再跑一次对照」，是因为对照本身就产出一份完整报告
    # （两个方案的对照表），两次运行会让报告里出现两份互相矛盾的汇总。
    if args.baseline:
        suite = "compare"
    elif raw_suite in SUITES:
        suite = raw_suite
    else:
        suite = "rag"  # 数据集路径形态：按 rag 套件跑
    settings = RagEvalSettings(
        suite=suite,
        only=tuple(args.only or ()),
        limit=args.limit,
        top_k=int(args.top_k),
        rerank=not bool(args.no_rerank),
        judge=str(args.judge),
        baseline=bool(args.baseline),
        out_dir=args.out_dir,
        report_format=str(args.report_format),
        recall_threshold=(
            defaults.recall_threshold if args.recall_threshold is None else float(args.recall_threshold)
        ),
        faithfulness_threshold=(
            defaults.faithfulness_threshold
            if args.faithfulness_threshold is None
            else float(args.faithfulness_threshold)
        ),
        relevance_threshold=(
            defaults.relevance_threshold
            if args.relevance_threshold is None
            else float(args.relevance_threshold)
        ),
        min_multi_agent_gain=(
            defaults.min_multi_agent_gain
            if args.min_multi_agent_gain is None
            else float(args.min_multi_agent_gain)
        ),
        corpus_fingerprint=corpus_fingerprint() if args.corpus_fingerprint else "",
    )
    return settings


def dataset_name_from_args(args: argparse.Namespace) -> str:
    """`--suite` 既接受逻辑名（`rag`/`agent`/`compare`），也接受数据集路径。

    `compare` 用 **rag 那份数据**：对比要在「检索能答上来的题」上做，
    而 agent 那份里有「知识库里没有答案」的用例（对照两边都会拒答，没有信息量）。
    """
    suite = str(args.suite)
    if suite == "compare":
        return "rag"
    return suite


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口（返回值即退出码，见模块 docstring 的码表）。"""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    settings = settings_from_args(args)
    try:
        dataset = load_dataset(dataset_name_from_args(args), root=Path("."))
    except DatasetError as exc:
        print(f"[评测无法启动] {exc}", file=sys.stderr)
        return EXIT_NOT_RUNNABLE

    dataset = dataset.subset(list(settings.only), settings.limit)

    judge = build_judge(settings.judge)
    try:
        answerer, answer_label = build_default_answerer(str(args.answer))
    except ValueError as exc:
        print(f"[评测无法启动] {exc}", file=sys.stderr)
        return EXIT_NOT_RUNNABLE
    searcher: Searcher | None = None
    graph: Any = None
    try:
        if settings.suite in ("rag", "compare"):
            searcher = build_default_searcher(rerank=settings.rerank, top_k_default=settings.top_k)
        if settings.suite == "agent":
            # ⚠️ 图里用的是**同步**检索器：`app/tools/retriever.retrieve_docs` 是同步函数，
            # 给它 async 函数会得到 `TypeError: 'coroutine' object is not iterable`
            # 并被 `rag_node` 落成一条拒答（症状看起来像「检索质量崩了」，见 runbook §10.1 坑 4）。
            graph = build_default_graph(
                search=build_sync_searcher(rerank=settings.rerank, top_k_default=settings.top_k)
            )
    except Exception as exc:  # noqa: BLE001 - 「没跑起来」与「不达标」必须分开
        print(f"[评测无法启动] {exc}", file=sys.stderr)
        return EXIT_NOT_RUNNABLE

    started = time.time()
    try:
        result = asyncio.run(
            run_suite(
                dataset,
                settings=settings,
                searcher=searcher,
                judge=judge,
                answerer=answerer,
                answer_label=answer_label,
                graph=graph,
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[评测无法启动] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_NOT_RUNNABLE

    if result.report is None:
        print(f"[评测无法启动] {result.error}", file=sys.stderr)
        return result.exit_code

    # `run_suite` 已经按套件写好了 `answerer` 标签（`context` / `graph+context` / `context+单Agent`），
    # 所以这里**不再覆盖** —— 覆盖过一次就会出现「报告说 context、实际是图」的错位。
    if settings.out_dir:
        try:
            path = write_report(result.report, out_dir=settings.out_dir, fmt=settings.report_format)
        except ReportError as exc:
            print(f"[报告写入失败] {exc}", file=sys.stderr)
            return EXIT_NOT_RUNNABLE
        result.written.append(str(path))
        # 同时落一份 JSON：人读 Markdown，CI 与外部工具读 JSON（两者同源，见 report.py）
        if settings.report_format == "markdown":
            result.written.append(
                str(write_report(result.report, out_dir=settings.out_dir, fmt="json"))
            )

    print(result.report.summary_line())
    for note in result.report.notes:
        print(f"  · {note}")
    for path in result.written:
        print(f"  报告：{path}")
    print(f"  耗时 {time.time() - started:.1f}s，退出码 {result.exit_code}")
    return result.exit_code


__all__ = [
    "ANSWER_MODES",
    "EXIT_BELOW_THRESHOLD",
    "EXIT_NOT_RUNNABLE",
    "EXIT_OK",
    "SUITES",
    "RunResult",
    "build_default_answerer",
    "build_default_graph",
    "build_default_searcher",
    "build_parser",
    "build_sync_searcher",
    "corpus_fingerprint",
    "main",
    "prefetch_searcher",
    "run_suite",
    "settings_from_args",
]

if __name__ == "__main__":
    sys.exit(main())