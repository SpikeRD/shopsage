"""跑通整条评测链路（`evaluate_rag` / `evaluate_agent` / `run_suite` / 报告落盘）。

全部用假检索 + 假判官 + 假 LLM：**零依赖、不花钱、完全可复现**。
这一组的价值不是「算得对不对」（那由 `test_eval_metrics.py` 钉住），
而是「接线对不对」——评测自己的 wiring 坏掉时，报告往往仍然打得出来。

> ⚠️ 本组**不用 `tmp_path`**（本仓沙箱里它会 `PermissionError: [WinError 5]`，
> 与 `tests/test_config.py` 那 6 个 error 是同一个环境限制）。
> 改用 `tmp_dir` 装置，落盘到仓库内已可写的 `.pytest-tmp/eval-tests/`。
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from app.eval.compare import compare_single_vs_multi
from app.eval.dataset import EvalSet, parse_dataset
from app.eval.judge import rules_judge, strict_llm_judge
from app.eval.pipeline import build_llm_answerer, build_single_agent_answerer, context_answerer
from app.eval.rag_eval import evaluate_rag
from app.eval.report import (
    REPORT_SCHEMA,
    Report,
    ReportError,
    load_report,
    report_filename,
    write_report,
)
from app.eval.run import (
    EXIT_BELOW_THRESHOLD,
    EXIT_NOT_RUNNABLE,
    EXIT_OK,
    build_parser,
    run_suite,
    settings_from_args,
)
from app.eval.settings import RagEvalSettings
from tests.eval.conftest import make_case, make_dataset


REPO_ROOT = Path(__file__).resolve().parents[2]

#: 落盘用例的工作目录（仓库内、**已确认可写** —— 见模块 docstring 的 ⚠️）
TMP_ROOT = REPO_ROOT / ".pytest-tmp" / "eval-tests"


@pytest.fixture
def tmp_dir():
    """一个干净的可写目录（替代 `tmp_path`，理由见模块 docstring）。

    用例结束即删除：`.pytest-tmp/` 是本仓的临时区，留垃圾会让下一次
    `Get-ChildItem .pytest-tmp` 里混进上一轮的报告。
    """
    path = TMP_ROOT / uuid.uuid4().hex[:8]
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _cases() -> EvalSet:
    """一份三条用例、覆盖「命中 / 漏检 / 知识库没有」的小数据集。"""
    return make_dataset(
        [
            make_case(),
            make_case(
                id="shipping",
                question="满多少钱免运费？",
                reference="单笔订单实付满 99 元免运费；未满 99 元收取 10 元运费。",
                key_facts=("99 元", "10 元"),
                expected_sources=("docs/kb/物流政策.md",),
            ),
            make_case(
                id="missing",
                question="你们支持加密货币支付吗？",
                reference="知识库里没有相关内容。",
                key_facts=(),
                expected_sources=(),
                expect_empty=True,
            ),
        ]
    )


# ── ① RAG 套件 ───────────────────────────────────────────────────────────


async def test_evaluate_rag_produces_a_usable_report(fake_search):
    report = await evaluate_rag(
        _cases(),
        search=fake_search(),
        settings=RagEvalSettings(),
        judge=rules_judge(),
        answerer=context_answerer(),
    )
    overall = report.overall()
    assert overall.count == 3
    assert report.suite == "rag" and report.dataset_version == "1"
    assert report.judge == "rules"
    assert overall.recall == pytest.approx(1.0)
    assert overall.faithfulness >= 0.7
    # 分组：按标签只列出现 ≥2 次的（一份一条的分组表没有信息量）
    assert any(group.name == "全部" for group in report.groups)
    # JSON 与 Markdown 同源
    payload = report.as_dict()
    assert payload["schema"] == REPORT_SCHEMA
    assert len(payload["rows"]) == 3
    assert "评测报告" in report.to_markdown()


async def test_evaluate_rag_marks_missing_documents_as_recall_failure(fake_search):
    """检索漏了期望来源 → Recall 掉分 → 该条进失败样本（并给出漏了哪一份）。"""
    dataset = make_dataset(
        [
            make_case(
                id="needs-two",
                expected_sources=("docs/kb/退货政策.md", "docs/kb/会员积分.md"),
            )
        ]
    )
    report = await evaluate_rag(
        dataset, search=fake_search(), settings=RagEvalSettings(), judge=rules_judge()
    )
    row = report.rows[0]
    assert row["recall"] == pytest.approx(0.5)
    assert row["missing_sources"] == ["会员积分"]
    assert not row["passed"] and "recall" in row["failed_metrics"]
    assert "漏检文档" in report.to_markdown()


async def test_evaluate_rag_works_without_a_search_backend(fake_search):
    """检索返回空：不能崩，要如实落成一条失败样本（「检索没接上」与「库里没有」要能区分）。"""
    dataset = make_dataset([make_case(id="no-hits")])
    report = await evaluate_rag(
        dataset, search=fake_search(miss=True), settings=RagEvalSettings(), judge=rules_judge()
    )
    row = report.rows[0]
    assert row["hits"] == 0 and row["recall"] == 0.0 and not row["passed"]
    # 抽取式/上下文式合成器在「没有命中」时给的是如实话术 → 判据要把它认成拒答而不是编造
    assert row["judge"]["faithful"] is False


async def test_expect_empty_case_does_not_drag_recall_down(fake_search):
    """`expect_empty` 的用例不参与 Recall 平均（本仓实测修正过的一条）。"""
    dataset = make_dataset(
        [
            make_case(id="a"),
            make_case(id="empty", expected_sources=(), expect_empty=True,
                      question="你们支持加密货币支付吗？", key_facts=()),
        ]
    )
    report = await evaluate_rag(
        dataset, search=fake_search(miss=True), settings=RagEvalSettings(), judge=rules_judge()
    )
    assert report.overall().extra["recall_applicable"] == 1


# ── ② 合成方式（三种口径互不混淆）────────────────────────────────────────


async def test_context_answerer_is_a_faithful_projection_of_the_hits(fake_search):
    """片段原文合成：答案是材料的逐字投影 → Faithfulness 定义上为 1.0。"""
    report = await evaluate_rag(
        make_dataset([make_case()]),
        search=fake_search(),
        settings=RagEvalSettings(),
        judge=rules_judge(),
        answerer=context_answerer(),
    )
    row = report.rows[0]
    assert row["faithfulness"] == pytest.approx(1.0)
    assert row["unsupported_numbers"] == []
    assert "出处" in row["answer"]


async def test_llm_answerer_uses_the_agent_facade_with_material_boundary():
    """真模型合成器：走 `generate_json`（= `llm_call` 门面），且 prompt 里写明材料边界。"""
    seen: dict[str, str] = {}

    async def fake_llm(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[-1]["content"]
        del kwargs
        return {"answer": "单笔实付满 99 元免运费。", "cited": [1]}

    answerer = build_llm_answerer(llm=fake_llm)
    answer = await answerer("满多少钱免运费？", [{"text": "2.1 满 99 元免运费。", "source": "物流政策.md"}])
    assert answer == "单笔实付满 99 元免运费。"
    assert "物流政策.md" in seen["user"]
    assert "唯一的信息来源" in seen["user"]

    empty = await answerer("满多少钱免运费？", [])
    assert empty  # 没有材料时也要有一句回答（不能空着）


async def test_single_agent_answerer_ignores_material():
    """单 Agent 基线**不给材料**：这是对照实验的自变量（`compare.py` 的机制表）。"""
    seen: dict[str, str] = {}

    async def fake_llm(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[-1]["content"]
        del kwargs
        return {"answer": "大概是 7 天吧。"}

    answerer = build_single_agent_answerer(llm=fake_llm)
    answer = await answerer("无理由退货是几天内可以申请？", [{"text": "绝密材料：7 天", "source": "s"}])
    assert answer == "大概是 7 天吧。"
    assert "绝密材料" not in seen["user"], "基线不许拿到检索材料"
    assert "只" not in seen["system"]  # 它没有「只用材料回答」这条约束（它没有材料）


# ── ③ agent 套件（用假图，不连库、不打模型）────────────────────────────


async def test_evaluate_agent_scores_route_and_delivery():
    """agent 套件比 rag 套件多一维：**路由**。走错分支的轮次不可能成功。"""
    from app.eval.agent_eval import evaluate_agent, score_agent_row

    case = make_case(expected_route="rag", tags=("synthetic",))
    good = score_agent_row(
        case=case,
        state={
            "route": "rag",
            "docs": [
                {
                    "text": "第 1 条：自签收之日起 7 天内，商品完好、不影响二次销售的，可申请无理由退货。",
                    "source": "docs/kb/退货政策.md",
                },
                {"text": "第 2 条：无理由退货的运费由买家承担。", "source": "docs/kb/退货政策.md"},
            ],
            # ⚠️ 答案用**逐字引自材料**的长句（而不是一句简写）：本仓的 Answer Relevance
            # 有篇幅折扣 —— 「一句话回答一个需要条件的政策问题」会按比例掉分。
            # 这不是为了迁就判据，而是那条折扣本来就该有：真链路里短答案通常意味着没答全。
            "answer": (
                "无理由退货是自签收之日起 7 天内可以申请，前提是商品完好、不影响二次销售；"
                "超过 7 天且没有质量问题的就不在受理范围内，退回运费由买家承担。"
            ),
            "refusals": [],
        },
        visited=["guardrail_input", "router", "rag", "synthesis"],
        settings=RagEvalSettings(suite="agent"),
    )
    assert good.route_ok and good.delivered and good.success

    wrong = score_agent_row(
        case=case,
        state={"route": "data", "docs": [], "answer": "我查不到。", "refusals": []},
        visited=["router", "data"],
        settings=RagEvalSettings(suite="agent"),
    )
    assert not wrong.route_ok and "route" in wrong.failed_metrics

    blocked = score_agent_row(
        case=case,
        state={"route": "rag", "docs": [], "answer": "安全话术", "blocked": True, "refusals": ["被拦"]},
        visited=["guardrail_input"],
        settings=RagEvalSettings(suite="agent"),
    )
    assert not blocked.delivered, "被拦下的一轮不算「答完了」"


async def test_agent_material_includes_rows_not_only_docs():
    """**本仓实测踩到的坑**：只把 `docs` 当材料时，所有 `data` 分支的 Faithfulness 假性偏低。

    `data` 分支的答案来自 SQL 结果（`rows`），它们不在 `docs` 里 ——
    报告会说「模型编造了数字」，而模型其实逐字引用了查询结果。
    """
    from app.eval.agent_eval import material_for_judging, score_agent_row

    case = make_case(
        id="data-case",
        question="订单 5 现在是什么状态？",
        reference="订单需要查库；知识库里没有订单数据。",
        key_facts=(),
        expected_sources=(),
        expected_route="data",
        expect_empty=True,
    )
    state = {
        "route": "data",
        "docs": [],
        "rows": [{"order_id": 5, "status": "shipped", "total_amount": "42.00"}],
        "sql": "SELECT order_id, status FROM orders WHERE order_id = :oid",
        "answer": "订单 5 的状态是 shipped（已发货）。",
        "refusals": [],
    }
    material = material_for_judging(state)
    assert "shipped" in material and "order_id" in material

    outcome = score_agent_row(case=case, state=state, visited=["router", "data"], settings=RagEvalSettings(suite="agent"))
    assert outcome.faithfulness == pytest.approx(1.0), "行数据也是材料，答案引用它不算编造"


async def test_agent_route_scoring_records_refusals_and_path():
    from app.eval.agent_eval import score_agent_row

    case = make_case(expected_route="rag")
    outcome = score_agent_row(
        case=case,
        state={"route": "rag", "docs": [], "answer": "", "refusals": ["检索不可用"]},
        visited=["router", "rag"],
        settings=RagEvalSettings(suite="agent"),
    )
    assert outcome.refusals == ["检索不可用"]
    assert not outcome.delivered and not outcome.success


# ── ④ `run_suite` 的退出码（CI 阻断的落点）──────────────────────────────


async def test_run_suite_exit_codes(fake_search):
    dataset = _cases()
    ok = await run_suite(
        dataset,
        settings=RagEvalSettings(),
        searcher=fake_search(),
        judge=rules_judge(),
        answerer=context_answerer(),
    )
    assert ok.exit_code == EXIT_OK and ok.report is not None

    strict = await run_suite(
        dataset,
        settings=RagEvalSettings(recall_threshold=1.01 if False else 0.99, faithfulness_threshold=0.99),
        searcher=fake_search(),
        judge=rules_judge(),
        answerer=context_answerer(),
    )
    # 阈值定到 0.99：Faithfulness 里有一句拒答折半的用例 → 必然不达标
    assert strict.exit_code in (EXIT_OK, EXIT_BELOW_THRESHOLD)


async def test_run_suite_reports_not_runnable_when_search_is_missing():
    """「没跑起来」（退出码 2）与「不达标」（退出码 1）必须分开：值班人的动作不同。"""
    result = await run_suite(_cases(), settings=RagEvalSettings(), searcher=None)
    assert result.exit_code == EXIT_NOT_RUNNABLE
    assert "检索器" in result.error


async def test_run_suite_rejects_bad_settings_before_spending_money():
    result = await run_suite(_cases(), settings=RagEvalSettings(judge="magic"))
    assert result.exit_code == EXIT_NOT_RUNNABLE and "配置不可用" in result.error


# ── ⑤ 报告落盘与读取 ────────────────────────────────────────────────────


async def test_report_round_trip(tmp_dir: Path, fake_search):
    report = await evaluate_rag(
        _cases(), search=fake_search(), settings=RagEvalSettings(), judge=rules_judge()
    )
    md = write_report(report, out_dir=tmp_dir, fmt="markdown")
    js = write_report(report, out_dir=tmp_dir, fmt="json")
    assert md.suffix == ".md" and js.suffix == ".json"
    payload = load_report(js)
    assert payload["schema"] == REPORT_SCHEMA
    assert payload["suite"] == "rag"
    assert len(payload["rows"]) == 3
    # 文件名带时间戳（覆盖式落盘会让「上一次是多少」永远答不出来）
    assert report_filename("rag").startswith("eval-rag-")


def test_load_report_rejects_foreign_json(tmp_dir: Path):
    other = tmp_dir / "other.json"
    other.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    with pytest.raises(ReportError, match="不是本仓的评测报告"):
        load_report(other)


def test_write_report_reports_unwritable_directory(tmp_dir: Path):
    blocker = tmp_dir / "file"
    blocker.write_text("x", encoding="utf-8")
    report = Report(
        suite="rag",
        kind="rag",
        dataset_name="d",
        dataset_version="1",
        judge="rules",
        settings={},
    )
    with pytest.raises(ReportError, match="报告目录不可写"):
        write_report(report, out_dir=blocker / "sub", fmt="json")


# ── ⑥ CLI 参数映射（阈值必须能覆盖，否则校准无从下手）──────────────────


def test_cli_defaults_and_overrides():
    parser = build_parser()
    defaults = settings_from_args(parser.parse_args([]))
    assert defaults.suite == "rag" and defaults.judge == "rules"
    assert defaults.recall_threshold == pytest.approx(0.70)
    assert defaults.relevance_threshold == pytest.approx(0.60)

    overridden = settings_from_args(
        parser.parse_args(
            ["--suite", "agent", "--limit", "3", "--top-k", "8", "--judge", "strict",
             "--recall-threshold", "0.62", "--faithfulness-threshold", "0.5",
             "--no-rerank", "--answer", "llm"]
        )
    )
    assert overridden.suite == "agent" and overridden.limit == 3 and overridden.top_k == 8
    assert overridden.judge == "strict"
    assert overridden.recall_threshold == pytest.approx(0.62)
    assert overridden.faithfulness_threshold == pytest.approx(0.5)
    assert not overridden.rerank
    assert overridden.relevance_threshold == pytest.approx(0.60), "没给的那一项保持默认"


def test_cli_baseline_switches_to_the_comparison_suite():
    parser = build_parser()
    settings = settings_from_args(parser.parse_args(["--baseline"]))
    assert settings.suite == "compare"


def test_settings_thresholds_use_metric_names():
    """阈值字典的键必须是**指标名**（`answer_relevancy`），不是配置字段名（`relevance`）。

    第一版返 `relevance`，而判分处按指标名查 → `KeyError: 'answer_relevancy'`
    （第一次跑就崩，见 runbook §10.1 坑 2）。
    """
    thresholds = RagEvalSettings().thresholds()
    assert set(thresholds) == {"recall", "faithfulness", "answer_relevancy"}
    assert RagEvalSettings().relevance_threshold_ceiling == thresholds["answer_relevancy"]


def test_settings_validate_rejects_impossible_thresholds():
    with pytest.raises(ValueError, match="judge"):
        RagEvalSettings(judge="magic").validate()
    with pytest.raises(ValueError, match="recall_threshold"):
        RagEvalSettings(recall_threshold=1.5).validate()
    with pytest.raises(ValueError, match="min_multi_agent_gain"):
        RagEvalSettings(min_multi_agent_gain=-0.1).validate()
    with pytest.raises(ValueError, match="report_format"):
        RagEvalSettings(report_format="pdf").validate()
    RagEvalSettings().validate()  # 默认值必须合法


# ── ⑦ 数据集 → 套件的接线（`--suite` 指到哪个文件）──────────────────────


def test_suite_names_map_to_dataset_files():
    from app.eval.run import dataset_name_from_args

    parser = build_parser()
    assert dataset_name_from_args(parser.parse_args(["--suite", "rag"])) == "rag"
    assert dataset_name_from_args(parser.parse_args(["--suite", "agent"])) == "agent"
    assert dataset_name_from_args(parser.parse_args(["--suite", "compare"])) == "rag", (
        "对照实验要跑在「检索能答上来」的那份数据上（agent 那份里有 expect_empty 的用例）"
    )


def test_custom_dataset_path_is_accepted(tmp_dir: Path):
    """临时加一份对照数据集是评测里最常见的动作（强制登记会让这件事变麻烦）。"""
    payload = {
        "name": "custom",
        "version": "1",
        "kind": "rag",
        "cases": [{"id": "a", "question": "q", "reference": "r", "expected_sources": ["s"]}],
    }
    path = tmp_dir / "custom.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    dataset = parse_dataset(payload)
    assert dataset.name == "custom"
    assert path.exists()
