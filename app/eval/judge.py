"""LLM 判官（`14.eval.md` §4 第 1 条「规则 + LLM 双重判分」的那一半）。

## 为什么评测需要第二个判分器（规则那一路已经能算分了）

`app/eval/metrics.py` 的规则判据有一个天生的盲区：**它只认字面**。
标准答案写「由买家承担运费」，模型答「运费需要您自己付」——规则判 0 分，
而人看是对的。这一格在真实评测里占的比例不小（本仓实测约 5%~10%），
所以只靠规则判分会让 Faithfulness 系统性偏低，掉分时你会先去怀疑检索。

于是判分分两层，各自有明确边界（`14.eval.md` §5 第二条的原话是「双重判分，更可靠」）：

| 层 | 谁判 | 强项 | 弱项 | 何时用 |
|---|---|---|---|---|
| 规则（`metrics.py`） | 本仓代码 | **完全可复现**、零成本、可逐条追责 | 同义改写判不出 | 默认（CI 闸门） |
| LLM（本模块） | 真模型 | 能判同义与蕴含 | 有随机性、有成本、需要密钥 | 人工对照 / 基线复算 |

## 本模块的三条纪律

1. **判官只输出判定与理由，不改分数公式**。它返回 `Verdict(faithful, relevant, reason)`，
   由 `rag_eval.py` 决定怎么与规则判据合并 —— 若判官直接给一个数字，
   两套口径就会分叉（而分叉的判分器比没有判分器更糟：没人知道数字是谁给的）。
2. **判分缓存永不进系统缓存**（`app/cache`）。理由：判分结果的正确性取决于
   「判官模型 + prompt 版本 + 被判答案」三者，而语义缓存的键只有问题向量。
   把判分结果塞进语义缓存，会在换判官模型之后继续返回旧判定 ——
   症状是「换了模型分数一点没变」，看起来像评测失灵。需要缓存时用**文件级**缓存
   （`--out-dir` 的 JSON 就是它），那是显式的、人看得见的。
3. **`strict` 后端是一个「诚实的假判官」**：它按真实判官的口径（标准答案重叠 +
   拒答识别）判分，而不是按「哪边是多 Agent」判分。它存在的唯一目的是
   **让离线端到端演示可跑**（没有密钥也能看完整链路），而不是让测试通过 ——
   所以它判错的形态与真判官一致（同义改写它也会判错）。见 `strict_llm_judge` 的说明。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, Union

from app.agents.agent_llm import JsonLLM, build_system_prompt, generate_json
from app.eval.text import is_emptyish, is_hedged, normalize_text

logger = logging.getLogger(__name__)

#: 判官看到的标准答案与候选答案的长度上限（防止把整篇文档塞进判分 prompt）
MAX_REFERENCE_CHARS: Final[int] = 1200
MAX_ANSWER_CHARS: Final[int] = 2000

JUDGE_SYSTEM: Final[str] = build_system_prompt(
    "你是**严格的评测判官**，只做两件判定，不打分、不写建议、不重写答案。\n\n"
    "判定 1「忠于材料」：候选答案里的**每个事实**是否都能在给定材料里找到依据。"
    "材料里没有的数字、期限、比例、条件，一律算不忠于材料（即使它听起来合理）。"
    "候选答案与标准答案**措辞不同但意思相同**时，算忠于材料。\n\n"
    "判定 2「切题」：候选答案是否在回答**用户问的那个问题**（而不是相近的另一个问题）。"
    "只答一半、或只说「我不确定/请联系客服」而没有给出问题要的信息，算不切题。\n\n"
    "判定要点：\n"
    "1. 材料为空时，任何具体事实都算不忠于材料（但「我这边看不到」这类如实说明不算编造）；\n"
    "2. 不要因为候选答案比标准答案长或更啰嗦就扣分；\n"
    "3. 拿不准时**从严**（评测的用途是卡住变差，宁可让人来看一眼，不要放过）。",
    output_schema='{"faithful": true|false, "relevant": true|false, "reason": "不超过 40 字"}',
)


@dataclass(frozen=True)
class Judgment:
    """一次判分的结论（真值从 `app/eval/text.py` 的判据派生，理由给人看）。"""

    faithful: bool
    relevant: bool
    reason: str = ""
    #: 判官身份（进报告：`rules` / `strict` / `llm:<模型名>`）。
    #: ⚠️ 必须记进报告 —— 「同一份答案在两个判官下分数不同」是评测最常见的争议，
    #: 而没有这一列时你甚至不知道两次跑的是不是同一个判官。
    judge: str = "rules"
    #: 真模型判官是否意外失败（缺密钥 / 超时 / 输出不可解析）
    degraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "faithful": self.faithful,
            "relevant": self.relevant,
            "reason": self.reason,
            "judge": self.judge,
            "degraded": self.degraded,
        }


class Judge(Protocol):
    """判官契约：给 (问题, 标准答案, 材料, 候选答案, 期望事实点)，回一个 `Judgment`。"""

    def __call__(
        self,
        *,
        question: str,
        reference: str,
        context: str,
        answer: str,
        key_facts: tuple[str, ...] = (),
    ) -> Awaitable[Judgment] | Judgment:
        ...


def _truncate(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + "…（已截断）"


def build_judge_messages(
    *,
    question: str,
    reference: str,
    context: str,
    answer: str,
    key_facts: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """拼判官的 prompt（**纯函数**：判分口径能被单独断言，不必真跑模型）。"""
    facts = "、".join(key_facts) if key_facts else "（没有标注事实点）"
    return [
        {"role": "system", "content": JUDGE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"用户问题：{question}\n\n"
                f"标准答案（参考，不是唯一正确写法）：\n{_truncate(reference, MAX_REFERENCE_CHARS)}\n\n"
                f"必须命中的事实点：{facts}\n\n"
                f"系统实际检索到的材料（判定 1 的唯一依据）：\n"
                f"{_truncate(context, MAX_ANSWER_CHARS) or '（本次没有检索到任何材料）'}\n\n"
                f"候选答案（要判的那一份）：\n{_truncate(answer, MAX_ANSWER_CHARS) or '（空）'}"
            ),
        },
    ]


def parse_judgment(payload: Mapping[str, Any], *, judge: str) -> Judgment:
    """把模型给的 JSON 解析成 `Judgment`（**宽松解析、严格默认**）。

    两个键缺失或类型不对时一律判 False（从严）：判分器的失败方向必须是
    「让可疑的答案不通过」，反过来会让闸门形同虚设。
    """
    faithful = payload.get("faithful")
    relevant = payload.get("relevant")
    reason = str(payload.get("reason") or "").strip()[:200]
    return Judgment(
        faithful=bool(faithful) if isinstance(faithful, (bool, int)) else False,
        relevant=bool(relevant) if isinstance(relevant, (bool, int)) else False,
        reason=reason,
        judge=judge,
    )


def build_llm_judge(
    *,
    llm: JsonLLM | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    answer_chars: int = MAX_ANSWER_CHARS,
) -> Judge:
    """造一个**真模型**判官（经 `app.agents.agent_llm.generate_json`，也就是走 `llm_call` 门面）。

    为什么经 `generate_json` 而不是自己调 litellm：全项目只允许
    `app/llm/client.py` 出现 `litellm.*`（`5.llm.md` §1 的硬约束），
    评测也不能例外 —— 绕过去的调用不经过重试/降级/埋点，而评测正是要用那些数据的地方。

    `model` 传 None 时用配置里的主模型。判分**永远**传 `temperature=0.0`（默认值）：
    评测的用途是比较两次运行，判分的随机性会直接变成噪声（见模块 docstring 的纪律 2）。
    """

    async def judge(
        *,
        question: str,
        reference: str,
        context: str,
        answer: str,
        key_facts: tuple[str, ...] = (),
    ) -> Judgment:
        messages = build_judge_messages(
            question=question,
            reference=reference,
            context=context,
            answer=answer,
            key_facts=key_facts,
        )
        kwargs: dict[str, Any] = {"temperature": float(temperature), "max_tokens": 400}
        if model:
            kwargs["model"] = model
        label = f"llm:{model or 'primary'}"
        try:
            payload = await generate_json(messages, llm=llm, agent="eval_judge", **kwargs)
        except Exception as exc:  # noqa: BLE001 - 判官失败不该让整轮评测崩
            # **失败口径（重要的取舍）**：判官不可用时**不静默当通过**，而是记为
            # `degraded=True` 且两项都判 False。理由：评测的职责是「发现变差」，
            # 判官挂掉时如果放行，CI 会在最需要它的时候变成绿的（假绿比假红危险得多）。
            # `degraded` 进报告，读报告的人立刻知道分数为什么塌了。
            logger.warning("LLM 判官不可用，本条按「未通过 + degraded」记录：%s: %s", type(exc).__name__, exc)
            return Judgment(
                faithful=False,
                relevant=False,
                reason=f"判官不可用（{type(exc).__name__}）",
                judge=label,
                degraded=True,
            )
        return parse_judgment(payload, judge=label)

    return judge


def strict_llm_judge() -> Judge:
    """**诚实的假判官**：按真实判官的口径（标准答案重叠 + 拒答识别）判分，零成本、可复现。

    它与 `judge: rules` 的区别不是「谁更松」，而是**判据的来源不同**：

    * `rules` 判的是「`key_facts` 有没有逐字命中」（`metrics.py` 的判据）；
    * `strict` 判的是「候选答案与标准答案有没有**足够的实质重叠**」——
      它不知道事实点是什么，只看两段文本，所以「同义改写」它同样判不出来
      （与真判官的一致失败模式，见模块 docstring 的纪律 3）。

    它**用在哪**：`--judge strict` 的离线端到端演示，以及探索「标准答案重叠」
    这条更宽松的判据会把分数抬多高（runbook §3.6 的对照表）。

    ⚠️ 它**刻意不读**「这份答案来自单 Agent 还是多 Agent」：
    一个知道实验分组的判官不是判官，那是把结论写进判据里。
    真实的多 Agent 增益必须来自「多 Agent 拿到了检索材料、单 Agent 没有」这个机制差异
    （见 `compare.py` 的设计），而不是判官偏心。
    """

    def judge(
        *,
        question: str,
        reference: str,
        context: str,
        answer: str,
        key_facts: tuple[str, ...] = (),
    ) -> Judgment:
        del question, key_facts  # 它只看「答案 vs 标准答案 vs 材料」
        normalized_answer = normalize_text(answer)
        normalized_reference = normalize_text(reference)
        normalized_context = normalize_text(context)

        if is_emptyish(answer):
            return Judgment(False, False, "空答案", judge="strict")
        if is_hedged(answer) and normalize_text(answer) not in normalized_reference:
            return Judgment(False, False, "拒答/推脱，没有给出问题要的信息", judge="strict")

        # 判据 1「忠于材料」：答案的实质内容必须能在材料或标准答案里找到依据。
        # 用 3-gram 覆盖率而不是整句包含 —— 整句包含在中文里几乎不可能成立
        # （语序与标点一变就散），而 3-gram 是「这段文字有来源」的合理近似。
        support = normalized_context + normalized_reference
        answer_grams = _ngrams(normalized_answer, 3)
        supported = sum(1 for gram in answer_grams if gram in support)
        support_ratio = supported / len(answer_grams) if answer_grams else 0.0

        # 判据 2「切题」：答案与标准答案的实质重叠（同义改写这一条同样会判低，
        # 与真判官一致 —— 这是刻意的，见 docstring）。
        reference_grams = _ngrams(normalized_reference, 3)
        overlap = (
            sum(1 for gram in reference_grams if gram in normalized_answer) / len(reference_grams)
            if reference_grams
            else 0.0
        )
        return Judgment(
            faithful=support_ratio >= 0.5,
            relevant=overlap >= 0.25,
            reason=f"材料支撑度 {support_ratio:.2f}，与标准答案重叠 {overlap:.2f}",
            judge="strict",
        )

    return judge


def _ngrams(text: str, size: int) -> set[str]:
    if len(text) < size:
        return {text} if text else set()
    return {text[i : i + size] for i in range(len(text) - size + 1)}


def rules_judge() -> Judge:
    """规则判官：**不调模型**，只看 `key_facts` 是否命中（`metrics.py` 的同一套判据）。

    它存在的意义是「把判官接口留出规则实现」，让 `rag_eval.py` 只有一条代码路径
    （判分与聚合都不必区分「有没有判官」）。真正的分数仍由 `metrics.py` 算 ——
    本判官只回答「按规则，这条算不算通过」。
    """

    def judge(
        *,
        question: str,
        reference: str,
        context: str,
        answer: str,
        key_facts: tuple[str, ...] = (),
    ) -> Judgment:
        del question, reference, context
        from app.eval.text import coverage

        if is_emptyish(answer):
            return Judgment(False, False, "空答案", judge="rules")
        ratio = coverage(answer, key_facts)
        hedged = is_hedged(answer)
        return Judgment(
            faithful=ratio >= 1.0 and not hedged,
            relevant=ratio >= 1.0 and not hedged,
            reason=f"事实点命中率 {ratio:.2f}" + ("；拒答" if hedged else ""),
            judge="rules",
        )

    return judge


def build_judge(mode: str, **kwargs: Any) -> Judge:
    """按模式造判官（CLI 的 `--judge` 映射到这里）。"""
    normalized = str(mode or "rules").strip().lower()
    if normalized == "rules":
        return rules_judge()
    if normalized == "strict":
        return strict_llm_judge()
    if normalized == "llm":
        return build_llm_judge(**kwargs)
    raise ValueError(f"未知的判官模式 {mode!r}（可选：rules / strict / llm）")


#: 判官也可以是一个「回 JSON 文本」的可调用对象（测试与自定义判官用）
JudgeLike = Union[Judge, Callable[..., Any]]


def verdict_json(judgment: Judgment) -> str:
    """判官的 JSON 形态（写进报告的 `judge` 字段，便于外部工具消费）。"""
    return json.dumps(judgment.as_dict(), ensure_ascii=False)


__all__ = [
    "JUDGE_SYSTEM",
    "MAX_ANSWER_CHARS",
    "MAX_REFERENCE_CHARS",
    "Judge",
    "JudgeLike",
    "Judgment",
    "build_judge",
    "build_judge_messages",
    "build_llm_judge",
    "parse_judgment",
    "rules_judge",
    "strict_llm_judge",
    "verdict_json",
]
