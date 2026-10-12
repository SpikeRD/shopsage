"""评测配置面（`14.eval.md` §3「阈值」的落点）。

## 为什么这些阈值**不进** `app/config/settings.py`（与第 13 步同一条口径）

`13.cache.md` 的阈值（0.93）是「与这套语料 + 这个 embedding 模型绑死」的语义选择，
换环境要重新校准，所以它留在 `CacheSettings` 里、不进 `.env`。
评测阈值更极端：它**只在跑评测时**有意义，业务进程一辈子读不到它。
把它做成环境变量，只会带来「两份环境跑出两种判决」而没人知道谁对。

## 但有一条例外：它**必须能被命令行覆盖**

评测阈值的用途是「卡住 CI」与「校准用哪条线合适」，这两件事都要求
「不改进仓库的任何文件，先试试 0.62 会怎样」。所以 `RagEvalSettings`
是一个**构造参数全部公开**的 dataclass，CLI 直接映射到它
（`--recall-threshold 0.62`），而不是让调用方去改这里再提交。

## 默认值是怎么定出来的

| 阈值 | 默认 | 依据（本仓实测，见 runbook §4.3 / §5.1） |
|---|---|---|
| `recall_threshold` | 0.70 | 15 条 golden 用例上实测 Recall@5 = 0.933，留出「换 chunk 策略会掉 1~2 条」的余量 |
| `faithfulness_threshold` | 0.70 | 规则判据（事实点覆盖）实测 0.93；留余量给「答案更啰嗦但同样正确」这类波动 |
| `relevance_threshold` | 0.60 | 比前两条低：它是三指标里**最主观**的一维（措辞差异大），当哨兵用，不当闸门 |
| `min_multi_agent_gain` | 0.15 | 「多 Agent 比单 Agent 好多少才算好」——见 `compare.py` 的说明（这条是有意定得**保守**的） |

⚠️ 这些数字**不是**从 Ragas 的默认值抄来的（Ragas 的指标没有内置「合格线」，
它的输出是一个 0~1 的分数，合格线由使用方定）。所以本仓把它们摆在这里、
并写明依据 —— 凭据写不清的阈值，三个月后没人敢改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

#: 三指标的默认合格线（依据见模块 docstring 的表格）
DEFAULT_RECALL_THRESHOLD: Final[float] = 0.70
DEFAULT_FAITHFULNESS_THRESHOLD: Final[float] = 0.70
DEFAULT_RELEVANCE_THRESHOLD: Final[float] = 0.60

#: 多 Agent 相对单 Agent 的最低增益（`compare.py` 用）
DEFAULT_MIN_MULTI_AGENT_GAIN: Final[float] = 0.15

#: 判分后端：`rules`（纯离线）/ `strict`（模拟真实判官的严格口径）/ `llm`（真模型判官）
JUDGE_MODES: Final[tuple[str, ...]] = ("rules", "strict", "llm")

#: 报告格式
REPORT_FORMATS: Final[tuple[str, ...]] = ("markdown", "json")


@dataclass(frozen=True)
class RagEvalSettings:
    """一次评测运行的**全部**可调项（阈值 + 数据 + 判分 + 落盘）。

    刻意不做成 `BaseSettings`：它不读 `.env`（理由见模块 docstring），
    也不该有单例 —— 一次进程里跑「阈值 0.7 与 0.62 两组对照」是评测的常规动作，
    单例会让第二组静默复用第一组的配置。
    """

    #: 数据集的逻辑名（`rag` / `agent`）或路径
    suite: str = "rag"
    #: 只跑这些用例 id（空 = 全跑）
    only: tuple[str, ...] = ()
    #: 最多跑多少条（None = 全跑）
    limit: int | None = None
    #: 检索取几条（送进答案合成与 Recall 的 K）
    top_k: int = 5
    #: 是否对候选做 rerank（对照实验用；`False` 时退回 RRF 融合顺序）
    rerank: bool = True
    #: 判分后端（`JUDGE_MODES`）
    judge: str = "rules"
    #: 判官模型的温度（0 = 尽量确定；LLM 判分最怕「同一份答案两次分数不同」）
    judge_temperature: float = 0.0
    #: 判官每条的候选答案长度上限（防止把整篇文档塞进判分 prompt）
    judge_answer_chars: int = 2000
    #: 三指标的合格线
    recall_threshold: float = DEFAULT_RECALL_THRESHOLD
    faithfulness_threshold: float = DEFAULT_FAITHFULNESS_THRESHOLD
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD
    #: 多 Agent 最低增益
    min_multi_agent_gain: float = DEFAULT_MIN_MULTI_AGENT_GAIN
    #: `--baseline`：是否同时跑单 Agent 对照（对照要额外一次生成，钱在这里花）
    baseline: bool = False
    #: 报告落盘目录（`None` = 不落盘，只在终端打印）
    out_dir: str | None = None
    #: 报告格式
    report_format: str = "markdown"
    #: 是否把逐条明细也写进 JSON（CI 归档用；Markdown 只放汇总与失败样本）
    include_details: bool = True
    #: 知识库快照指纹（进报告，让「换了语料之后分数变了」可归因）；空 = 不记
    corpus_fingerprint: str = ""

    def validate(self) -> None:
        """跑之前的自检：**把配错的组合在花掉钱之前拦住**。

        每一条都对应一个「不拦就会静默给出错误数字」的形态（见各条的理由）。
        """
        if self.judge not in JUDGE_MODES:
            raise ValueError(f"judge 必须是 {JUDGE_MODES} 之一，收到 {self.judge!r}")
        if self.report_format not in REPORT_FORMATS:
            raise ValueError(f"report_format 必须是 {REPORT_FORMATS} 之一，收到 {self.report_format!r}")
        if self.top_k < 1:
            raise ValueError(f"top_k 必须 >= 1，收到 {self.top_k}")
        if self.limit is not None and self.limit < 0:
            raise ValueError(f"limit 不能为负，收到 {self.limit}")
        for name in ("recall_threshold", "faithfulness_threshold", "relevance_threshold"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} 必须在 [0, 1] 里，收到 {value}")
        if self.min_multi_agent_gain < 0:
            # 负增益阈值＝「多 Agent 比单 Agent 差也算通过」，那是把对照题的意义抹掉。
            raise ValueError(
                f"min_multi_agent_gain 不能为负，收到 {self.min_multi_agent_gain}；"
                "想「只记录差距、不做判定」请传 0.0（它表示「持平即通过」）"
            )

    def thresholds(self) -> dict[str, float]:
        """三指标合格线（报告里要原样打印，否则读报告的人不知道分是怎么判的）。

        ⚠️ 键用的是**指标名**（`answer_relevancy`，与报告的 JSON 字段、Ragas 口径一致），
        而不是配置字段名（`relevance`）。第一版这里返 `relevance`，而
        `rag_eval._score_row` 按指标名去查 → `KeyError: 'answer_relevancy'`
        （第一次跑就崩，见 runbook §10.1 坑 2）。
        两套名字只允许在**这一处**对齐：别的地方一律用指标名。
        """
        return {
            "recall": float(self.recall_threshold),
            "faithfulness": float(self.faithfulness_threshold),
            "answer_relevancy": float(self.relevance_threshold),
        }

    @property
    def relevance_threshold_ceiling(self) -> float:
        """兼容别名：`relevance` 是**配置字段名**，`answer_relevancy` 是**指标名**。

        留着它是因为 CLI 的 `--relevance-threshold` 与 `1.config-guide.md` 的命名习惯
        都叫 relevance —— 但代码内部只认 `thresholds()` 那一份键。
        """
        return float(self.relevance_threshold)

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "only": list(self.only),
            "limit": self.limit,
            "top_k": self.top_k,
            "rerank": self.rerank,
            "judge": self.judge,
            "judge_temperature": self.judge_temperature,
            "judge_answer_chars": self.judge_answer_chars,
            "thresholds": self.thresholds(),
            "min_multi_agent_gain": self.min_multi_agent_gain,
            "baseline": self.baseline,
            "out_dir": self.out_dir,
            "report_format": self.report_format,
            "corpus_fingerprint": self.corpus_fingerprint,
        }


__all__ = [
    "DEFAULT_FAITHFULNESS_THRESHOLD",
    "DEFAULT_MIN_MULTI_AGENT_GAIN",
    "DEFAULT_RECALL_THRESHOLD",
    "DEFAULT_RELEVANCE_THRESHOLD",
    "JUDGE_MODES",
    "REPORT_FORMATS",
    "RagEvalSettings",
]
