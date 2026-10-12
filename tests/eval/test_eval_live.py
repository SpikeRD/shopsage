"""真依赖的评测用例（**默认 skip**，需要显式开门）。

与第 10/12/13 步同一条纪律：真依赖的用例默认不跑，否则「没起依赖就红一片」
会让本步的验收基线不可用。两种开门方式：

| 门 | 打开什么 | 命令 |
|---|---|---|
| `--rag-live`（`pytest.mark.rag_live`） | 真检索（embedding + rerank + pgvector） | `uv run pytest -q tests/eval --rag-live` |
| `llm_live`（`pytest.mark.llm_live`） | 真模型（合成与判官） | `uv run pytest -q -m llm_live tests/eval` |

⚠️ **真检索那一组会把整份 15 条 golden set 跑一遍**（实测 ~50s，含模型加载 36s）——
这是刻意的：它量的是「真实链路上的 Recall / Faithfulness / Relevance」，
而那正是 `14.eval.md` §8 验收要的数字。跑之前请确认已灌语料：

```bash
uv run python -m scripts.load_knowledge_base
```
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.rag_live
async def test_live_rag_pipeline_meets_documented_thresholds():
    """真检索 + 片段原文合成 + 规则判官：三指标必须达到默认阈值。

    这条是「本步的验收基线」：它同时钉住
    ① 语料已灌进 pgvector；② 检索配置（维度/rerank/候选数）没被改坏；
    ③ golden set 与语料仍然对得上（改了一份政策文档而没改 golden set 时，这里会红）。
    """
    from app.eval.dataset import load_dataset
    from app.eval.judge import rules_judge
    from app.eval.pipeline import context_answerer
    from app.eval.rag_eval import evaluate_rag
    from app.eval.run import build_default_searcher
    from app.eval.settings import RagEvalSettings

    dataset = load_dataset("rag", root=REPO_ROOT)
    settings = RagEvalSettings(top_k=5)
    report = await evaluate_rag(
        dataset,
        search=build_default_searcher(rerank=True, top_k_default=5),
        settings=settings,
        judge=rules_judge(),
        answerer=context_answerer(),
    )
    overall = report.overall()
    print(report.summary_line())
    assert overall.count == len(dataset)
    assert overall.recall >= settings.recall_threshold
    assert overall.faithfulness >= settings.faithfulness_threshold
    assert overall.answer_relevancy >= settings.relevance_threshold


@pytest.mark.rag_live
async def test_live_retrieval_finds_every_expected_document():
    """细粒度那条：15 条用例的期望文档**全部**要在 top-5 里（Recall@5 = 1.0）。

    比总阈值更严的判据，用来第一时间发现「某一份文档突然检索不到了」。
    """
    from app.eval.dataset import load_dataset
    from app.eval.judge import rules_judge
    from app.eval.pipeline import context_answerer
    from app.eval.rag_eval import evaluate_rag
    from app.eval.run import build_default_searcher
    from app.eval.settings import RagEvalSettings

    dataset = load_dataset("rag", root=REPO_ROOT)
    report = await evaluate_rag(
        dataset,
        search=build_default_searcher(rerank=True, top_k_default=5),
        settings=RagEvalSettings(top_k=5),
        judge=rules_judge(),
        answerer=context_answerer(),
    )
    missed = {row["id"]: row["missing_sources"] for row in report.rows if row["missing_sources"]}
    assert not missed, f"这些用例漏检了期望文档：{missed}"


@pytest.mark.rag_live
def test_live_corpus_fingerprint_is_available():
    """语料指纹取得到（它进报告，让「换语料导致的掉分」可归因）。"""
    from app.eval.run import corpus_fingerprint

    fingerprint = corpus_fingerprint()
    assert fingerprint.startswith("chunks=")
    assert "rev=" in fingerprint


@pytest.mark.llm_live
async def test_live_llm_judge_and_answerer_run_end_to_end():
    """真模型那一组：真判官 + 真合成，跑前 3 条（省时省钱，形态与全量一致）。

    它回答的问题是「接上真模型之后这条链路还跑不跑得通」——
    而不是「分数是多少」（那需要全量 + 多次运行，见 runbook §6.3）。
    """
    from app.eval.dataset import load_dataset
    from app.eval.judge import build_llm_judge
    from app.eval.pipeline import build_llm_answerer
    from app.eval.rag_eval import evaluate_rag
    from app.eval.run import build_default_searcher
    from app.eval.settings import RagEvalSettings

    dataset = load_dataset("rag", root=REPO_ROOT).subset(limit=3)
    report = await evaluate_rag(
        dataset,
        search=build_default_searcher(rerank=True, top_k_default=5),
        settings=RagEvalSettings(top_k=5, judge="llm"),
        judge=build_llm_judge(),
        answerer=build_llm_answerer(),
    )
    print(report.summary_line())
    assert report.overall().count == 3
    assert all(row["answer"].strip() for row in report.rows), "真模型合成不该给出空答案"
    assert all(not row["judge"]["degraded"] for row in report.rows), "判官不该降级"
