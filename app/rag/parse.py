"""文档解析：PDF / Markdown / HTML / 纯文本 → 带页与章节结构的**纯文本**。

为什么自己写解析而不上 `llama-index`（`10.rag.md` 第 3 节里列了它）：

1. **本仓已经有 embedding 门面**。llama-index 最大的价值是它自带 `Settings.embed_model`
   与一堆集成，而本项目第 5 步已经定了「全项目唯一入口是 `app.llm.embed()`」。
   两套 embedding 配置并存，就是两处真相 —— 迟早有人在 llama-index 那边改了模型，
   于是「查询用的向量空间」与「入库用的向量空间」不一致（最坏的那种事故，见 `textkit.py`）。
2. **依赖重量与收益不成比例**。`llama-index-core` 会拉进 nltk、tiktoken、网络栈等一大票传递依赖，
   而本项目实际用到的只有「读文件 → 分块 → 存 pgvector → 检索」四件事，其中分块策略还是**按文档类型定制**的
   （政策按条款、手册按章节、FAQ 一条一块，见 `chunking.py`），框架的通用切分器反而要绕开。
3. **可测试性**。本模块是纯函数（输入文本 → 输出块），可以在没有模型、没有数据库的环境里全量单测；
   换成框架对象后，测试要先构造一堆框架状态。

⚠️ 三条有意保留的取舍：

* **PDF 走可选依赖 `pypdf`**（`uv sync --extra full` 或 dev 组已含），没装时给出**能照着修**的报错。
  不自己写 PDF 解析器：PDF 的字体/编码/CID 映射足够写一本书，手写版在真实扫描件上必然出洋相。
* **不做 OCR**：扫描件（图片型 PDF）抽不出文字是**预期行为**，会记 WARNING 并返回空文本，
  由上层决定要不要走 OCR 管道（本步不做）。
* **不做版面还原**：多栏 PDF 会串行，这是纯文本抽取的共同限制；真需要版面时应当换解析服务。
"""

from __future__ import annotations

import html as html_module
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES: Final[tuple[str, ...]] = (".md", ".markdown", ".txt", ".html", ".htm", ".pdf")

# PDF 分页标记：用 form-feed，因为它是**正文里不可能自然出现**的字符。
# 为什么不用「第 N 页」这类文字：正文里出现「第 3 页」时会把页码算错，
# 而页码错了的表现是溯源指向错误的页 —— 比没有页码更糟。
PAGE_BREAK: Final[str] = "\f"

# 归一化时的替换：只处理「排版垃圾」，不动语义字符
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)


@dataclass(frozen=True)
class ParsedDocument:
    """解析结果：纯文本（含页码分页符）+ 元数据。"""

    source: str
    text: str
    kind: str = "text"  # markdown | html | text | pdf
    pages: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """抽不出文字（空文档 / 扫描件）：调用方据此决定是告警还是失败。"""
        return not self.text.strip()


