# 模块 14 · eval（评测体系）

## 目的

回答"这次改动是变好还是变坏"：用 golden dataset 把答案变成可比的分数，并能对"单 Agent vs 多 Agent"做量化对照。

> ⚠️ **技术选型以代码为准**：本仓**不装 ragas**。指标**名**对齐 Ragas（`recall` / `faithfulness` / `answer_relevancy`），**实现是自己写的判据**（`app/eval/metrics.py:19-37` 明确给了三条理由：会换掉锁定的 22 个包、口径本身只是一段可读定义、我们要能在离线跑）。`docs/modules/14.eval.md` 与 `docs/problem-solving.md` G1 的"用 Ragas 评测"说法需按此读。

## 关键结论

1. **两份 golden dataset**：`eval/datasets/rag_golden_v1.json`（15 条）、`eval/datasets/agent_golden_v1.json`（10 条）。装载 `load_dataset()`（`app/eval/dataset.py:302`），校验 `_assert_facts_related()`（:218）。
2. **三个套件**：`rag` / `agent` / `compare`（`app/eval/run.py:56 SUITES`）。
3. **三指标**（`app/eval/metrics.py`，全部是纯函数、可离线）：
   - `recall`：检索命中率（期望来源是否在 top-k）；
   - `faithfulness = (0.6·事实点覆盖 + 0.4·数值支持率) × 不拒答系数`（:43）；
   - `answer_relevancy = (0.5·词项覆盖 + 0.3·事实点覆盖 + 0.2·不跑题) × 篇幅系数`（:44）。
4. **判官三档**：`rules`（默认，离线零成本）/ `strict` / `llm`（`app/eval/judge.py:312 build_judge`）。
   ⚠️ 注意：`rules` **不是** Ragas 的语义判分，它是 n-gram 与字段覆盖率 —— 所以分数不能和 ragas 论文数值横比。
5. **阈值**（都在代码里，**不进 `.env`**）：`recall ≥ 0.70`、`faithfulness ≥ 0.70`、`relevance ≥ 0.60`、多 Agent 最小增益 `0.15`（`app/eval/settings.py:37-42`）。
6. **退出码是 CI 闸门**：`EXIT_OK=0` / `EXIT_BELOW_THRESHOLD=1` / `EXIT_NOT_RUNNABLE=2`（`app/eval/run.py:51-53`）。
7. **纪律：`app/eval` 不 import FastAPI**（`docs/runbook/14.eval-runbook.md` §2.4）—— 只想算一个 Recall 的调用方不该付一次应用启动代价。

## 实际用法

```bash
uv run python -m eval.run --suite rag --dataset rag_golden_v1      # 只量检索
uv run python -m eval.run --suite agent --dataset agent_golden_v1  # 量整张图
uv run python -m eval.run --suite compare                          # 单 vs 多 Agent
```

```python
from app.eval import load_dataset, evaluate_rag, evaluate_agent, compare_single_vs_multi
```

## 待办 / 已知边界

- ⚠️ **"多 Agent 优于单 Agent"的量化结论没有落地数值**：`docs/problem-solving.md` G1 的验证指标仍是 `X% → Y%` 占位，`compare` 套件存在但**未见到成文的结果报告**。→ `../../03-待确认清单.md` Q-05。
- ⚠️ 评测**未接入 CI**（仓库无 `.github/`，见 `../../02-异常清单.md` E-05）。退出码闸门目前只能人工跑。
- `docs/modules/14.eval.md` 的证据章标题是「本轮实测，2026-10-12」，早于换 embedding 后端（提交 `0447b11`），历史数字不可直接复用。

## 坑

| 坑 | 依据 |
|---|---|
| 语料没灌就评测 → recall 全 0 | `docs/runbook/14.eval-runbook.md` §1.1 ② |
| 把 `rules` 判官的分数当 ragas 论文数值对比 | `app/eval/metrics.py:19-37` |
| 以为阈值能从 `.env` 改 | `app/eval/settings.py`（全在代码） |

## 来源

- `docs/modules/14.eval.md`（94 行）、`docs/runbook/14.eval-runbook.md`（1129 行）
- `docs/problem-solving.md` G1
- 代码：`app/eval/*.py`（13 文件）、`eval/`（薄壳）、`eval/datasets/*.json`
