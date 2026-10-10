"""`textkit` 的测试：中文切词、n-gram 词表、查询词分组（关键词腿的底座）。

这些用例全离线（纯字符串处理）。它们存在的理由：关键词腿的**召回率** 100% 取决于
切词是否与建库时一致 —— 两边不一致的症状是「明明库里有的词，就是搜不到」，
而这类问题在检索结果上看不出原因（看起来像「模型不行」）。
"""

from __future__ import annotations

import pytest

from app.rag.textkit import (
    MAX_QUERY_CHARS,
    approx_tokens,
    index_text,
    index_tokens,
    is_cjk,
    like_terms,
    query_terms,
    terms,
)


def test_ascii_tokens_are_lowercased_and_kept_whole():
    """ASCII 词元原样保留（小写化）：型号、编号、日期都靠它精确匹配。

    `bge-m3` 是一个整体（词元定义允许内部出现 `-`/`.`/`_`），因为拆开之后
    「bge」与「m3」都不再是用户会打的东西，反而让精确匹配失效。
    """
    assert terms("BGE-M3 是 1024 维") == ["bge-m3", "是", "1024", "维"]


def test_cjk_gets_unigrams_and_bigrams():
    """汉字切出「单字 + 相邻二字」：单字保召回，二字保精度。"""
    tokens = terms("退货")
    assert tokens == ["退", "退货", "货"]


def test_index_text_is_space_separated_for_to_tsvector():
    """入库词表是空格分隔串 —— `to_tsvector('simple', ...)` 按空白切，正好复用它的 tokenizer。"""
    text = index_text("七天无理由退货")
    assert " " in text
    assert "无理" in text.split()
    assert all(" " not in token for token in text.split())


def test_single_char_query_still_produces_terms():
    """单字查询（`货`）不产生 bigram，但必须仍有词元 —— 否则这类查询直接零召回。"""
    primary, fallback = query_terms("货")
    assert primary == []  # 单字不进候选门（中文里几乎人人都有这个字）
    assert fallback == ["货"]  # 但兜底词里有它
    assert index_tokens("货") == ["货"]


def test_query_terms_split_primary_and_fallback():
    """候选词 = ASCII 词 + CJK bigram；兜底词 = 全部词元（含单字）。"""
    primary, fallback = query_terms("7天无理由退货")
    assert primary == ["7", "天无", "无理", "理由", "由退", "退货"]
    assert "无" in fallback and "无" not in primary
    # 兜底词必须包含候选词（否则「主词表为空时退到兜底」会丢召回）
    assert set(primary) <= set(fallback)


def test_terms_dedupe_keeps_first_occurrence_order():
    """去重保序：`_assign_sections` 与「截断尾部」都依赖顺序，用 set 会打乱它。

    `退货退货` 的 bigram 里有跨重复边界的 `货退` —— 这是 n-gram 的预期行为
    （它没有分词边界的概念），在检索里表现为「多一个几乎不命中的词元」，无害。
    """
    assert terms("退货退货") == ["退", "退货", "货", "货退"]
    assert len(terms("退货退货")) == len(set(terms("退货退货")))


def test_long_query_is_truncated():
    """超长查询被截断（避免 tsquery 长出几百个参数）。"""
    long_query = "退货" * 300
    assert len(terms(long_query)) <= 64
    assert len(like_terms(long_query)) <= 64
    # 截断按字符数先做（MAX_QUERY_CHARS），所以这里不会碰到 64 的上限
    assert len(long_query[:MAX_QUERY_CHARS]) >= MAX_QUERY_CHARS


def test_like_terms_are_raw_fragments_of_length_at_least_two():
    """`LIKE` 兜底要的是**原文片段**（不是切好的词）：长度 >= 2 才有区分度。"""
    assert like_terms("bge-m3 维度") == ["bge-m3", "维度"]


@pytest.mark.parametrize(
    ("char", "expected"),
    [("中", True), ("㐀", True), ("A", False), ("7", False), ("，", False)],
)
def test_is_cjk(char, expected):
    assert is_cjk(char) is expected


def test_approx_tokens_counts_cjk_and_words():
    """近似 token 口径：汉字 1 token/字 + ASCII 1 token/词（同一函数前后对比即可比）。

    ⚠️ 它是**近似**：`bge-m3` 在真 tokenizer 里可能碎成 3~4 个 BPE，
    这里只算 1 个 —— 所以本文只用它做「同一口径的前后对比」（E2 的 token 下降），
    不拿它当账单依据。
    """
    assert approx_tokens("退货政策") == 4
    assert approx_tokens("bge-m3 模型") == 1 + 2
    assert approx_tokens("") == 0


def test_punctuation_and_whitespace_are_separators():
    """标点是分隔符（不做停用词表）：`第 3 条：自签收` 里的「：」不该变成词元。"""
    tokens = terms("第 3 条：自签收")
    assert "：" not in tokens
    assert "自签" in tokens
