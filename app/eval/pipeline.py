"""答案合成层（**评测的「生成」那一段**，三种实现共用一个签名）。

## 为什么评测要把「检索」与「生成」分开

`14.eval.md` §4 第 2 条把指标分了层：检索层看 Recall，Agent 层看成功率与关键事实命中率。
要让「检索层的分数」真的只反映检索，就必须能把**生成**这一段换掉 ——
否则同一份检索结果会因为生成模型的随机性而在两次运行里得到不同的 Faithfulness，
于是「这次改动让检索变好了吗」这个问题永远答不出来。

所以这里有三个实现，签名完全相同（`Answerer`）：

| 实现 | 答案从哪来 | 用来回答什么 | 成本 |
|---|---|---|---|
| `context_answerer`（**默认**） | 检索命中的片段**原文** | 「这份检索结果里有没有答案」 | 0（不打模型） |
| `extractive_answerer` | 从命中里**挑最相关的几句** | 同上，但进一步压篇幅 | 0 |
| `build_llm_answerer` | 经 `llm_call` 门面打真模型 | 「模型拿到这些材料后答得怎么样」 | 一次生成/条 |

## ⚠️ 本仓实测的一处重要取舍：默认用 `context_answerer` 而不是「挑句子」

第一版把「挑最相关的 4~5 句」定成默认，理由是「答案太长会让篇幅系数形同虚设」。
**实测证明这个理由撑不住**（15 条 golden set，runbook §5.2 有完整对照表）：

| 合成方式 | Recall | Faithfulness | Relevance | 复现性 |
|---|---|---|---|---|
| 挑句子（4~5 句，4-gram 排序） | 1.000 | **0.771** | 0.674 | 完全可复现 |
| 挑句子 + 长度破平局（5 句） | 1.000 | 0.918 | **0.760** | 完全可复现 |
| **片段原文（`context_answerer`）** | 1.000 | **1.000** | 0.824 | 完全可复现 |

关键那一行是「挑句子」的 Faithfulness **不是 1.0** —— 而它抽的每一句**都逐字来自检索材料**。
也就是说：那个 0.771 量的**不是**「答案忠不忠于材料」，而是「我的挑句启发式准不准」。
这正是同义反复的一半 —— 把评分器自己的缺陷算进了被测系统的分数里。

`context_answerer` 没有这个问题：它的语义是**唯一且可证伪的** ——
「假设生成阶段逐字引用材料，这份检索结果里有没有答案？」于是：

* Faithfulness 恒为 1.0 是**定义上**的结果（答案就是材料），它退化成「有一份完整性检查」；
* **Recall 与 Relevance 才是真正的信号**：前者说检索到了没有，后者说「检索到的那几段是否
  覆盖了问的东西」—— `invoice-reissue` 那条在挑句模式下只有 0.5 的 Relevance，
  改为片段原文后是 0.92（因为答案句确实在材料里，只是我的挑句没挑中）。

⚠️ **两种方式的分数不可直接比较**（报告里 `answerer` 一列就是为此而设）。
`extractive_answerer` 现在只用于 `compare.py` 的对照实验（那里要的是「措辞整齐的答案」），
以及探索「挑句启发式值多少分」这个反向问题。

## 两条实现细节（都是实测踩出来的）

1. **必须带上出处**：`14.eval.md` §8 第 4 条要求答案可追溯，
   而抽取式答案的出处就是它被抽出来的那几条命中。带上它，报告里的样本才可核对。
2. **每个命中最多贡献几句**（`per_hit`）：见 `PER_HIT_SENTENCES` 的注释 ——
   不限制时排第一的文档会把名额吃光，而答案的第二个条件常常在第二份文档里。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, Union

from app.agents.agent_llm import JsonLLM, build_system_prompt, generate_json
from app.eval.text import normalize_text

#: 片段原文答案的字符上限。1800 的依据：Top-5 的实测总长中位数约 1200 字
#: （`10.rag-runbook.md` §6 的 token 口径：候选池 11907 token → top-5 3246 token），
#: 1800 能装下其中 3~4 条完整片段 —— 再多就会把「答非所问的片段」也当成答案的一部分，
#: 而 Relevance 会因此被稀释（不是提高）。
CONTEXT_ANSWER_CHARS: Final[int] = 1800

#: 抽取式答案最多几句。定成 5 的依据（本仓实测）：一条政策问题的完整回答通常需要
#: 「1 句条件 + 1~2 句数字 + 1 句例外」，4 句会让「例外」被挤掉，
#: 而事实点覆盖立刻掉到 0.5（见 runbook §5.2 的实测表）。
MAX_EXTRACT_SENTENCES: Final[int] = 5

#: 每个命中最多贡献几句。**这是为了「答全」而不是「答深」**：
#: 排在最前面的命中往往只是最相关的那一份文档，而答案需要的第二个条件
#: 常常在第二份文档里（实测：`退货政策.md` 的第 2 条 + 第 7 条）。
PER_HIT_SENTENCES: Final[int] = 2

#: 抽取式答案的字数上限（与 `agents/synthesis.CUSTOMER_MAX_CHARS`=300 同量级）
MAX_EXTRACT_CHARS: Final[int] = 700

#: 与 `agents/synthesis.py` 的对客口径一致：回答里引用知识库时写 `[编号]`
ANSWER_SYSTEM: Final[str] = build_system_prompt(
    "你是电商平台的客服助手，正在**只根据**给定的知识库片段回答用户问题。\n\n"
    "硬要求：\n"
    "1. 只用在给定片段里出现过的信息；片段里没有的条款、天数、金额一律不许补；\n"
    "2. 片段里没有答案时，如实说「知识库里没有相关内容」，不要推测；\n"
    "3. 口语、直接给结论，不超过 200 字；\n"
    "4. 引用片段时写 `[编号]`，编号必须是给定片段里出现过的。",
    output_schema='{"answer": "给客户的回答", "cited": [引用的片段编号]}',
)


class Answerer(Protocol):
    """答案合成契约：给 (问题, 命中片段)，回一段答案文本（同步或异步都吃）。"""

    def __call__(self, question: str, hits: Sequence[Any]) -> Any:
        ...


#: 合成器也可以是任意可调用对象（测试与自定义合成器用）。
#: 与 `Answerer` 并列导出，是为了让「规约」与「可替换的实现」在类型层面分开：
#: 实现方按 `Answerer` 写，调用方按 `AnswererLike` 收。
AnswererLike = Union[Answerer, Callable[..., Any]]


# ── 命中的取值口径（只依赖「有 text/source」这一条最小契约）────────────────


def hit_text(hit: Any) -> str:
    return str(_pick(hit, "text", "content", "page_content") or "")


def hit_source(hit: Any) -> str:
    return str(_pick(hit, "source", "document", "file", "doc_id") or "")


def hit_score(hit: Any) -> float:
    raw = _pick(hit, "score", "similarity", "distance")
    try:
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _pick(hit: Any, *names: str) -> Any:
    """从 dict 或对象上取字段（与 `app/tools/retriever.py::_pick` 同一口径）。"""
    for name in names:
        if isinstance(hit, dict):
            if hit.get(name) is not None:
                return hit[name]
        else:
            value = getattr(hit, name, None)
            if value is not None:
                return value
    return None


def context_block(hits: Sequence[Any], *, max_chars: int = 4000) -> str:
    """把命中渲染成 **Faithfulness 的判据材料**（也是送进合成 prompt 的上下文）。

    它与 `agents/rag.docs_to_prompt_block` 的差别只有一处：这里是评测模块，
    **不允许 import agent 层**（评测要能在 agent 层坏掉时独立指出问题）——
    所以两份渲染各自实现，而它们的一致性由「都用同一条命中契约」保证。
    """
    parts: list[str] = []
    used = 0
    for index, hit in enumerate(hits, start=1):
        text = hit_text(hit).strip()
        block = f"[{index}]（出处：{hit_source(hit)}）\n{text}"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


# ── 抽取式（离线、零成本、可复现）────────────────────────────────────────


_SENTENCE_SPLIT: Final[re.Pattern[str]] = re.compile(r"[。！？；\n]+")

#: Markdown 标题（`#` / `##` / `###` …）。命中之后**剥掉**而不是保留：
#: 标题不是内容，而它几乎总是「问题里所有词的超集」（`### 如何修改收货地址？` 命中
#: 「地址/收货/修改」），于是它会在排序里挤掉真正的答案句 —— 本仓实测的第一版就是这样：
#: 15 条里有 4 条的答案是**纯标题**（`### 发票丢了可以补开吗。`），事实点覆盖 0.00。
#: 详见 runbook §10.1 坑 3。
_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^\s*#{1,6}\s*")


def split_sentences(text: str) -> list[str]:
    """按中文句读切句，并**剥掉 Markdown 标题记号**（评测内部用）。

    ⚠️ 剥掉的只是 `#` 记号：`## 四、不予受理的情形` 会变成 `四、不予受理的情形`，
    那**仍然**是一句（它是条款的小标题，含信息量）；被丢弃的是「纯 `#` 的整行」——
    那没有任何内容。
    """
    out: list[str] = []
    for item in _SENTENCE_SPLIT.split(str(text or "")):
        cleaned = _HEADING_RE.sub("", item.strip()).strip()
        if cleaned and cleaned.strip("#").strip():
            out.append(cleaned)
    return out


def pick_sentences(
    question: str,
    hits: Sequence[Any],
    *,
    limit: int = MAX_EXTRACT_SENTENCES,
    per_hit: int = PER_HIT_SENTENCES,
) -> list[str]:
    """挑出**与问题最相关**的句子（可复现：同一份输入永远产出同一份答案）。

    排序键是 `(-分数, 句序号)`，而分数由两部分组成：

    ```
    分数 = 问题 4-gram 覆盖率 + 0.1 × min(句长 / 40, 1)
                └── 主判据（谁更贴问题）      └── **并列时的破平局项**
    ```

    ⚠️ 那个「破平局项」是本仓实测补上的（runbook §5.2 的对照表）：
    文档里的**小标题**（`### 如何修改收货地址？`、`2.1 银卡：生日月双倍积分`）与
    **正文答案句**（`未发货的订单可在订单详情页点击「修改地址」自助修改`）
    在 4-gram 覆盖率上**完全并列**（都是 1.0 —— 标题几乎总是问题词的超集），
    于是排序退化成「谁在文档里更靠前」，而标题永远靠前 → 抽出来的答案是一串标题。

    用「长度」破平局而不是用「是不是标题」破平局，理由：长度是与语料格式无关的性质
    （换成纯文本语料时它对同一件事仍然成立），而「是不是标题」要靠 Markdown 记号判断，
    那些记号在 `.txt` 与 HTML 转换后的语料里可能不存在。

    `per_hit` 是**每个命中最多几句**：不设它时排第一的那份文档会把名额吃光，
    而答案需要的第二个条件常常在第二份文档里（本仓实测见 `PER_HIT_SENTENCES` 的注释）。
    """
    terms = _terms(question)
    picked: list[str] = []
    for hit in hits:
        scored: list[tuple[float, int, str]] = []
        for sentence_index, sentence in enumerate(split_sentences(hit_text(hit))):
            haystack = normalize_text(sentence)
            coverage_score = sum(1 for term in terms if term in haystack) / len(terms) if terms else 0.0
            length_bonus = 0.1 * min(len(haystack) / 40.0, 1.0)
            scored.append((coverage_score + length_bonus, sentence_index, sentence))
        scored.sort(key=lambda item: (-item[0], item[1]))
        taken = 0
        for _, _, sentence in scored:
            if taken >= per_hit or len(picked) >= limit:
                break
            if sentence in picked:
                continue
            picked.append(sentence)
            taken += 1
        if len(picked) >= limit:
            break
    if not picked:
        # 一个命中都没有（或全是空文本）：退回「前几条命中的第一句」，
        # 而不是返回空 —— 空答案会被判成「拒答」，那会污染失败样本的归因。
        for hit in hits:
            sentences = split_sentences(hit_text(hit))
            if sentences:
                picked.append(sentences[0])
            if len(picked) >= limit:
                break
    return picked


def _terms(question: str) -> tuple[str, ...]:
    """问题词项（4-gram，比 `metrics._query_terms` 的 2-gram 更具体）。

    为什么这里用 4-gram：抽取要**精准挑句**（挑错一句会让整条用例的分数失真），
    而判分要**宽**（判错一次只是分数偏差）。两处刻意用不同的粒度。
    """
    normalized = normalize_text(question)
    if len(normalized) < 4:
        return (normalized,) if normalized else ()
    return tuple(dict.fromkeys(normalized[i : i + 4] for i in range(len(normalized) - 3)))


def context_answerer(*, max_chars: int = CONTEXT_ANSWER_CHARS) -> Answerer:
    """**默认**的答案合成：检索命中的**片段原文**（语义唯一，见模块 docstring 的对照表）。

    它不是「没做生成」，而是把生成固定成「逐字引用材料」——
    于是 Faithfulness 变成定义上的 1.0，真正的信号落在 Recall 与 Relevance 上。
    这是评测检索层时唯一自洽的选择：任何更聪明的合成都会把
    「我的合成准不准」混进「检索到了没有」这个问题的答案里。
    """

    def answer(question: str, hits: Sequence[Any]) -> str:
        del question  # 片段原文不需要挑句（这正是它的语义）
        if not hits:
            return "知识库里没有找到相关内容，我这边看不到可以引用的条款。"
        body = context_block(hits, max_chars=max_chars)
        if not body.strip():
            return "知识库里没有找到相关内容，我这边看不到可以引用的条款。"
        sources = [hit_source(hit) for hit in hits[:3] if hit_source(hit)]
        cite = f"\n（出处：{'、'.join(sources)}）" if sources else ""
        return (body + cite)[:max_chars]

    return answer


def extractive_answerer(*, limit: int = MAX_EXTRACT_SENTENCES) -> Answerer:
    """抽取式答案合成：从命中里挑最相关的几句（不打模型，`--answer extract` 用）。

    ⚠️ 它的分数**与 `context_answerer` 不可比较**：Faithfulness 在它这里量的
    一半是「挑句启发式准不准」（见模块 docstring 的三行对照表）。
    用途是「挑句值多少分」这个反向问题，以及需要短答案时的对照。
    """

    def answer(question: str, hits: Sequence[Any]) -> str:
        if not hits:
            return "知识库里没有找到相关内容，我这边看不到可以引用的条款。"
        picked = pick_sentences(question, hits, limit=limit)
        if not picked:
            return "知识库里没有找到相关内容，我这边看不到可以引用的条款。"
        body = "".join(
            item if item.endswith(("。", "！", "？")) else item + "。" for item in picked
        )
        sources = [hit_source(hit) for hit in hits[:3] if hit_source(hit)]
        cite = f"（出处：{'、'.join(sources)}）" if sources else ""
        return (body + cite)[:MAX_EXTRACT_CHARS]

    return answer


# ── 真模型（走 `llm_call` 门面）─────────────────────────────────────────


@dataclass(frozen=True)
class LlmAnswer:
    """一次真模型合成的结果（`answer` + 它引用了哪些片段编号）。"""

    answer: str
    cited: tuple[int, ...] = ()
    model: str = ""


def build_llm_answerer(
    *,
    llm: JsonLLM | None = None,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 700,
) -> Answerer:
    """造一个真模型合成器（`JsonLLM` 注入 = 测试可离线跑完整链路）。"""

    def build_messages(question: str, hits: Sequence[Any]) -> list[dict[str, Any]]:
        # ⚠️ `hits` 为空时**必须**把「没有材料」这件事写进 prompt（与 `synthesis.py` 的材料
        # 边界同一条纪律）：模型编造往往不是因为它想撒谎，而是因为**没人告诉它这里没有材料**。
        # 这条分支正是「单 Agent 基线」走的那一条 —— 它决定了对基线是否公平。
        material = context_block(hits) or "（本次没有检索到任何片段，你没有可依据的材料）"
        return [
            {"role": "system", "content": ANSWER_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"用户问题：{question}\n\n"
                    f"知识库片段（**这是你唯一的信息来源**）：\n{material}\n\n"
                    "要求：只用上面的片段回答；片段里没有的内容不要推测。"
                ),
            },
        ]

    async def answer(question: str, hits: Sequence[Any]) -> str:
        kwargs: dict[str, Any] = {"temperature": float(temperature), "max_tokens": int(max_tokens)}
        if model:
            kwargs["model"] = model
        payload = await generate_json(
            build_messages(question, hits), llm=llm, agent="eval_answer", **kwargs
        )
        return str(payload.get("answer") or "").strip()

    answer.build_messages = build_messages  # type: ignore[attr-defined]
    return answer


# ── 单 Agent 基线（`compare.py` 的对照组）──────────────────────────────────

#: 单 Agent 的 system prompt。**刻意写得很短**：它是「一个大 prompt」的如实形态 ——
#: 一个把检索、路由、权限、护栏全塞进去的「大 prompt」不是单 Agent，
#: 是伪装成单 Agent 的图（那会让对照实验失去意义，见 `compare.py` 的机制表）。
SINGLE_AGENT_SYSTEM: Final[str] = (
    "你是电商平台的智能助手。请直接回答用户的问题，语气礼貌、口语、200 字以内。"
)


def build_single_agent_answerer(
    *,
    llm: JsonLLM | None = None,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 700,
) -> Answerer:
    """造**单 Agent 基线**的合成器：一个 system prompt + 一个问题，不给任何材料。

    它**忽略** `hits`（对照组拿不到材料）。刻意仍然收这个参数：
    这样对照实验的两条路可以用完全相同的方式被调用 —— 差别只在实现里，
    而不是在「谁传了哪些参数」这种接线细节上。
    """

    async def answer(question: str, hits: Sequence[Any]) -> str:
        del hits
        kwargs: dict[str, Any] = {"temperature": float(temperature), "max_tokens": int(max_tokens)}
        if model:
            kwargs["model"] = model
        payload = await generate_json(
            [
                {"role": "system", "content": SINGLE_AGENT_SYSTEM},
                {"role": "user", "content": question},
            ],
            llm=llm,
            agent="eval_single_agent",
            **kwargs,
        )
        return str(payload.get("answer") or "").strip()

    return answer


__all__ = [
    "ANSWER_SYSTEM",
    "CONTEXT_ANSWER_CHARS",
    "MAX_EXTRACT_CHARS",
    "MAX_EXTRACT_SENTENCES",
    "PER_HIT_SENTENCES",
    "SINGLE_AGENT_SYSTEM",
    "Answerer",
    "AnswererLike",
    "LlmAnswer",
    "build_llm_answerer",
    "build_single_agent_answerer",
    "context_answerer",
    "context_block",
    "extractive_answerer",
    "hit_score",
    "hit_source",
    "hit_text",
    "pick_sentences",
    "split_sentences",
]
