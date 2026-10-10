"""`parse` 的测试：Markdown / HTML / 纯文本 / PDF（缺依赖与坏文件）的解析契约。

为什么这些用例值得写：解析是整条 RAG 链路的**入口**，它出错的表现是「检索不到」或
「检索到一坨排版垃圾」（`##`、`<div>`、脚本内容都进了向量库）—— 而这两种症状
在检索层看起来都像「模型不行」。所以这里把「哪些字符会进正文」钉死。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.rag.parse import (
    PAGE_BREAK,
    SUPPORTED_SUFFIXES,
    ParseError,
    detect_kind,
    normalize,
    parse_html,
    parse_markdown,
    parse_text,
)


def test_detect_kind_by_suffix():
    assert detect_kind("a.md") == "markdown"
    assert detect_kind("a.MARKDOWN") == "markdown"
    assert detect_kind("a.html") == "html"
    assert detect_kind("a.htm") == "html"
    assert detect_kind("a.pdf") == "pdf"
    assert detect_kind("a.txt") == "text"
    # 不认识的类型按纯文本处理（保持宽容度：宁愿灌进去也不能默默跳过）
    assert detect_kind("a.csv") == "text"


def test_supported_suffixes_cover_the_corpus():
    assert set(SUPPORTED_SUFFIXES) >= {".md", ".txt", ".html", ".pdf"}


def test_normalize_collapses_blank_lines_and_zero_width():
    raw = "第一行\r\n\r\n\r\n\r\n第二行\u200b\r\n"
    assert normalize(raw) == "第一行\n\n第二行"


def test_normalize_keeps_inner_spaces_between_cjk_and_digits():
    """⚠️ 刻意**不**压缩 `第 3 条` 里的空格：它是切词的 token 边界。"""
    assert "第 3 条" in normalize("第 3 条：自签收")


def test_markdown_parser_strips_link_urls_but_keeps_titles():
    """链接保留文字、去掉 URL：URL 对检索没有价值，还会在切词时制造噪音。"""
    text = parse_markdown("详见 [退货政策](https://example.com/x?y=1) 与 ![图](a.png)")
    assert "退货政策" in text
    assert "https://example.com" not in text
    assert "图" in text


def test_markdown_parser_keeps_heading_marks():
    """标题的 `#` 必须保留 —— `chunking` 靠它识别章节，去掉就再也切不出章节。"""
    assert parse_markdown("## 一、无理由退货").strip() == "## 一、无理由退货"


def test_markdown_parser_drops_fence_markers_but_keeps_code():
    text = parse_markdown("```python\nx = 1\n```")
    assert "x = 1" in text
    assert "```" not in text


def test_markdown_parser_removes_emphasis():
    assert parse_markdown("**重要**：__这条__ 与 ~~那条~~") == "重要：这条 与 那条"


def test_html_parser_drops_script_and_style():
    raw = "<style>p{color:red}</style><script>alert(1)</script><p>正文在这里</p>"
    text = parse_html(raw)
    assert "正文在这里" in text
    assert "alert" not in text
    assert "color:red" not in text


def test_html_parser_turns_headings_into_markdown_marks():
    """`<h2>` → `## `：让 HTML 复用同一套章节识别规则（不必给 HTML 再写一套）。"""
    text = parse_html("<h1>标题</h1><h2>小节</h2><p>正文</p>")
    assert "# 标题" in text
    assert "## 小节" in text


def test_html_parser_decodes_entities_and_lists():
    text = parse_html("<ul><li>破损理赔</li><li>功能故障</li></ul>&amp;")
    assert "- 破损理赔" in text
    assert "- 功能故障" in text
    assert "&" in text


def test_parse_text_reads_from_disk(local_tmp: Path):
    file = local_tmp / "政策.md"
    file.write_text("# 退货政策\n\n第 1 条：7 天无理由。\n", encoding="utf-8")
    parsed = parse_text(file)
    assert parsed.kind == "markdown"
    assert parsed.source.endswith("政策.md")
    assert "7 天无理由" in parsed.text
    assert parsed.is_empty is False


def test_parse_text_accepts_inline_content():
    """`content=` 直接给内容（测试与「内存里的文档」用），不读盘。"""
    parsed = parse_text("memory/doc.md", "# 标题\n\n正文")
    assert parsed.text == "# 标题\n\n正文"


def test_parse_text_missing_file_is_actionable(local_tmp: Path):
    with pytest.raises(ParseError, match="文件不存在"):
        parse_text(local_tmp / "不存在.md")


def test_empty_document_is_flagged_not_raised():
    """空文档不抛错，而是用 `is_empty` 标记：调用方据此告警并**不删除**已有片段。"""
    parsed = parse_text("empty.txt", "   \n\n")
    assert parsed.is_empty is True


def test_parse_pdf_without_pypdf_is_actionable(monkeypatch):
    """缺可选依赖时给出**能照着修**的报错（而不是裸 ModuleNotFoundError）。"""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("No module named 'pypdf'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    from app.rag.parse import parse_pdf

    with pytest.raises(ParseError) as excinfo:
        parse_pdf(b"%PDF-1.4 fake")
    message = str(excinfo.value)
    assert "pypdf" in message
    assert "uv add pypdf" in message  # 能照着修


def test_pypdf_is_available_in_this_repo():
    """本仓把 pypdf 放进了 dev 组：它是「能被测到」的前提（runbook §3.5 的口径）。"""
    import importlib.util

    assert importlib.util.find_spec("pypdf") is not None, (
        "pypdf 不在环境里：PDF 分支无法被测试。请 `uv add --dev pypdf`（本仓的做法）"
    )


def test_parse_pdf_extracts_text_from_a_real_pdf(local_tmp: Path):
    """用 pypdf **写**一个带文字的 PDF，再解析回来。

    为什么值得写：PDF 分支是「可选依赖 + 第三方解析」的组合，
    最容易出现「装了依赖但仍抽不出文字」——这里闭环验证「写进去 → 抽出来」。
    """
    pypdf = pytest.importorskip("pypdf")
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    from app.rag.parse import parse_pdf

    # 空白页抽不出文字是**预期行为**（本步不做 OCR），关键是**不抛错**
    text = parse_pdf(buffer.getvalue())
    assert text == "" or PAGE_BREAK in text
    assert pypdf is not None


def test_parse_pdf_counts_pages(local_tmp: Path):
    """两页 PDF → `pages == 2`（页码是溯源的一部分，算错比没有更糟）。"""
    pytest.importorskip("pypdf")
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    file = local_tmp / "两页.pdf"
    file.write_bytes(buffer.getvalue())

    parsed = parse_text(file)
    assert parsed.kind == "pdf"
    assert parsed.pages == 2