def normalize(text: str) -> str:
    """统一换行、去零宽字符、压掉超过两个的连续空行。

    ⚠️ **不**做「压掉所有多余空格」：中文排版里 `第 3 条` 这种数字与汉字之间的空格
    是有意义的（切词时会成为 token 边界），全压掉反而让 `第 3 条` 变成 `第3条`，
    与用户输入的 `第3条` 命中不一致 —— 保留空格比统一空格更安全。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_ZERO_WIDTH)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_kind(path: Path | str) -> str:
    """按后缀判类型；不认识的类型按纯文本处理（保持「能吃任何文件」的宽容度）。"""
    suffix = Path(path).suffix.lower()
    if suffix in (".md", ".markdown"):
        return "markdown"
    if suffix in (".html", ".htm"):
        return "html"
    if suffix == ".pdf":
        return "pdf"
    return "text"


def parse_markdown(raw: str) -> str:
    """Markdown → 纯文本。

    只做三件**必要**的事，刻意不引入 markdown 解析库：
      ① 去掉代码围栏的标记行（保留代码内容：它可能就是答案）；
      ② 链接 `[文本](url)` → `文本`（url 对检索无意义，且会让切词多出噪音）；
      ③ 去掉强调符号（`**` / `__` / ~~），保留标题的 `#` ——
         章节识别要靠它（见 `chunking.detect_sections`），去掉就再也切不出章节了。
    """
    lines: list[str] = []
    in_fence = False
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence:
            line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)  # 图片 → alt 文本
            line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)  # 链接 → 文本
            line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)
            line = re.sub(r"(?<!\w)(\*|_)(?!\s)(.+?)(?<!\s)\1(?!\w)", r"\2", line)
            line = re.sub(r"~~(.+?)~~", r"\1", line)
        lines.append(line)
    return "\n".join(lines)


def parse_html(raw: str) -> str:
    """HTML → 纯文本：去 `script`/`style`，块级标签转换行，其余去标签 + 解实体。

    同样刻意不引入解析库：现在要的是「可检索的文本」，不是 DOM。
    真要精确定位页面元素（如只取 `<article>`）时，用 `selectolax`/`lxml` 更好 ——
    但那是**另一种需求**，本项目没有。
    """
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    # 标题与块级标签转换行，顺带把标题层级保留成 `#`，让章节识别能复用 markdown 的规则
    text = re.sub(r"(?is)<h([1-6])\b[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", text)
    text = re.sub(r"(?is)</h[1-6]>", "\n", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|li|tr|section|article|table|ul|ol|blockquote)>", "\n", text)
    text = re.sub(r"(?is)<li\b[^>]*>", "- ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html_module.unescape(text)


def parse_pdf(raw: bytes) -> str:
    """PDF → 纯文本（每页之间插入 `PAGE_BREAK`）。需要可选依赖 `pypdf`。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖已装时不可达
        raise ParseError(
            "解析 PDF 需要可选依赖 pypdf。请执行 `uv add pypdf`（或 `uv sync --extra full`）；"
            "也可以先把 PDF 转成 Markdown/文本再入库（解析质量更好，且能人工校对）。"
        ) from exc

    import io

    reader = PdfReader(io.BytesIO(raw))
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 - 单页失败不该让整篇失败
            logger.warning("PDF 某一页抽取失败（已跳过该页）：%s", exc)
            pages.append("")

    text = PAGE_BREAK.join(pages)
    if not text.strip():
        # 典型成因：扫描件（图片）、全加密、字体无 ToUnicode 映射。
        # 明说「这是预期行为、本步不做 OCR」，避免下一个人花一天去查解析器 bug。
        logger.warning(
            "PDF 抽不出任何文字（共 %d 页）：多半是扫描件/图片型 PDF 或加密文档。"
            "本步不做 OCR —— 请改用文本版或用 OCR 工具预处理。",
            len(pages),
        )
    return text


class ParseError(RuntimeError):
    """解析失败（缺依赖、文件读不了、格式不认）：消息必须能照着修。"""


def parse_text(path: Path | str, raw: str | bytes | None = None) -> ParsedDocument:
    """解析一个文件（或直接给内容）→ `ParsedDocument`。

    参数：
        path: 文件路径（也用于生成 `source` —— **相对仓库根的路径**，溯源要稳定且可点）。
        raw:  已读好的内容；`None` 时现场读文件（PDF 按二进制读）。
    """
    file_path = Path(path)
    kind = detect_kind(file_path)

    if raw is None:
        if not file_path.exists():
            raise ParseError(f"文件不存在：{file_path}")
        raw = file_path.read_bytes() if kind == "pdf" else file_path.read_text(encoding="utf-8")

    if kind == "pdf":
        assert isinstance(raw, bytes)
        text = parse_pdf(raw)
        pages = text.count(PAGE_BREAK) + 1 if text else 0
    else:
        content = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if kind == "html":
            text = parse_html(content)
        elif kind == "markdown":
            text = parse_markdown(content)
        else:
            text = content
        pages = 0

    normalized = normalize(text)
    return ParsedDocument(
        source=file_path.as_posix(),
        text=normalized,
        kind=kind,
        pages=pages,
        metadata={"suffix": file_path.suffix.lower(), "bytes": len(raw)},
    )


__all__ = [
    "PAGE_BREAK",
    "SUPPORTED_SUFFIXES",
    "ParseError",
    "ParsedDocument",
    "detect_kind",
    "normalize",
    "parse_html",
    "parse_markdown",
    "parse_pdf",
    "parse_text",
]
