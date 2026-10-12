"""评测的**文本归一与判据原语**（本模块只有纯函数，不 import 任何项目内模块）。

## 为什么「判事实点」必须先归一

`14.eval.md` §5 第二条要的是「规则 + LLM 双重判分」。规则那一路的唯一要求是
**可复现**：同一份答案在任何机器上都必须判出同一个分。而中文语料里，
「同一件事」的写法有大量等价形态，逐字 `in` 会判错：

| 标准答案里写的 | 模型实际输出的 | 逐字匹配 | 归一后匹配 |
|---|---|---|---|
| `7 天` | `7天` / `七天` | ❌（中间空格、数字写法） | ✅ |
| `24 小时` | `二十四小时` | ❌ | ✅ |
| `99 元` | `￥99`（全角/货币符号） | ❌ | ✅（标点与全角被折叠） |
| `支付宝、微信` | `支付宝，微信` | ❌ | ✅（全角逗号折叠） |

归一**只做**这几件事（其余一律不动，避免「归一出错反而把不相关的也算命中」）：
全角→半角、去掉所有空白与常见标点、ASCII 小写、全角数字→半角数字。

> ⚠️ 有一条**刻意不做**：不做同义词替换（如把「运费」当成「邮费」）。
> 那会让判据从「可复现的规则」变成「一份小型词典」，而词典的维护成本
> 会随着语料增长而失控 —— 同义与蕴含的判定交给 `app/eval/judge.py` 的 LLM 那一路。
> 两条路各有明确边界，才不会出现「两边都做一半」的灰区。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

#: 判定「这个答案其实是在拒答/推脱」的关键词（`app/agents/guardrail.py` 的落地话术、
#: `nodes.FALLBACK_MESSAGE`、以及数据节点「查不到」的常见措辞都在这里）。
#:
#: ⚠️ 这份表**只用于判分**，绝不用来改行为：判分判错只影响一个数字，
#: 而拿它去拦回答就是另一回事了（那是 `agents/guardrail.py` 的活）。
HEDGE_PHRASES: Final[tuple[str, ...]] = (
    "抱歉",
    "无法处理",
    "无法回答",
    "我这边看不到",
    "没有找到",
    "知识库没有",
    "知识库里没有",
    "暂不可用",
    "暂时不可用",
    "请稍后重试",
    "请联系人工",
    "不知道",
    "不清楚",
    "不确定",
)

#: 常见标点（归一后一律删除）。用显式字符集而不是 `\W`：
#: `\W` 在 Python 里对中文的处理依赖 `re.UNICODE`，而「中文标点算不算 \w」
#: 这个问题在不同版本/不同库上不一致 —— 评测的判据不能建立在库的行为差异上。
_PUNCT: Final[str] = (
    " \t\r\n\u3000"
    "，。、；：？！“”‘’（）《》〈〉【】〔〕—…·～"
    ",.;:?!\"'()[]{}<>/\\|@#$%^&*_+=~`-"
    "％￥$€£"
)

_NUMBER_CHARS: Final[dict[str, str]] = {
    "零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}

#: `十` 开头的复合数（`十五`=15、`二十`=20、`二十四`=24）。覆盖到 99 ——
#: 业务语料里的中文数字全是天数/月数/金额量级，再往上是过度工程。
#: ⚠️ `十` **不能**放进上面的单字表（放进去会让「二十四」→「204」）：
#: 它是位权而不是数值，必须由一个专门的规则处理（本仓实测踩到，见 runbook §10.1 坑 7）。
_TENS_RE: Final[re.Pattern[str]] = re.compile(r"([0-9])?十([0-9])?")


def to_halfwidth(text: str) -> str:
    """全角 → 半角（NFKC 会把全角数字、全角字母、全角标点一起折叠）。"""
    return unicodedata.normalize("NFKC", text)


def _expand_tens(text: str) -> str:
    """`十五`→`15`、`二十`→`20`、`二十四`→`24`、`二十四小时`→`24小时`。

    ⚠️ 顺序是**先折单个汉字数字、再处理 `十`**（`normalize_text` 里的调用顺序）——
    否则 `四` 会先把 `二十四` 变成 `二十4`，而 `二十4` 在正则里只会匹配到「十4」那一截，
    结果是 `24` 变成 `十4`（本仓实测踩到，见 runbook §10.1 坑 7）。

    于是本函数实际只面对两种残留形态：`十x`（`十五`）与 `x十`（`二十`）
    —— 上面的单字折换已经把 `四` 吃掉，所以 `([0-9])?十([0-9])?` 的两个捕获组
    最多各有一个数字，逻辑因此是完备的。
    """

    def _sub(match: re.Match[str]) -> str:
        tens, ones = match.group(1), match.group(2)
        value = (int(tens) if tens else 1) * 10 + (int(ones) if ones else 0)
        return str(value)

    return _TENS_RE.sub(_sub, text)


def normalize_text(text: str) -> str:
    """判据用的归一文本（见模块 docstring 的表格）。空输入返回空串，绝不抛错。

    处理顺序固定为：全角→半角 → 小写 → 单个汉字数字 → `十` 复合数 → 去标点空白。
    每一步都有它必须在那里的理由：
      * 全长宽与小写在最前，否则 `ＡＢＣ` 与 `abc` 判不上；
      * 数字两步见 `_expand_tens` 的 ⚠️；
      * 去标点放最后，否则「99,000」里的逗号被去掉后两段会粘成一个我们不认识的数
        （这是可接受的：评判断言的是「数字串在不在材料里」，而材料两边都过同一个归一）。
    """
    if not text:
        return ""
    out = to_halfwidth(str(text)).lower()
    for char, digit in _NUMBER_CHARS.items():
        out = out.replace(char, digit)
    out = _expand_tens(out)
    for char in _PUNCT:
        out = out.replace(char, "")
    # 归一之后再折一次空白：`_PUNCT` 里已含空白，但这行兜住「上面替换产生的新空白」
    return "".join(out.split())


def contains(haystack: str, needle: str) -> bool:
    """归一后包含（**判分的主判据**，`normalize_text` 的两端都过一遍）。"""
    target = normalize_text(needle)
    if not target:
        return False
    return target in normalize_text(haystack)


def matching_key_facts(answer: str, key_facts: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """答案里命中了哪些事实点（返回命中的**原始写法**，保持报告可读）。"""
    return tuple(fact for fact in key_facts if contains(answer, fact))


def missing_key_facts(answer: str, key_facts: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """答案里**没**命中哪些事实点（报告里要逐条列出来，否则「为什么掉分」无从下手）。"""
    return tuple(fact for fact in key_facts if not contains(answer, fact))


def coverage(answer: str, key_facts: tuple[str, ...] | list[str]) -> float:
    """事实点命中率 ∈ [0, 1]。没有标注事实点时返回 1.0（＝这条不参与该维度判分）。

    为什么空集返回 1.0 而不是 0.0：`key_facts` 是**可选的补充判据**。
    返回 0 会让「没标注事实点的用例」被当成「全错」，而返回 1 是「这一维不加分不减分」
    —— 后者可以用「本次评测里有多少条带了事实点」如实交代（`AnswerQuality.facts_declared`）。
    """
    if not key_facts:
        return 1.0
    return len(matching_key_facts(answer, key_facts)) / len(key_facts)


#: 抽数字串用的正则。**刻意从 2 位起**（`[0-9]{2,}`）：
#: 一位数字在中文回答里遍地都是（`[1]` 这样的引用编号、`第 1 条`、`1 元`），
#: 而它们几乎总能从「材料里恰好也有一个 1」处蒙对，于是这条判据的假阳性远高于信息量。
#: 本仓实测：只算一位数时，一句干净的寒暄（`你好！我是客服助手`）会被判成
#: 「材料里没有的数字：['1'] ← 编造信号」——那是引用编号被当成了数字（见 runbook §10.1 坑 5）。
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]{2,}(?:\.[0-9]+)?")


def digit_tokens(text: str) -> frozenset[str]:
    """文本里的数字串集合（归一后，**至少两位**）。

    用于「答案里的数字是否凭空出现」的机械核对。`14.eval.md` §5 第一条关心的
    「Faithfulness ≠ 编造」在数字上最容易出问题（政策里的天数、比例、金额
    被改一个数就是一次对客事故），所以单独抽一个原语，
    由 `metrics.answer_quality` 拿它算 `unsupported_numbers`。
    """
    return frozenset(_NUMBER_RE.findall(normalize_text(text)))


def unsupported_numbers(answer: str, context: str) -> tuple[str, ...]:
    """答案里出现、但**检索上下文里没有**的数字（按出现顺序，去重）。

    这条判据是「Faithfulness 低」的机械信号里最值钱的一个：
    模型编造政策条款时，最典型的形态就是把「7 天」写成「15 天」（数字变了、话术没变），
    而纯文本相似度判不出来（一句话只差两个字）。
    代价是它对**合理的换算**会误报（上下文写「1 元」，答案写「10 积分」）——
    所以它只进报告，且**单独出现时不让分数失败**（见 `metrics.answer_quality`）。
    """
    known = digit_tokens(context)
    seen: list[str] = []
    for token in _NUMBER_RE.findall(normalize_text(answer)):
        if token in known or token in seen:
            continue
        seen.append(token)
    return tuple(seen)


def is_hedged(text: str) -> bool:
    """这段文本是否**主要在拒答/推脱**（见 `HEDGE_PHRASES`）。

    ## ⚠️ 为什么不能「命中一个词就算拒答」（本仓实测的第一版就是这么错的）

    对客回答里带一句**免责说明**是**正常且正确**的写法，例如：

    > 单笔实付满 99 元免运费[1]。……具体金额以结算页显示为准，**我这边看不到**您当前是什么等级。

    第一版把「命中任一 `HEDGE_PHRASES`」判成拒答，于是上面这段话被判成 hedged →
    Faithfulness 折半 → 那 5 条回答质量最好的用例反而成了「失败样本」。
    症状是「评测报告在惩罚正确的行为」，而它看起来像是模型的问题。

    所以判据收紧成两条**结构性**的条件（任一成立才算拒答）：

    ① **短回答**（< `HEDGE_SHORT_CHARS` 个归一字符）且含拒答措辞 ——
       「抱歉，这个请求我无法处理。」这类；
    ② **开头就是拒答**（前 `HEDGE_PREFIX_CHARS` 个字符里出现拒答措辞）且**总长很短**
       （< `HEDGE_PREFIX_MAX_CHARS`）—— 「我这边看不到，请联系人工客服。」这类。

    两条都带「短」这个条件，是因为**长回答里的免责说明不改变它已经答了问题这个事实**。
    """
    normalized = normalize_text(text)
    if not normalized:
        return False
    hits = [phrase for phrase in HEDGE_PHRASES if normalize_text(phrase) in normalized]
    if not hits:
        return False
    if len(normalized) < HEDGE_SHORT_CHARS:
        return True
    prefix = normalized[:HEDGE_PREFIX_CHARS]
    if len(normalized) < HEDGE_PREFIX_MAX_CHARS and any(
        normalize_text(phrase) in prefix for phrase in HEDGE_PHRASES
    ):
        return True
    return False


#: 判「短回答即拒答」的长度界线（归一字符数）
HEDGE_SHORT_CHARS: Final[int] = 40
#: 「开头就是拒答」的判定窗口
HEDGE_PREFIX_CHARS: Final[int] = 24
#: 「开头就是拒答」的长度上限（超过它就不算纯拒答 —— 它已经答了别的东西）
HEDGE_PREFIX_MAX_CHARS: Final[int] = 90


def is_emptyish(text: str) -> bool:
    """空、或只有标点/空白（「答了但什么都没说」）。"""
    return not normalize_text(text)


def tokenish_length(text: str) -> int:
    """归一后的字符数（篇幅判据用；中文按字算，不做分词）。"""
    return len(normalize_text(text))


@dataclass(frozen=True)
class Finding:
    """一条判据的结论（**判据名 + 是否通过 + 人话理由**）。

    三条一起返回是刻意的：只有布尔值时，报告里只能显示「✗」，
    而「为什么 ✗」必须去复跑一次才能知道 —— 而那一次的结果可能与这次不同
    （涉及真模型时尤其如此）。
    """

    name: str
    passed: bool
    detail: str = ""

    def mark(self) -> str:
        return "✅" if self.passed else "✗"


def finding(name: str, passed: bool, detail: str = "") -> Finding:
    return Finding(name=name, passed=bool(passed), detail=detail)


__all__ = [
    "HEDGE_PHRASES",
    "HEDGE_PREFIX_CHARS",
    "HEDGE_PREFIX_MAX_CHARS",
    "HEDGE_SHORT_CHARS",
    "Finding",
    "contains",
    "coverage",
    "digit_tokens",
    "finding",
    "is_emptyish",
    "is_hedged",
    "matching_key_facts",
    "missing_key_facts",
    "normalize_text",
    "to_halfwidth",
    "tokenish_length",
    "unsupported_numbers",
]
