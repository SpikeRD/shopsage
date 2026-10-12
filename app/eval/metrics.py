"""三指标与聚合口径（`14.eval.md` §5 第一条、§4 第 2 条）。

## 三指标各测什么（这是本模块唯一的解释，报告与 runbook 都引它）

| 指标 | 回答的问题 | 涨了说明 | 掉了说明 |
|---|---|---|---|
| **Recall** | 该检索到的有没有检索到 | 分块/embedding/rerank 在变好 | 召回漏了 —— 后面两步再强也没用 |
| **Faithfulness** | 答案是否**忠于检索结果**（不幻觉） | prompt 的材料边界更严 | 模型在编，或检索没给它材料 |
| **Answer Relevance** | 答案是否**切题** | 措辞更贴问题 | 答非所问 / 空话 / 拒答 |

⚠️ 三者**必须一起看**，因为它们的失败模式互相伪装（本仓实测）：

* Faithfulness 低 + Recall 高 → **模型的锅**（材料给到了它没用）；
* Faithfulness 低 + Recall 低 → **检索的锅**（材料没给到，模型只能编）；
* Relevance 高 + Faithfulness 高 + Recall 低 → **最危险的一格**：
  答案流畅、切题、且「忠于它拿到的那点材料」，但那点材料根本不是答案 ——
  只看后两个指标会以为一切正常。

## 为什么把 Ragas 的三指标实现成「判据组合」而不是调用 ragas 库

`14.eval.md` §3 把 `ragas` 列成依赖，本仓**没有**把它装进默认依赖，理由是三条实测事实：

1. **它会换掉我们锁定的版本**：解析结果为 22 个新包（`langchain` / `langchain-openai` /
   `datasets` / `pandas` / `pyarrow` / `instructor` / `scikit-network` …），
   并会把 `jiter` 0.17→0.14、`fsspec` 2026.9→2026.7 —— 而 `litellm` 与 `openai`
   都在用它们。为了一个只在评测时跑的库去动生产依赖的版本，代价与收益不成比例。
2. **它的判分内核是「LLM 打分」**：`faithfulness` 要拆 claim 再逐条问模型「这条有依据吗」，
   `answer_relevancy` 要反向生成问题再算相似度 —— 105 条 × 多次调用是真实开销，
   而且**离线没法复现**（LLM 有随机性）。而第 14 步的验收（§8）要求「低于阈值即阻断合并」，
   一个「同一次提交今天 0.71、明天 0.68」的闸门会立刻被人关掉。
3. **它的口径本身只是一段可读的定义**（Ragas 论文的贡献是「把 RAG 评测拆成可测量的几维」，
   而不是某个不可替代的实现）。本模块把这几个维度**逐条实现成显式判据**，
   判据名进报告 —— 掉分时能一眼看出是哪一条掉的（`missing_key_facts` /
   `unsupported_numbers` / `hedged`），这不是黑箱分数能给的信息。

结论：**接口与指标名对齐 Ragas**（`recall` / `faithfulness` / `answer_relevancy`），
判分实现是本仓自己的。要不要接真 ragas 作为**可选对照**，见 runbook §3.6。

## 打分公式（三条都不引入任何魔法常数）

```
Recall@K        = 命中的期望文档数 / 期望文档总数            ∈ [0, 1]
faithfulness    = (0.6·事实点覆盖 + 0.4·数值支持率) × 不拒答系数
answer_relevancy= (0.5·词项覆盖 + 0.3·事实点覆盖 + 0.2·不跑题) × 篇幅系数
```

`0.6/0.4` 与 `0.5/0.3/0.2` 是**权重**而不是阈值：它们的和分别是 1.0，含义是
「事实点覆盖是主判据，数值支持率是它的防线」与「相关性以问题词项为主，
事实点是它的补充」。权重写在代码里而不是配置里，因为改它等于改指标定义 ——
那不是运行时该调的东西（要改就改这里并重跑基线）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from app.eval.dataset import EvalCase
from app.eval.text import (
    coverage,
    digit_tokens,
    is_emptyish,
    is_hedged,
    matching_key_facts,
    missing_key_facts,
    normalize_text,
    tokenish_length,
    unsupported_numbers,
)

#: 三指标的规范名（报告、JSON、阈值键都用这三个字符串）
METRIC_NAMES: Final[tuple[str, ...]] = ("recall", "faithfulness", "answer_relevancy")

#: 「短到不可能答完」的下限：少于这么多归一字符就把相关性按比例折算。
#: 30 个中文字符大约是一句半话 —— 政策类问题的完整回答（含条件与数字）不会这么短。
SHORT_ANSWER_CHARS: Final[int] = 30

#: 事实点覆盖在 faithfulness 里的权重（其余给数值支持率）
FACT_WEIGHT: Final[float] = 0.6
#: 相关性里「问题词项覆盖」的权重
QUERY_TERM_WEIGHT: Final[float] = 0.5
#: 相关性里「事实点覆盖」的权重
RELEVANCE_FACT_WEIGHT: Final[float] = 0.3
#: 相关性里「不跑题」的权重
ON_TOPIC_WEIGHT: Final[float] = 0.2


@dataclass(frozen=True)
class RetrievalQuality:
    """一次检索的判分（Recall 那一路）。"""

    expected: tuple[str, ...]
    retrieved: tuple[str, ...]
    hits: tuple[str, ...]
    #: 命中的期望文档（归一后比对，`docs/kb/x.md` 与 `x.md` 视为同一份）
    matched: tuple[str, ...]
    missing: tuple[str, ...]
    recall: float
    #: 检索到的第一条的得分（诊断用：**Recall 高但 score 很低**说明排序不可信）
    top_score: float = 0.0
    expect_empty: bool = False
    empty_ok: bool = True
    k: int = 5
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "recall": round(self.recall, 4),
            "matched": list(self.matched),
            "missing": list(self.missing),
            "retrieved": list(self.retrieved),
            "hits": len(self.hits),
            "top_score": round(self.top_score, 4),
            "k": self.k,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


@dataclass(frozen=True)
class AnswerQuality:
    """一次回答的判分（Faithfulness 与 Relevance 两路，**同一份事实点**上算出来）。"""

    #: 事实点命中率 ∈ [0, 1]（`text.coverage`）
    fact_coverage: float
    matched_facts: tuple[str, ...]
    missing_facts: tuple[str, ...]
    #: 答案里出现、检索上下文里没有的数字（Faithfulness 的机械信号）
    unsupported_numbers: tuple[str, ...]
    #: 答案里的数字总数与「有支撑」的个数（数值支持率的分母/分子）
    number_total: int
    number_supported: int
    faithfulness: float
    relevance: float
    #: 判据明细（报告里逐条打印 —— 掉分时这比分数有用得多）
    findings: tuple[tuple[str, bool, str], ...] = ()
    hedge: bool = False
    answered: bool = True
    length: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "faithfulness": round(self.faithfulness, 4),
            "answer_relevancy": round(self.relevance, 4),
            "fact_coverage": round(self.fact_coverage, 4),
            "missing_facts": list(self.missing_facts),
            "unsupported_numbers": list(self.unsupported_numbers),
            "number_supported": self.number_supported,
            "number_total": self.number_total,
            "hedge": self.hedge,
            "length": self.length,
            "findings": [
                {"name": name, "passed": passed, "detail": detail}
                for name, passed, detail in self.findings
            ],
        }


def _normalize_source(value: str) -> str:
    """文档名的比对形态：`docs/kb/退货政策.md` → `退货政策`。

    为什么去掉目录与扩展名：golden set 里手写 `docs/kb/退货政策.md` 太长且容易写错，
    而「哪一份文档」这个信息在 basename 上已经完整（本仓语料没有重名文档）。
    若将来出现同名文档，这条归一必须收紧 —— 那是一个**会让 Recall 虚高**的口径，
    所以它被写成一个独立函数而不是内联表达式（改的时候只改一处）。
    """
    text = str(value or "").strip().replace("\\", "/")
    name = text.rsplit("/", 1)[-1]
    for suffix in (".md", ".html", ".txt", ".pdf"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    return normalize_text(name)


def retrieval_quality(
    *,
    case: EvalCase,
    retrieved: list[str],
    scores: list[float] | None = None,
    k: int = 5,
    elapsed_ms: float = 0.0,
) -> RetrievalQuality:
    """算 Recall@K（以及「命中了哪几份、漏了哪几份」）。

    `expect_empty=True` 的用例走**另一条判据**：它不看命中率，看「有没有静默答上来」——
    知识库里确实没有答案的问题，检索到东西反而是错（会诱使模型编造）。
    """
    normalized_retrieved = tuple(_normalize_source(item) for item in retrieved)
    hit_sources = tuple(dict.fromkeys(item for item in normalized_retrieved if item))

    if case.expect_empty or not case.expected_sources:
        # 两类用例走这条：显式声明 `expect_empty=True`（知识库里确实没有 / 走的是 data 分支），
        # 以及没写期望来源的用例。判据是「**有没有静默答上来**」而不是命中率 ——
        # 知识库里确实没有答案的问题，检索到东西反而是错（会诱使模型编造）。
        empty_ok = not hit_sources
        return RetrievalQuality(
            expected=tuple(case.expected_sources),
            retrieved=tuple(retrieved),
            hits=hit_sources,
            matched=(),
            missing=(),
            recall=1.0 if empty_ok else 0.0,
            top_score=float(scores[0]) if scores else 0.0,
            expect_empty=True,
            empty_ok=empty_ok,
            k=k,
            elapsed_ms=elapsed_ms,
        )

    expected_norm = tuple(dict.fromkeys(_normalize_source(item) for item in case.expected_sources))
    matched = tuple(item for item in expected_norm if item in hit_sources)
    missing = tuple(item for item in expected_norm if item not in hit_sources)
    recall = len(matched) / len(expected_norm) if expected_norm else 0.0
    return RetrievalQuality(
        expected=case.expected_sources,
        retrieved=tuple(retrieved),
        hits=hit_sources,
        matched=matched,
        missing=missing,
        recall=recall,
        top_score=float(scores[0]) if scores else 0.0,
        k=k,
        elapsed_ms=elapsed_ms,
    )


def _query_terms(question: str) -> tuple[str, ...]:
    """问题的**内容词**（2-gram 去重），用于「答案有没有接住问题」。

    中文不做分词（本仓没有分词器依赖），所以用 2-gram：它的召回率高、精度低，
    正好适合当「切题」的粗判据 —— 精度那部分由事实点覆盖补（见相关性公式的权重）。

    ⚠️ 刻意**去掉问题里的疑问词与语气词**（`吗` / `呢` / `怎么` / `是什么`…）：
    它们是所有问题共有的，留在里面会让「任何一段中文」都覆盖到 20%~30%，
    于是相关性这一维的分差被压平。
    """
    normalized = normalize_text(question)
    for filler in ("是什么", "怎么样", "怎么", "如何", "哪些", "多少", "多久", "吗", "呢", "的", "了"):
        normalized = normalized.replace(filler, "")
    if len(normalized) < 2:
        return (normalized,) if normalized else ()
    return tuple(dict.fromkeys(normalized[i : i + 2] for i in range(len(normalized) - 1)))


def answer_quality(
    *,
    case: EvalCase,
    answer: str,
    context: str,
    blocklist: tuple[str, ...] = (),
) -> AnswerQuality:
    """算 Faithfulness 与 Answer Relevance（判据逐条落在 `findings` 里）。

    参数 `context` 是**这次回答真正看到的材料**（检索命中的原文拼接）。
    它是 Faithfulness 的分母来源：同一个答案在「材料充分」与「材料为空」两种情况下，
    faithfulness 完全不同 —— 这正是这个指标的意义所在。
    """
    text = str(answer or "")
    findings: list[tuple[str, bool, str]] = []

    length = tokenish_length(text)
    answered = not is_emptyish(text)
    hedge = is_hedged(text)

    findings.append(("answered", answered, "有内容" if answered else "空答案（或只有标点）"))
    findings.append(
        (
            "not_hedged",
            not hedge,
            "正常作答" if not hedge else "命中拒答/推脱话术（答案没答到问题上）",
        )
    )

    # ── Faithfulness：事实点覆盖 + 数值支持率 ────────────────────────────────
    fact_cov = coverage(text, case.key_facts)
    matched = matching_key_facts(text, case.key_facts)
    missing = missing_key_facts(text, case.key_facts)
    findings.append(
        (
            "key_facts",
            fact_cov >= 1.0,
            f"命中 {len(matched)}/{len(case.key_facts)}" + (f"，缺：{list(missing)}" if missing else ""),
        )
    )

    answer_numbers = digit_tokens(text)
    context_numbers = digit_tokens(context)
    supported = answer_numbers & context_numbers
    unsupported = unsupported_numbers(text, context)
    # ⚠️ 只有**答案里有数字**时这条判据才参与打分。答案里一个数字都没有时，
    #    「数值支持率」= 1.0（没有可编造的数字），而不是 0（那会把纯定性回答判死）。
    number_ratio = 1.0 if not answer_numbers else len(supported) / len(answer_numbers)
    # 这条判据**刻意不是**「有一个数字不在材料里就判失败」——本仓实测这么写会误伤：
    # 合规的对客回答常常带上「以结算页显示的金额为准」这类**来自别处**的数字
    # （运费示例、会员门槛），而它们来自语料里的其它片段，不是编造。
    # 真正要拦的是「**事实点一个都没命中，却凭空给出了数字**」——
    # 那是「模型没拿到材料还硬答」的签名（`kb-missing-answer` 那条就是为它设的）。
    numbers_finding_passed = not unsupported or bool(matched)
    findings.append(
        (
            "numbers_supported",
            numbers_finding_passed,
            f"答案里的数字 {sorted(answer_numbers) or '（无）'}"
            + (f"，其中 {list(unsupported)} 不在本次检索材料里" if unsupported else "，全部有材料支撑"),
        )
    )

    faithfulness = (FACT_WEIGHT * fact_cov + (1 - FACT_WEIGHT) * number_ratio)
    if not answered:
        faithfulness = 0.0
    elif hedge:
        # 拒答不是幻觉，但它也**没有**忠于材料（材料给了它却没答）。折半是刻意的：
        # 判成 0 会让「检索没给到材料所以如实说不知道」这种正确行为被惩罚，
        # 判成 1 会让「一律拒答」拿满分（那是评测里最常见的作弊路径）。
        faithfulness *= 0.5
    findings.append(
        (
            "faithful",
            faithfulness >= 0.7,
            f"事实点 {fact_cov:.2f} × 数值支持 {number_ratio:.2f}"
            + ("；拒答折半" if hedge else ""),
        )
    )

    # ── Answer Relevance：问题词项 + 事实点 + 不跑题 ─────────────────────────
    terms = _query_terms(case.question)
    haystack = normalize_text(text)
    term_hit = (
        sum(1 for term in terms if term in haystack) / len(terms) if terms else 0.0
    )
    on_topic = bool(terms) and term_hit > 0.0
    relevance = (
        QUERY_TERM_WEIGHT * term_hit
        + RELEVANCE_FACT_WEIGHT * fact_cov
        + ON_TOPIC_WEIGHT * (1.0 if on_topic else 0.0)
    )
    if length < SHORT_ANSWER_CHARS:
        # 篇幅系数：一句话回答一个需要条件与数字的政策问题，通常意味着没答完。
        # 用**线性折扣**而不是清零 —— 短答案可能只是精炼（本仓的对客模板要求 ≤300 字）。
        relevance *= max(0.3, length / SHORT_ANSWER_CHARS)
    if not answered:
        relevance = 0.0
    findings.append(
        (
            "relevant",
            relevance >= 0.6,
            f"问题词项覆盖 {term_hit:.2f}（{len(terms)} 个 2-gram）× 事实点 {fact_cov:.2f}"
            + (f"；篇幅 {length} 字偏短，已折扣" if length < SHORT_ANSWER_CHARS else ""),
        )
    )

    return AnswerQuality(
        fact_coverage=fact_cov,
        matched_facts=matched,
        missing_facts=missing,
        unsupported_numbers=unsupported,
        number_total=len(answer_numbers),
        number_supported=len(supported),
        faithfulness=faithfulness,
        relevance=relevance,
        findings=tuple(findings),
        hedge=hedge,
        answered=answered,
        length=length,
        note=case.note,
    )


@dataclass(frozen=True)
class Aggregate:
    """一组用例的聚合（**分母里带条数**，因为「0.0」与「没跑」必须能区分）。"""

    name: str
    count: int
    recall: float
    faithfulness: float
    answer_relevancy: float
    #: 逐维度的「通过条数 / 总条数」
    passed: dict[str, int] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": self.count,
            "recall": round(self.recall, 4),
            "faithfulness": round(self.faithfulness, 4),
            "answer_relevancy": round(self.answer_relevancy, 4),
            "passed": dict(self.passed),
            **self.extra,
        }

    def mean_of(self, metric: str) -> float:
        return float(getattr(self, "answer_relevancy" if metric == "answer_relevancy" else metric))


def mean(values: list[float]) -> float:
    """算术平均；空列表返回 0.0（并由调用方在报告里显式打印 `count=0`）。

    为什么空列表返回 0.0 而不是抛错：`--only` 挑出一批用例跑完之后，
    「某一维没有任何样本」是**正常**的（例如全挑了 expect_empty 的用例）。
    抛错会让「跑一份自定义子集」这件事变得没法做，而 0.0 + `count=0` 是如实的。
    """
    return sum(values) / len(values) if values else 0.0


def aggregate(name: str, rows: list[tuple[float, float, float]], *, thresholds: dict[str, float]) -> Aggregate:
    """把逐条的 `(recall, faithfulness, relevance)` 聚合成一组指标。

    ⚠️ **Recall 用 `float("nan")` 表示「这条不适用于 Recall」**，聚合时被跳过：
    这是「没有期望来源」的用例（`expect_empty`）与「期望来源一条都没命中」的用例
    必须区分的地方 —— 前者没有分母，后者是**真的 0 分**。
    混在一起的后果本仓实测过：agent 套件里 6 条 `expect_empty` 用例
    把 Recall 从 0.90 拉到 0.36，而报告里看起来像「检索坏了」（见 runbook §7.3）。
    """
    recall_rows = [row[0] for row in rows if row[0] == row[0]]  # nan != nan
    recall = mean(recall_rows)
    faithfulness = mean([row[1] for row in rows])
    relevance = mean([row[2] for row in rows])
    passed = {
        "recall": sum(1 for row in recall_rows if row >= thresholds.get("recall", 0.0)),
        "faithfulness": sum(1 for row in rows if row[1] >= thresholds.get("faithfulness", 0.0)),
        "answer_relevancy": sum(1 for row in rows if row[2] >= thresholds.get("answer_relevancy", 0.0)),
    }
    return Aggregate(
        name=name,
        count=len(rows),
        recall=recall,
        faithfulness=faithfulness,
        answer_relevancy=relevance,
        passed=passed,
        extra={"thresholds": dict(thresholds), "recall_applicable": len(recall_rows)},
    )


def passed_overall(agg: Aggregate, *, thresholds: dict[str, float], min_count: int = 1) -> bool:
    """整体是否达标（CI 的阻断判据）。

    ⚠️ `count < min_count` **判为不通过**（默认 min_count=1）：一份「一条都没跑」的报告
    不能算通过 —— 那是 CI 里最容易发生的假绿（数据集路径写错、`--only` 挑空）。
    """
    if agg.count < max(1, int(min_count)):
        return False
    return (
        agg.recall >= thresholds.get("recall", 0.0)
        and agg.faithfulness >= thresholds.get("faithfulness", 0.0)
        and agg.answer_relevancy >= thresholds.get("answer_relevancy", 0.0)
    )


#: 表示「这条用例不适用于 Recall」的哨兵值（见 `aggregate` 的说明）。
#: 用 `float("nan")` 而不是 `None`：它进 `mean` 之前会被过滤掉，
#: 而类型上仍然是 `float`（调用方不必到处写 `if value is None`）。
RECALL_NA: Final[float] = float("nan")


def recall_for(case: EvalCase, quality: RetrievalQuality) -> float:
    """这条用例在 Recall 这一维上的取值（不适用的那类返回 `RECALL_NA`）。

    两类用例不适用：

    * `expect_empty=True`（知识库里确实没有答案 / 走的是 data 分支）——**没有期望来源**，
      Recall 的分母不存在；
    * `expected_sources` 为空的用例（同上，只是没显式写 `expect_empty`）。

    ⚠️ 它们**不是 0 分**，而是「不参与」。这条区分是本仓实测补上的（runbook §7.3）。
    """
    if case.expect_empty or not case.expected_sources:
        return RECALL_NA
    return quality.recall


__all__ = [
    "FACT_WEIGHT",
    "METRIC_NAMES",
    "ON_TOPIC_WEIGHT",
    "QUERY_TERM_WEIGHT",
    "RECALL_NA",
    "RELEVANCE_FACT_WEIGHT",
    "SHORT_ANSWER_CHARS",
    "Aggregate",
    "AnswerQuality",
    "RetrievalQuality",
    "aggregate",
    "answer_quality",
    "mean",
    "passed_overall",
    "recall_for",
    "retrieval_quality",
]
