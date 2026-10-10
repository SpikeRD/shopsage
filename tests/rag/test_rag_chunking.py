"""`chunking` 的测试：分块策略、章节路径、字节级细节（含三个实测坑的回归用例）。

分块是 RAG 里**最容易被忽略、又最影响质量**的一步：
- 切碎了 → 每个片段的 embedding 都是噪声，检索得再好也没用；
- 切大了 → 一个片段混十条信息，rerank 无处下手；
- 章节/页码挂了 → 溯源指向错误的条款（比「没有溯源」更糟，因为看起来是对的）。

下面每个「坑」都对应一段实测踩过的经历（见 `docs/runbook/10.rag-runbook.md` §10.1）。
"""

from __future__ import annotations

import pytest

from app.rag.chunking import (
    ChunkingConfig,
    PageMap,
    chunk_document,
    chunk_id_for,
    content_hash,
    detect_strategy,
)
from app.rag.parse import PAGE_BREAK, ParsedDocument


# ── 策略判定 ────────────────────────────────────────────────────────────────


def test_detect_strategy_policy_by_clause_numbers(policy_document):
    assert detect_strategy(policy_document) == "policy"


def test_detect_strategy_faq_by_question_headings(faq_document):
    """FAQ 的判据是「标题就是问句」——中文 FAQ 最常见的写法。"""
    assert detect_strategy(faq_document) == "faq"


def test_detect_strategy_manual_is_the_default(manual_document):
    """没有条款号、也没有问句 → manual（按标题切，最保守）。"""
    assert detect_strategy(manual_document) == "manual"


def test_detect_strategy_on_empty_document_is_manual():
    assert detect_strategy(ParsedDocument(source="x.md", text="   ")) == "manual"


def test_unknown_strategy_raises(policy_document):
    with pytest.raises(ValueError, match="不认识的分块策略"):
        chunk_document(policy_document, strategy="creative")


# ── 政策类：一条一 chunk ────────────────────────────────────────────────────


def test_policy_clauses_become_separate_chunks(policy_document):
    """**一条一款一块**：条款检索的前提（问「第 1 条」不该返回第 2 条的内容）。

    计数用 `assert_text` 而不是 `startswith("第 ")`：小标题与其后的第一条正文是**合法合并**的
    （见 `test_short_heading_merges_forward_into_its_first_clause`），
    所以「第 1 条」那个 chunk 以 `## 一、无理由退货` 开头。
    """
    chunks = chunk_document(policy_document, ChunkingConfig(strategy="policy"))
    bodies = [chunk for chunk in chunks if "第 " in chunk.text]
    assert len(bodies) == 3
    # 每个条款块里只出现一个条款号（不能把两条条款粘在一起）
    assert all(chunk.text.count("第 ") == 1 for chunk in bodies)


def test_every_chunk_carries_metadata(policy_document):
    """`10.rag.md` 第 5 节：每个 chunk 必须带 metadata（溯源与增量更新都靠它）。"""
    chunks = chunk_document(policy_document, ChunkingConfig(strategy="policy"))
    for chunk in chunks:
        assert chunk.section, f"章节为空：{chunk.text[:20]!r}"
        assert chunk.chunk_id.startswith("c")
        assert chunk.content_hash
        assert chunk.ordinal >= 0


def test_section_path_keeps_parent_chapters(policy_document):
    """章节路径保留父级（`政策 > 一、无理由退货`），不只是最后一级标题。"""
    chunks = chunk_document(policy_document, ChunkingConfig(strategy="policy"))
    clause = next(chunk for chunk in chunks if "第 2 条" in chunk.text)
    assert clause.section == "退货与换货政策 > 2 条"
    # 同一个「第 2 条」在别的文档里也存在，父级章节是区分它们的依据
    assert " > " in clause.section


def test_short_heading_merges_forward_into_its_first_clause(policy_document):
    """坑 1 回归：`## 一、无理由退货`（短标题）必须并入**它的正文**，不能单独成块。

    单独成块的后果：那个向量里没有答案，却会占一个 top-k 名额、还要过一次 rerank。
    """
    chunks = chunk_document(policy_document, ChunkingConfig(strategy="policy"))
    first = next(chunk for chunk in chunks if "第 1 条" in chunk.text)
    assert first.text.startswith("## 一、无理由退货")


def test_heading_never_merges_into_the_previous_clause(policy_document):
    """坑 2 回归：标题**不允许向后**粘到上一段条款的结尾。

    实测症状：`## 三、换货规则` 被并进「第 2 条」的 chunk 里，
    于是检索「第 2 条」会把第三章标题一起捞出来（两个无关章节搅在一起）。
    """
    chunks = chunk_document(policy_document, ChunkingConfig(strategy="policy"))
    for chunk in chunks:
        if chunk.text.startswith("第 "):
            markdown_heads = [
                line for line in chunk.text.split("\n") if line.startswith("#")
            ]
            # 开头允许带一个自己的上级标题（forward merge），但正文里不许再出现别的标题
            assert len(markdown_heads) <= 1, f"条款块里混进了额外标题：{chunk.text!r}"


