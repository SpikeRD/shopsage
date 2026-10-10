"""中文/混排文本的切词：**关键词腿的底座**（`10.rag.md` 第 4 节第 1 条要的「BM25 互补」）。

为什么需要它：PostgreSQL 的 `to_tsvector('simple', 中文)` **不切词** ——
`simple` 只做小写化与按空白切分，而中文没有空白，于是一整段中文会变成**一个** lexeme：
「第 3 条：自签收之日起 7 天内…」整句一个词，任何查询都召不到它（只有整句完全相同才命中）。
实测：`select to_tsvector('simple', '自签收之日起 7 天内')` → `'3':3 '7':6` ——
**汉字全部消失**，剩下的只有数字。这就是「中文默认全文检索几乎是坏的」的原因。

解法（本仓实测过三种，见 `docs/runbook/10.rag-runbook.md` §3.2）：

| 方案 | 结论 |
|---|---|
| `zhparser` / `pg_jieba` 扩展 | 最正统，但要在开发镜像里装扩展（`pgvector/pgvector:pg16` 没有），换机器即失效 |
| `pg_trgm`（trigram） | 只支持 LIKE/相似度，**没有词频与长度归一化**，做不了 BM25 |
| ✅ **CJK n-gram 词表 + tsvector + SQL 内手算 BM25** | 零扩展依赖，GIN 索引照常生效，词频/长度归一化都在 tsvector 里 |

具体做法：入库时把文本切成「单字 + 相邻二字（bigram）+ ASCII 词」拼成空格分隔的词表，
交给 `to_tsvector('simple', ...)`（此时 `simple` 的按空白切分正好就是我们要的 tokenizer）。
查询侧同样切词，用 bigram 做**候选门**（覆盖率阈值），用全部词在 SQL 里手算 BM25 排序。

为什么是 1-gram + 2-gram，而不是 jieba 那样分词：
- 分词要词典，词典之外的新词（型号、编号、政策名）都切不出，而 RAG 的查询恰恰大量是**长尾专名**；
- n-gram 没有未登录词问题，且「2-gram 命中率」天然是一个**短语近似匹配**信号：
  查询「7天无理由退货」的 2-gram 覆盖到同一段相邻文本上，等价于「这句话里真的出现过这个词」。
"""

from __future__ import annotations

import re
from typing import Final

# 词元：ASCII 字母数字串（`7`、`bge`、`shopsage.v1`、`2026-09`）或连续 CJK。
# 其它字符（空白、标点、全角括号）一律当分隔符 —— 不做停用词表：
# 1-gram 的停用词噪音由「bigram 候选门 + BM25 的 IDF」压住，维护一张中文停用词表
# 只会让「客户问『的』」这类边界查询变成不可解释的零召回。
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[0-9A-Za-z]+(?:[.\-_'][0-9A-Za-z]+)*|[\u3400-\u4dbf\u4e00-\u9fff]+")

# 单字与 bigram 的最大数量上限：查询里塞一段 500 字的话时，
# 不设上限会让 SQL 里出现上百个参数（PostgreSQL 的参数上限是 65535，但 tsquery 会长得离谱）。
MAX_QUERY_TERMS: Final[int] = 64
MAX_QUERY_CHARS: Final[int] = 200


def is_cjk(char: str) -> bool:
    """是否 CJK 汉字（含扩展 A 区）。"""
    return "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff"


def terms(text: str, *, max_chars: int = MAX_QUERY_CHARS) -> list[str]:
    """切词：ASCII 词（小写化）+ CJK 单字 + CJK bigram；去重保序。

    保序很重要：`query_terms()` 依赖「bigram 在前的顺序」做「去掉尾部未登录词」的截断，
    用 set 会把顺序打乱，于是「哪一端该截」就不可控了。
    """
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text[:max_chars] if max_chars else text):
        piece = match.group(0)
        if piece[0].isascii():
            tokens.append(piece.lower())
            continue
        # CJK：单字 + 相邻二字，按「从前到后」的顺序追加（bigram 紧跟在它的首字之后）
        for index, char in enumerate(piece):
            tokens.append(char)
            if index + 1 < len(piece):
                tokens.append(piece[index : index + 2])

    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered[:MAX_QUERY_TERMS]


def index_tokens(text: str) -> list[str]:
    """入库侧词表（与查询侧同一套切词，保证两边词元空间一致）。

    ⚠️ 入库侧**不做** `MAX_QUERY_CHARS` 截断：截断是查询侧的保护，用在入库侧会
    把长文档的后半段变成「索引里不存在」，症状是「后面几段永远搜不到」。
    """
    return terms(text, max_chars=0)


def index_text(text: str) -> str:
    """入库侧词表拼成的空格分隔串（直接喂 `to_tsvector('simple', ...)`）。"""
    return " ".join(index_tokens(text))


def query_terms(query: str) -> tuple[list[str], list[str]]:
    """查询切词，返回 `(候选词, 兜底词)`。

    - **候选词**（primary）：ASCII 词 + CJK bigram —— 做覆盖率门槛的分母。
      单字**不参与**：单字在中文里几乎人人都有（的/是/不），拿它当门槛等于没有门槛。
    - **兜底词**（fallback）：全部词元（含单字）—— 当候选词一个都没命中时（错字、口语化、
      只打了一个字）再放宽到单字，避免「宁缺毋滥」变成「永远召回不到」。

    返回两个列表而不是一个，是因为它们的**语义不同**：候选词决定「这条能不能进候选集」，
    兜底词只在候选集为空时用来救召回。混在一起写会让「门槛」悄悄变成「排序」。
    """
    all_terms = terms(query)
    primary = [term for term in all_terms if len(term) > 1 or not is_cjk(term)]
    return primary, all_terms


def like_terms(query: str) -> list[str]:
    """给 `LIKE` 兜底用的子串（未登录词查库存在性时用，见 `store.known_terms`）。

    取「长度 >= 2 的原始片段」而不是切好的词：`LIKE` 要的是**原文字面量**，
    拿 bigram 去 LIKE 也能用，但会把「无 理 由」这种被切开的片段也带进来，徒增噪音。
    """
    chunks = [m.group(0) for m in _TOKEN_RE.finditer(query[:MAX_QUERY_CHARS])]
    return [chunk.lower() for chunk in chunks if len(chunk) >= 2]


def approx_tokens(text: str) -> int:
    """token 数的**近似**（E2 的成本口径，见 runbook §7）。

    明确写清它是近似：真正的 tokenizer 在模型侧（bge-m3 用 XLM-R 的 sentencepiece），
    在 Python 里复刻它等于把「词表」变成第二份真相。近似口径：
      - CJK 字符 ≈ 1 token/字（XLM-R 对中文常见字基本一字一 token，生僻字 2~3 个）
      - ASCII 词 ≈ 1 token/词（未登录词会碎成若干 BPE，这里不展开）
    两种近似都会**轻微低估**，所以本文用它做「rerank 前后同样口径的对比」是可靠的
    （同一个函数、同一份文本形态），但**不能**拿它当账单依据。
    """
    cjk = sum(1 for char in text if is_cjk(char))
    words = len(re.findall(r"[0-9A-Za-z]+(?:[.\-_'][0-9A-Za-z]+)*", text))
    return cjk + words


__all__ = [
    "MAX_QUERY_CHARS",
    "MAX_QUERY_TERMS",
    "approx_tokens",
    "index_text",
    "index_tokens",
    "is_cjk",
    "like_terms",
    "query_terms",
    "terms",
]
