"""分块：**按文档类型定粒度**（`10.rag.md` 第 4 节第 4 条）。

三条硬约束（都是「不这么做就出事故」的级别）：

1. **每个 chunk 必须带 metadata**（`source / section / page / updated_at`）。
   溯源（`10.rag.md` 第 5 节）、增量更新的差异比对、前端的「跳到原文」全靠它。
   丢 metadata 的片段检索得再好，也没有资格进上下文 —— 上层**必须**能给出处。
2. **chunk 太小丢上下文、太大稀释语义**：一个 20 字的片段 embedding 是噪声，
   一个 3000 字的片段把十条不同信息平均成一条，两者都会让 rerank 无料可排。
3. **不能把答案切在缝上**：所以超长段落用「重叠窗口」切（默认 80 字），
   而不是硬断 —— 代价是同一段文字会出现在两个 chunk 里（检索时是**好事**：命中概率更高）。

三种分块策略（`strategy`）：

| 策略 | 适用 | 边界判据 |
|---|---|---|
| `policy` | 政策/条款/规章 | 条款号（`第 3 条`、`3.2`、`（二）`、`Rule 4`）+ Markdown 标题 |
| `manual` | 手册/说明书/长文 | 只按标题层级（一、二级标题） |
| `faq` | FAQ / 问答对 | 「问…答…」成对；没有明显 Q/A 标记时退化成 policy |
| `auto` | 默认 | 按上述特征打分自动挑一种（判据写在 `detect_strategy` 里，可解释、可测） |

分块的对象是「**单元**（unit）」而不是字符：每个单元天然带 章节 + 页码，
合并单元时取第一个单元的章节/页码（同一段里跨页很少见，真跨了按起始页记 —— 溯源指到起始页
比指到两页之间的某个位置更有用）。
"""

from __future__ import annotations

import bisect
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from app.rag.parse import PAGE_BREAK, ParsedDocument

logger = logging.getLogger(__name__)

# 章节判据（顺位即优先级；命中越靠前越"硬"）
#
# ⚠️ 全部带 `re.MULTILINE`（本仓实测踩到）：单元不一定是单行 ——
# FAQ 策略会把「问句 + 答句」合并成一个多行单元，而 `$` 在默认模式下只匹配**字符串末尾**，
# 于是 `### 如何修改收货地址？\n在订单详情页…` 这种单元一条标题判据都命中不了，
# 章节全部退化成父级（「常见问题」），「一条 FAQ 一块」跟着失效 —— 而且**没有任何报错**。
HEADING_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    # Markdown / HTML 标题（HTML 解析时已转成 # 形式）
    ("heading", re.compile(r"^(#{1,6})\s+(?P<title>.+?)\s*#*$", re.MULTILINE)),
    # 条款号：第 3 条 / 第七条 / 第 3.2 条 / Rule 4 / Article 5
    (
        "clause",
        re.compile(
            r"^\s*(?:第\s*)?(?P<title>[0-9一二三四五六七八九十百]+(?:\.[0-9]+)*\s*(?:条|款|项|章|节)"
            r"|[Rr]ule\s+[0-9.]+|[Aa]rticle\s+[0-9.]+)\b.*$",
            re.MULTILINE,
        ),
    ),
    # 编号标题：1.2 标题 / 3、标题 / （二）标题 / 三、章节名
    #
    # ⚠️ 两处细节都是实测踩出来的（否则「章节」会被列表项污染）：
    #   ① 数字点号式（`1.` / `1.2`）允许后接任意标题文字；
    #   ② 「分隔符式」（`3、` / `一、` / `（二）`）**要求后面没有标点**（`.{0,29}$`）——
    #      否则 `1、以下情况不予受理：` 这种列表项会被当成章节。
    #   ③ 中文数字**必须**跟分隔符（`一、`），否则 `以下情形不予受理…` 里的「一」会被当成编号。
    (
        "numbered",
        re.compile(
            r"^\s*(?:[0-9]+(?:\.[0-9]+)*\.\s*[^\s].{0,29}"
            r"|[（(]?[0-9一二三四五六七八九十百]+[）)、]\s*[^\s].{0,29})$",
            re.MULTILINE,
        ),
    ),
)