def test_chunks_are_not_duplicated(policy_document):
    """没有重复内容（重叠窗口只用于超长单元，普通段落不该重复）。"""
    chunks = chunk_document(policy_document, ChunkingConfig(strategy="policy"))
    texts = [chunk.text for chunk in chunks]
    assert len(texts) == len(set(texts))


# ── FAQ 类：问 + 答一块 ─────────────────────────────────────────────────────


def test_faq_question_and_answer_stay_together(faq_document):
    """坑 3 回归：FAQ 的「问」与「答」必须在**同一块**里。

    实测症状（两次）：`### 如何修改收货地址？` 与它的答案被切成两块
    —— 检索命中标题却拿不到答案，是这类文档最没用的一种结果。
    根因有两个，都必须一起修：① `？` 结尾的行被 `numbered` 判据误判成编号标题；
    ② 正则没开 `MULTILINE`，多行单元（合并后的问答）一条标题判据都命中不了。
    """
    chunks = chunk_document(faq_document, ChunkingConfig(strategy="faq"))
    qa = [chunk for chunk in chunks if "收货地址" in chunk.text]
    assert len(qa) == 1
    assert "订单详情页" in qa[0].text  # 答案在同一块里
    assert qa[0].section.endswith("如何修改收货地址？")  # 问句就是它的章节


def test_faq_answers_stay_with_their_own_question(faq_document):
    """两个问答各自成块、且答案不串台（「修改地址」的块里不能出现发票的答案）。

    ⚠️ 若两条问答都很短（本例），它们**可以**被合并成一块 —— 那是 `min_chars`
    的碎片合并策略（避免 20 字的 chunk 污染索引），前提是同一章节：
    这里两条问句的章节不同，所以不合并。这条断言正是在钉住「章节边界阻止了乱合并」。
    """
    chunks = chunk_document(faq_document, ChunkingConfig(strategy="faq"))
    address = [chunk for chunk in chunks if "收货地址" in chunk.text]
    invoice = [chunk for chunk in chunks if "发票" in chunk.text]
    assert len(address) == 1 and len(invoice) == 1
    assert "发票" not in address[0].text
    assert "收货地址" not in invoice[0].text
    assert set(address[0].section.split(" > ")) & {"如何修改收货地址？"}
    assert set(invoice[0].section.split(" > ")) & {"能否开具增值税专用发票？"}


# ── 超长与边界 ──────────────────────────────────────────────────────────────


def test_oversized_paragraph_is_split_with_overlap():
    """超长段落用**重叠窗口**切：不能把答案切在缝上。"""
    long_text = "。".join(f"第 {index} 段的内容说明文字" for index in range(200))
    document = ParsedDocument(source="long.txt", text=long_text, kind="text")
    config = ChunkingConfig(strategy="manual", max_chars=200, overlap=40)
    chunks = chunk_document(document, config)
    assert len(chunks) > 3
    assert all(len(chunk.text) <= 200 for chunk in chunks)
    # 相邻窗口必须有重叠（否则就是硬切，答案可能被切断）
    joined = "\n".join(chunk.text for chunk in chunks)
    assert len(joined) > len(long_text)


def test_overlap_must_be_smaller_than_max_chars():
    """`overlap >= max_chars` 会让窗口不前进（死循环），所以在配置层就拦住。"""
    with pytest.raises(ValueError, match="overlap"):
        ChunkingConfig(max_chars=100, overlap=100)


def test_tiny_document_still_produces_one_chunk():
    document = ParsedDocument(source="t.txt", text="只有一句话。", kind="text")
    chunks = chunk_document(document)
    assert len(chunks) == 1
    assert chunks[0].text == "只有一句话。"


def test_blank_document_produces_no_chunks():
    document = ParsedDocument(source="t.txt", text="   \n\n  ", kind="text")
    assert chunk_document(document) == []


def test_ordinals_are_contiguous_after_merging(policy_document):
    """合并之后 `ordinal` 必须连续，否则「第 N 块」的说法会与库里对不上。"""
    chunks = chunk_document(policy_document, ChunkingConfig(strategy="policy"))
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


# ── 页码与 ID ───────────────────────────────────────────────────────────────


def test_page_map_maps_offsets_to_pages():
    """两页文本：第二页开头的偏移应该落在第 2 页。"""
    first = "第一页的内容"
    second = "第二页的内容"
    text = f"{first}{PAGE_BREAK}{second}"
    page_map = PageMap(text, pages=2)
    assert page_map.page_of(0) == 1
    assert page_map.page_of(len(first) + 1) == 2


def test_page_map_without_pages_returns_none():
    assert PageMap("没有分页的文本", pages=0).page_of(0) is None


def test_chunk_id_is_content_based_not_position_based():
    """内容型 ID：同一段文字挪了位置仍是同一个 `chunk_id`（增量更新才不会全量重嵌）。"""
    text = "第 1 条：自签收之日起 7 天内可无理由退货。"
    assert chunk_id_for(text) == chunk_id_for(text)
    assert chunk_id_for(text) != chunk_id_for(text + "（补充）")
    assert content_hash(text) == content_hash(text)


def test_content_hash_is_stable_across_processes():
    """用 sha256 而不是 `hash()`：Python 的字符串 hash 每个进程都不同，
    存库后下次启动就对不上 → 增量更新会误判「全变了」。"""
    assert content_hash("abc") == "ba7816bf8f01cfea"
