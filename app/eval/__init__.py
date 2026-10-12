"""评测体系（第 14 步 eval）—— **门面**（`14.eval.md` §2 的对外接口在这里落地）。

```python
from app.eval import load_dataset, evaluate_rag, evaluate_agent, compare_single_vs_multi
```

## 为什么包名叫 `app.eval` 而不是文档里写的 `eval/`（**一处有意的差异**）

`14.eval.md` §2 写的入口是 `python -m eval.run`，§3 写的数据在 `eval/datasets/*.json`。
本仓把**代码**放在 `app/eval/`，把**数据**留在 `eval/datasets/`，理由三条：

1. `eval` 是 Python 内建函数名，把它做成顶层包会在 `import eval` 与 `eval(...)`
   之间制造长期困惑（IDE 与静态检查都会提示，而提示会被习惯性忽略）；
2. 本仓所有模块都在 `app/` 下（`app.rag` / `app.cache` / `app.graph` …），
   一个顶层 `eval/` 是唯一的例外，而例外需要理由 —— 这里没有足够的理由；
3. **文档里那条命令仍然可用**：仓库根有一个 `eval/run.py` 薄壳（只有 import 与转发），
   所以 `uv run python -m eval.run --suite rag` 逐字可跑。两条路进的是同一份实现。

## 四个出口

| 出口 | 谁用 | 落点 |
|---|---|---|
| `load_dataset` / `EvalSet` | 评测、报告、自定义脚本 | `dataset.py` |
| `evaluate_rag` | `--suite rag`、CI 闸门 | `rag_eval.py` |
| `evaluate_agent` | `--suite agent`、端到端验收 | `agent_eval.py` |
| `compare_single_vs_multi` | `--baseline`、简历上的「量化收益」 | `compare.py` |

⚠️ 本包**不 import FastAPI、不 import `app.api`**：评测是离线可跑的能力，
把 web 层拖进来会让「只想算一个 Recall」的调用方付出一次应用启动的代价
（与 `app/cache/__init__.py` 的同一条纪律）。
"""

from __future__ import annotations

from app.eval.agent_eval import AgentOutcome, evaluate_agent, score_agent_row
from app.eval.compare import Comparison, Variant, compare_single_vs_multi
from app.eval.dataset import (
    DATASET_DIR,
    DATASET_FILES,
    DatasetError,
    EvalCase,
    EvalSet,
    dataset_path,
    load_dataset,
    parse_dataset,
)
from app.eval.judge import (
    Judge,
    Judgment,
    build_judge,
    build_judge_messages,
    build_llm_judge,
    parse_judgment,
    rules_judge,
    strict_llm_judge,
)
from app.eval.metrics import (
    METRIC_NAMES,
    Aggregate,
    AnswerQuality,
    RetrievalQuality,
    aggregate,
    answer_quality,
    passed_overall,
    retrieval_quality,
)
from app.eval.pipeline import (
    Answerer,
    build_llm_answerer,
    build_single_agent_answerer,
    context_block,
    extractive_answerer,
)
from app.eval.rag_eval import CaseOutcome, evaluate_case, evaluate_rag
from app.eval.report import REPORT_SCHEMA, Report, ReportError, load_report, write_report
from app.eval.run import (
    EXIT_BELOW_THRESHOLD,
    EXIT_NOT_RUNNABLE,
    EXIT_OK,
    RunResult,
    build_parser,
    corpus_fingerprint,
    run_suite,
)
from app.eval.settings import (
    DEFAULT_FAITHFULNESS_THRESHOLD,
    DEFAULT_MIN_MULTI_AGENT_GAIN,
    DEFAULT_RECALL_THRESHOLD,
    DEFAULT_RELEVANCE_THRESHOLD,
    JUDGE_MODES,
    RagEvalSettings,
)
from app.eval.text import (
    contains,
    coverage,
    is_hedged,
    matching_key_facts,
    missing_key_facts,
    normalize_text,
    unsupported_numbers,
)

__all__ = [
    # 数据
    "DATASET_DIR",
    "DATASET_FILES",
    "DatasetError",
    "EvalCase",
    "EvalSet",
    "dataset_path",
    "load_dataset",
    "parse_dataset",
    # 判据与指标
    "METRIC_NAMES",
    "Aggregate",
    "AnswerQuality",
    "RetrievalQuality",
    "aggregate",
    "answer_quality",
    "contains",
    "coverage",
    "is_hedged",
    "matching_key_facts",
    "missing_key_facts",
    "normalize_text",
    "passed_overall",
    "retrieval_quality",
    "unsupported_numbers",
    # 判官
    "Judge",
    "Judgment",
    "build_judge",
    "build_judge_messages",
    "build_llm_judge",
    "parse_judgment",
    "rules_judge",
    "strict_llm_judge",
    # 合成
    "Answerer",
    "build_llm_answerer",
    "build_single_agent_answerer",
    "context_block",
    "extractive_answerer",
    # 三个套件
    "AgentOutcome",
    "CaseOutcome",
    "Comparison",
    "Variant",
    "compare_single_vs_multi",
    "evaluate_agent",
    "evaluate_case",
    "evaluate_rag",
    "score_agent_row",
    # 报告
    "REPORT_SCHEMA",
    "Report",
    "ReportError",
    "load_report",
    "write_report",
    # 入口
    "EXIT_BELOW_THRESHOLD",
    "EXIT_NOT_RUNNABLE",
    "EXIT_OK",
    "RunResult",
    "build_parser",
    "corpus_fingerprint",
    "run_suite",
    # 配置
    "DEFAULT_FAITHFULNESS_THRESHOLD",
    "DEFAULT_MIN_MULTI_AGENT_GAIN",
    "DEFAULT_RECALL_THRESHOLD",
    "DEFAULT_RELEVANCE_THRESHOLD",
    "JUDGE_MODES",
    "RagEvalSettings",
]