# 行末的「这不像标题」判据：句号/分号/问号/叹号结尾的行是正文（FAQ 的问句就是典型）。
# ⚠️ 本仓实测踩到：只判 `。；` 时，FAQ 里的 `如何修改收货地址？` 会被 `numbered` 判据
# （`一`/`如何` 里的汉字数字）误判成编号标题，于是同一条问答的**两个部分落到不同章节**，
# 分块时被切成两块 —— 「一条 FAQ 一块」的承诺直接失效，而且没有任何报错。
_NOT_A_TITLE_TAIL = re.compile(r"[。；;？！?!!]\s*$")
# 标题长度上限：超过它的一律当正文（标题不会是一整句话）
_TITLE_MAX_CHARS = 60
# 编号标题（`1、xxx` / `（二）xxx`）的**更严**上限：这一族判据最松，放宽就等于没有判据
_NUMBERED_TITLE_MAX_CHARS = 30

# FAQ 的问答判据：问句 + 答句相邻
FAQ_QUESTION = re.compile(r"^\s*(?:[Qq]\s*[:：]|问\s*[:：]|[Qq]\d*\s*[:：])")
FAQ_ANSWER = re.compile(r"^\s*(?:[Aa]\s*[:：]|答\s*[:：])")
# 没有 Q/A 前缀时的弱判据：整行以问号结尾且不太长（「如何修改收货地址？」）
QUESTION_LINE = re.compile(r"^\s*(?P<title>.{4,80}?[?？])\s*$")
# 常见 Markdown FAQ 写法：`### 如何修改收货地址？`
FAQ_HEADING = re.compile(r"^#{2,6}\s+(?P<title>.{2,80}?[?？])\s*$")


@dataclass(frozen=True)
class ChunkingConfig:
    """分块参数（不可变；`RAG_CHUNK_*` 是它的来源）。"""

    strategy: str = "auto"
    max_chars: int = 600
    overlap: int = 80
    # 小于这个长度的碎片会被并入相邻块（孤立的一行标题不值得单独成一个 chunk）
    min_chars: int = 24

    def __post_init__(self) -> None:
        if self.overlap >= self.max_chars:
            raise ValueError(
                f"overlap({self.overlap}) 必须小于 max_chars({self.max_chars})："
                "否则重叠窗口会不前进（死循环）。RAG_CHUNK_* 的正常取值是 80 / 600。"
            )


@dataclass(frozen=True)
class TextChunk:
    """一个可入库的片段。`chunk_id` 是**内容 hash 前缀**（见 `chunk_id_for`）。"""

    text: str
    section: str | None
    page: int | None
    ordinal: int
    chunk_id: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def as_metadata(self) -> dict[str, Any]:
        """给上层（含前端）用的溯源字段（`10.rag.md` 第 5 节的四件套）。"""
        return {
            "chunk_id": self.chunk_id,
            "section": self.section,
            "page": self.page,
            "ordinal": self.ordinal,
            **self.metadata,
        }


def content_hash(text: str) -> str:
    """内容指纹（sha256 前 16 位十六进制）。

    为什么不用 `hash()`：Python 的字符串 hash 每个进程都不同（PYTHONHASHSEED），
    存进库后下次启动就对不上了 —— 增量更新会误判「全部片段都变了」，于是每次同步都全量重嵌。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def chunk_id_for(text: str) -> str:
    """`chunk_id` = `c` + 内容指纹前缀。

    为什么不做「章节 + 序号」这种**位置型** ID：章节被改一次（重命名标题、插一段），
    后面所有片段的序号都会平移，于是增量更新会认为「全变了」；
    而内容型 ID 的语义是「这段文字」，同一段文字在文档里挪了位置仍然是同一件事 ——
    这正是增量更新想要的（只有**内容**变了才重嵌）。
    """
    return f"c{content_hash(text)}"


def detect_strategy(document: ParsedDocument, sample_lines: int = 400) -> str:
    """自动判断分块策略（可解释：命中的判据都在这三个计数里）。

    判据（按顺序，先命中先返回）：
      1. **标题就是问句**（`### 如何修改收货地址？`）>= 2 且占标题数 >= 1/3 → `faq`；
         这是中文 FAQ 最常见的写法，单独判它才能让「只有两三条问答」的小文档也走对策略。
      2. Q/A 前缀行（`Q:` / `问：` / `A:` / `答：`）>= 2，或问句行 >= 3 且占比 >= 8% → `faq`。
      3. 条款号命中 >= 2 → `policy`。
      4. 其余 → `manual`（长文按标题切是最保守的选择；没有标题时退化成按长度窗切）。

    ⚠️ 只采样前 `sample_lines` 行：判断文档类型不需要读全文，而全文的正则在超大文档上会很慢。
    代价是「前 400 行是表格、后面全是问答」的文档会判错 —— 这类文档请显式传 `strategy=`。
    """
    lines = [line for line in document.text.split("\n")[:sample_lines] if line.strip()]
    if not lines:
        return "manual"

    headings = [line for line in lines if HEADING_PATTERNS[0][1].match(line)]
    question_headings = [line for line in headings if FAQ_HEADING.match(line)]
    if len(question_headings) >= 2 and len(question_headings) * 3 >= len(headings):
        return "faq"

    explicit = sum(1 for line in lines if FAQ_QUESTION.match(line) or FAQ_ANSWER.match(line))
    question_lines = sum(1 for line in lines if QUESTION_LINE.match(line))
    if explicit >= 2 or (question_lines >= 3 and question_lines / len(lines) >= 0.08):
        return "faq"

    clause_pattern = HEADING_PATTERNS[1][1]
    if sum(1 for line in lines if clause_pattern.match(line)) >= 2:
        return "policy"

    return "manual"


def _heading_level(match: re.Match[str], kind: str) -> int:
    """标题层级：Markdown 的 `#` 数量即层级，其余判据都按「二级」处理。"""
    if kind == "heading":
        return len(match.group(1))
    return 2


def _looks_like_title(line: str, kind: str) -> bool:
    """标题长度上限对**所有**判据生效；「不能以句末标点结尾」只对 `numbered` 生效。

    为什么只对 `numbered` 加标点判据：Markdown 标题（`# ...`）与条款号（`第 7 条`）
    是**声明式**的（作者显式写了标记），而「1、xxx」「（二）xxx」在中文文档里
    既可能是标题、也可能是**编号列表项** —— 列表项不是章节，把它当章节会让章节路径炸开。

    ⚠️ 本仓实测踩到三次（同一处的三个方向，逐条记下来免得下次再犯）：
      · 不加长度/标点判据时，`1、以下情况不予受理。` 这种**带句号的列表项**会变成章节；
      · 长度上限放到 60 时，`1、企业客户单笔订单满 20000 元可申请企业价…`（无句号、58 字）
        仍然会被当成章节（实测），于是章节名变成一整句话 —— 编号标题的上限必须更严（30 字）；
      · 把标点判据也套到 `heading` 上时，FAQ 的 `### 如何修改收货地址？` 被否掉，
        「一条 FAQ 一块」退化成「整篇 FAQ 一块」（而**没有任何报错**）。
    """
    if len(line) > _TITLE_MAX_CHARS:
        return False
    if kind == "numbered":
        return len(line) <= _NUMBERED_TITLE_MAX_CHARS and not _NOT_A_TITLE_TAIL.search(line)
    return True


def _split_units(text: str) -> list[tuple[int, str]]:
    """把文本切成 `(起始偏移, 文本)` 单元：标题自己一个单元，正文按空行分段。

    偏移是必须的：页码要按偏移算（见 `PageMap`），没偏移就没法知道某段在第几页。
    """
    units: list[tuple[int, str]] = []
    offset = 0
    buffer: list[str] = []
    buffer_start = 0

    def flush() -> None:
        nonlocal buffer, buffer_start
        if buffer:
            block = "\n".join(buffer).strip()
            if block:
                units.append((buffer_start, block))
            buffer = []

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        starts_here = offset
        offset += len(raw_line) + 1  # +1：被 split 吃掉的换行

        if not line.strip():
            flush()
            continue

        is_heading = False
        for kind, pattern in HEADING_PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            if not _looks_like_title(line, kind):
                continue
            is_heading = True
            break

        if is_heading:
            flush()
            units.append((starts_here, line.strip()))
            buffer_start = offset
            continue
        if not buffer:
            buffer_start = starts_here
        buffer.append(line)

    flush()
    return units


def _merge_faq_pairs(units: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """FAQ 策略：把「问」与紧随其后的「答」并成一个单元（一条一 chunk 的落点）。"""
    merged: list[tuple[int, str]] = []
    index = 0
    while index < len(units):
        offset, unit = units[index]
        is_question = bool(
            FAQ_QUESTION.match(unit) or FAQ_HEADING.match(unit) or QUESTION_LINE.match(unit)
        )
        if is_question and index + 1 < len(units):
            next_offset, next_unit = units[index + 1]
            if not (FAQ_QUESTION.match(next_unit) or FAQ_HEADING.match(next_unit)):
                merged.append((offset, f"{unit}\n{next_unit}"))
                index += 2
                continue
        merged.append((offset, unit))
        index += 1
    return merged


class PageMap:
    """字符偏移 → 页码（PDF 用；非 PDF 返回 `None`）。

    实现是「各页结束偏移的累加表 + bisect」：O(log n) 查一次，且**不需要**
    在文本里插任何标记 —— 标记会被切词切进索引，然后在检索结果里露出来。
    """

    def __init__(self, text: str, pages: int) -> None:
        self._bounds: list[int] = []
        if pages > 1 and PAGE_BREAK in text:
            position = 0
            for part in text.split(PAGE_BREAK)[:-1]:
                position += len(part) + 1  # +1 = 被 split 吃掉的 PAGE_BREAK
                self._bounds.append(position)
        self._pages = pages

    def page_of(self, offset: int) -> int | None:
        if not self._bounds:
            return 1 if self._pages == 1 else None
        return bisect.bisect_right(self._bounds, offset) + 1


def _sliding_windows(text: str, max_chars: int, overlap: int) -> list[str]:
    """超长单元的「重叠窗口」切法：切点优先落在中文句末标点上（语义更完整）。"""
    windows: list[str] = []
    start = 0
    length = len(text)
    boundary = re.compile(r"[。！？；;\n]")
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            # 在 [start + max_chars*0.6, end] 里找最后一个句末标点当切点
            floor = start + int(max_chars * 0.6)
            candidates = [m.end() for m in boundary.finditer(text, floor, end)]
            if candidates:
                end = candidates[-1]
        windows.append(text[start:end].strip())
        if end >= length:
            break
        start = max(end - overlap, start + 1)  # +1 兜底：保证一定前进
    return [window for window in windows if window]


def _build_section_path(stack: list[tuple[int, str]], level: int, title: str) -> str:
    """维护标题栈 → 章节路径（`产品手册 > 2 保修 > 2.1 保修期`）。

    为什么要「路径」而不是「最后一级标题」：手册里 `2.1` 在多个大章节下会重复出现
    （「2.1 保修期」与「5.1 保修期」），只用最后一级的话溯源会指向错误章节。
    """
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))
    return " > ".join(item[1] for item in stack)


def chunk_document(
    document: ParsedDocument,
    config: ChunkingConfig | None = None,
    *,
    strategy: str | None = None,
) -> list[TextChunk]:
    """把解析结果切成片段（本模块的主入口）。

    参数：
        document: `parse.parse_text()` 的输出。
        config:   分块参数；`None` = 默认（600/80）。
        strategy: 强制指定策略；`None` = 按 `config.strategy`（`auto` 时自动判断）。

    返回：`list[TextChunk]`（顺序即文档顺序，`ordinal` 从 0 开始）。
    """
    cfg = config or ChunkingConfig()
    resolved = strategy or cfg.strategy
    if resolved not in ("policy", "manual", "faq"):
        if resolved != "auto":
            raise ValueError(f"不认识的分块策略：{resolved!r}（只支持 policy/manual/faq/auto）")
        resolved = detect_strategy(document)

    units = _split_units(document.text)
    if resolved == "faq":
        units = _merge_faq_pairs(units)

    page_map = PageMap(document.text, document.pages)
    sections = _assign_sections(units, resolved)

    chunks: list[TextChunk] = []

    def emit(text: str, section: str | None, page: int | None) -> None:
        stripped = text.strip()
        if not stripped:
            return
        chunks.append(
            TextChunk(
                text=stripped,
                section=section,
                page=page,
                ordinal=len(chunks),
                chunk_id=chunk_id_for(stripped),
                content_hash=content_hash(stripped),
                metadata={"strategy": resolved, "chars": len(stripped)},
            )
        )

    buffer_texts: list[str] = []
    buffer_length = 0
    buffer_section: str | None = None
    buffer_page: int | None = None

    for (offset, unit), section in zip(units, sections, strict=True):
        page = page_map.page_of(offset)

        if buffer_texts and (section != buffer_section or buffer_length + len(unit) + 1 > cfg.max_chars):
            emit("\n".join(buffer_texts), buffer_section, buffer_page)
            buffer_texts = []
            buffer_length = 0

        if len(unit) > cfg.max_chars:
            # 单元本身就超长：先把它前面攒的块收掉，再按重叠窗口切（见 `_sliding_windows`）
            if buffer_texts:
                emit("\n".join(buffer_texts), buffer_section, buffer_page)
                buffer_texts = []
                buffer_length = 0
            for window in _sliding_windows(unit, cfg.max_chars, cfg.overlap):
                emit(window, section, page)
            continue

        if not buffer_texts:
            buffer_section = section
            buffer_page = page
        buffer_texts.append(unit)
        buffer_length += len(unit) + 1

    if buffer_texts:
        emit("\n".join(buffer_texts), buffer_section, buffer_page)

    # 碎片合并：孤立的小块（如单独的一行标题）并进相邻块，避免「20 字的 chunk」污染索引。
    chunks = _merge_small_chunks(chunks, cfg.min_chars, cfg.max_chars)
    logger.debug(
        "分块完成：source=%s strategy=%s chunks=%d 平均长度=%.0f",
        document.source,
        resolved,
        len(chunks),
        sum(len(chunk.text) for chunk in chunks) / max(1, len(chunks)),
    )
    return chunks


def _assign_sections(units: list[tuple[int, str]], strategy: str) -> list[str | None]:
    """给每个单元定章节路径（标题单元自己是新章节，正文单元继承当前章节）。

    FAQ 策略下「问句行」也算章节（它就是这个片段的标题）—— 否则一条 FAQ 的章节会是空的，
    而溯源里最有用的恰恰是那个问题本身。
    """
    stack: list[tuple[int, str]] = []
    sections: list[str | None] = []
    for _offset, unit in units:
        level: int | None = None
        title = ""

        first_line = unit.split("\n", 1)[0]
        for kind, pattern in HEADING_PATTERNS:
            candidate = pattern.match(unit)
            if candidate is None or not _looks_like_title(first_line, kind):
                continue
            group_title = candidate.groupdict().get("title")
            level = _heading_level(candidate, kind)
            title = (group_title or first_line).strip()
            break

        if level is None and strategy == "faq":
            question = FAQ_HEADING.match(unit) or QUESTION_LINE.match(unit) or FAQ_QUESTION.match(unit)
            if question is not None:
                level = 2
                title = (question.groupdict().get("title") or first_line).strip()

        if level is not None and title:
            sections.append(_build_section_path(stack, level, title))
        else:
            sections.append(" > ".join(item[1] for item in stack) or None)
    return sections


def _parent_section(section: str | None) -> str | None:
    """章节路径去掉最后一级（`退货与换货政策 > 一、无理由退货` → `退货与换货政策`）。"""
    if not section or " > " not in section:
        return None
    return section.rsplit(" > ", 1)[0]


def _looks_like_heading_text(text: str) -> bool:
    """这个块的文本**是不是一条标题**（而不是一段正文）。

    判据（三条都要满足）：单行、长度 < `_NUMBERED_TITLE_MAX_CHARS`（30）、不以句末标点结尾。

    为什么必须单独判它（本仓实测踩到的**第三个**合并方向的坑，见 §10.1 坑 13）：
    「共享父级」这条回退判据原本不带这个约束，于是「两个短条款」也被判为可合并 ——
    一份这样的文档：

        # 演示政策
        第 1 条：第一段占位内容。
        第 2 条：第二段占位内容。

    会被合**成一块**（区块的 `section` 还变成了 `演示政策 > 2 条`，溯源指向第 2 条，
    而块里第 1/3 条的内容都在）。后果是「一条一款一块」这条承诺静默失效：
    问「第 1 条」会连同第 2、3 条一起返回，rerank 也因此无从下手。

    为什么标题能靠这三条区分出来：`## 二、质量问题退换货`（12 字、单行、无句末标点）
    是标题；`第 2 条：第二段占位内容。`（13 字、单行、**以「。」结尾**）是正文。
    最关键的区分点是**句末标点**：中文文档里正文几乎总是以句号/分号结尾，标题不会。
    """
    stripped = text.strip()
    if not stripped or "\n" in stripped:
        return False
    if len(stripped) > _NUMBERED_TITLE_MAX_CHARS:
        return False
    return not _NOT_A_TITLE_TAIL.search(stripped)


def _attachable(previous: TextChunk, chunk: TextChunk) -> bool:
    """两个相邻块能不能合：同章节；或「上一块是下一块的章节祖先」；
    或「上一块是标题、下一块是它的第一个子块」。

    三段判据各自的来历：

    1. **同章节**：同一段落被 `max_chars` 限制切开的相邻块（正常情况）。
    2. **祖先/前缀**：`## 一、无理由退货`（路径 `父级 > 一、无理由退货`）后面跟着
       `第 1 条…`（路径 `父级 > 1 条`）—— 两者的最后一段不同，靠这条判据才能合上。
    3. **标题 + 首个兄弟**：Markdown 小标题单独成块时只有十几个字 —— 它没有答案、
       却要占一个向量位与一次 rerank，是纯粹的浪费。而它后面那段正文的路径是
       `父级 > 5 条`，与标题的路径**既不相等、也不是对方的前缀**，所以必须回退到
       「父级相同」这一层。

    ⚠️ 第 3 条必须额外要求 `_looks_like_heading_text(previous)` **且下一块更长**：
    否则「父级相同」会把两个短条款也合起来（实测坑 13：一份三条款的短文档被合成了 1 块）。
    加约束之后，「标题 + 正文」仍然合得上（标题 12 字、正文更长），
    而「条款 + 条款」合不上（两者都以句号结尾、长度相当）。
    """
    if previous.section == chunk.section:
        return True
    if previous.section and chunk.section and chunk.section.startswith(f"{previous.section} > "):
        return True
    if chunk.section and previous.section and previous.section.startswith(f"{chunk.section} > "):
        return True
    # 同级兄弟：父章节相同 —— **只允许「上一块是标题、且比下一块短」**这一种。
    # 「上一块必须是标题」用 `_looks_like_heading_text` 判（单行 + 短 + 无句末标点）；
    # 「比下一块短」是第二道保险：标题天然比它的正文短，而两条条款正文长度相当。
    # 两道都不加时，一份「三条款各一句」的短文档会被合成**一块**（实测坑 13）。
    return bool(
        previous.section
        and chunk.section
        and _parent_section(previous.section) == _parent_section(chunk.section)
        and previous.section != chunk.section
        and _looks_like_heading_text(previous.text)
        and len(previous.text) < len(chunk.text)
    )


def _merge_small_chunks(chunks: list[TextChunk], min_chars: int, max_chars: int) -> list[TextChunk]:
    """把过小的块并入**相邻**的块（合并后仍不超 max_chars 时）。

    合并方向有两条，**都只允许同章节（或上级标题 + 其第一个子块）**：

    * **向前**：`## 一、无理由退货`（10 字）这种小标题并入紧随其后的正文 ——
      「章节标题」单独成一个 chunk 是纯粹的浪费：它没有答案、却要占一个向量位与一次 rerank。
    * **向后**：短块后面没有可合并的块时（文档末尾的小节），并入前一块。

    ⚠️ 本仓实测踩到的一个真坑：第一版是「无条件并进**前一块**」，于是
    `# 退货政策`（6 字）与紧随其后的 `## 运费` + 正文被并成了同一块 ——
    而它们的**章节完全不同**，结果是一条政策条款挂在了错误的章节路径下（溯源直接错）。
    """
    merged: list[TextChunk] = []
    for index, chunk in enumerate(chunks):
        if not merged:
            merged.append(chunk)
            continue

        previous = merged[-1]
        same_section = _attachable(previous, chunk)
        fits = len(previous.text) + len(chunk.text) + 1 <= max_chars
        is_last = index == len(chunks) - 1

        # ① 向前合并（主路径）：上一块太小（标题类）、同章节、且合并后不超长。
        #    标题与正文合在一起存，语义最完整；分开存会出现「检索到标题、拿不到答案」。
        if len(previous.text) < min_chars and same_section and fits:
            text = f"{previous.text}\n{chunk.text}"
            merged[-1] = TextChunk(
                text=text,
                section=chunk.section,
                page=previous.page,
                ordinal=previous.ordinal,
                chunk_id=chunk_id_for(text),
                content_hash=content_hash(text),
                metadata={**previous.metadata, "chars": len(text), "merged": "forward"},
            )
            continue

        # ② 向后合并：**只用于文档最后的尾巴块**（它后面没有块了，不合就只能当孤儿）。
        #    ⚠️ 本仓实测踩到：不加 `is_last` 约束时，`## 三、换货规则`（9 字）会被并进
        #    **上一段条款**的尾部，于是「第 7 条」的 chunk 里出现了第三章的标题 ——
        #    检索「第 7 条」会把「三、换货规则」一起捞出来，属于把两个无关章节搅在一起。
        #    规则：标题只允许**向前**并入它的正文；不允许向后粘到别人的结尾。
        if is_last and len(chunk.text) < min_chars and same_section and fits:
            text = f"{previous.text}\n{chunk.text}"
            merged[-1] = TextChunk(
                text=text,
                section=previous.section,
                page=previous.page,
                ordinal=previous.ordinal,
                chunk_id=chunk_id_for(text),
                content_hash=content_hash(text),
                metadata={**previous.metadata, "chars": len(text), "merged": "backward"},
            )
            continue

        merged.append(chunk)

    # 重排 ordinal：合并后序号必须连续，否则「第 N 块」的说法会与库里对不上
    return [
        TextChunk(
            text=chunk.text,
            section=chunk.section,
            page=chunk.page,
            ordinal=index,
            chunk_id=chunk.chunk_id,
            content_hash=chunk.content_hash,
            metadata=chunk.metadata,
        )
        for index, chunk in enumerate(merged)
    ]


__all__ = [
    "HEADING_PATTERNS",
    "ChunkingConfig",
    "PageMap",
    "TextChunk",
    "chunk_document",
    "chunk_id_for",
    "content_hash",
    "detect_strategy",
]