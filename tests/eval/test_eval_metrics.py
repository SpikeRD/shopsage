"""判据与三指标（`app/eval/text.py` + `app/eval/metrics.py`）—— 纯函数，零依赖。

这一组是 `14.eval.md` §7 里 `test_ragas_recall_threshold` 与
`test_faithfulness_no_hallucination` 的落点：两者都断言**阈值判据本身**，
而不是「跑一次真链路看分数」（后者是 `test_eval_live.py` 的事）。
"""

from __future__ import annotations

import pytest

from app.eval.metrics import (
    RECALL_NA,
    aggregate,
    answer_quality,
    passed_overall,
    recall_for,
    retrieval_quality,
)
from app.eval.text import (
    contains,
    coverage,
    is_emptyish,
    is_hedged,
    matching_key_facts,
    missing_key_facts,
    normalize_text,
    unsupported_numbers,
)
from tests.eval.conftest import make_case


# ── ① 文本归一（判据可复现的前提）─────────────────────────────────────────


@pytest.mark.parametrize(
    "left,right",
    [
        ("7 天", "7天"),
        ("7天", "七天"),
        ("99 元", "99元"),
        ("24 小时", "二十四小时"),
        ("支付宝、微信", "支付宝，微信"),
        ("ABC", "abc"),
        ("满 99 元免运费", "满99元免运费"),
    ],
)
def test_normalize_treats_equivalent_writings_as_equal(left: str, right: str):
    """同一件事的等价写法必须归一成同一个串（否则规则判分会误杀正确的答案）。"""
    assert normalize_text(left) == normalize_text(right)
    assert contains(left, right) and contains(right, left)


@pytest.mark.parametrize(
    "question,expected",
    [
        ("十五天", "15天"),
        ("二十个月", "20个月"),
        ("十天", "10天"),
    ],
)
def test_chinese_ten_compounds_are_expanded(question: str, expected: str):
    assert normalize_text(question) == expected


def test_coverage_and_matching_helpers_agree():
    facts = ("7 天", "签收", "质量问题")
    answer = "自签收之日起 7 天内可申请无理由退货。"
    assert matching_key_facts(answer, facts) == ("7 天", "签收")
    assert missing_key_facts(answer, facts) == ("质量问题",)
    assert coverage(answer, facts) == pytest.approx(2 / 3)
    assert coverage("", facts) == 0.0
    # 没有事实点时返回 1.0（这一维不加分不减分），而不是 0.0（那会把没标注的用例判死）
    assert coverage("任意", ()) == 1.0


def test_normalize_never_raises_on_weird_input():
    for value in ("", "   ", "。", "🙂", None):
        assert isinstance(normalize_text(value), str)  # type: ignore[arg-type]


# ── ② 拒答识别（本仓实测修正过的那条判据）────────────────────────────────


def test_short_apology_counts_as_hedged():
    assert is_hedged("抱歉，这个请求我无法处理。")
    assert is_hedged("我这边看不到，请联系人工客服。")
    assert is_emptyish("。。。")


def test_免责说明_inside_a_long_answer_is_not_a_refusal():
    """**本仓实测踩到的坑**：好答案里的一句「我这边看不到」曾被判成拒答。

    后果是「评测在惩罚正确的行为」——5 条质量最好的对客回答全成了失败样本
    （见 `text.is_hedged` 的 docstring 与 runbook §10.1 坑 1）。
    """
    answer = (
        "单笔订单实付满 99 元免运费，未满 99 元收取 10 元运费[1]。"
        "如果是金卡会员，包邮门槛降到 49 元；钻石会员全场包邮，还送退货运费券[2]。"
        "大家电、家具这类大件按体积计费，具体金额会在下单时展示[3]。"
        "另外您当前的会员等级与订单里有没有大件，我这边看不到，建议以结算页显示的运费为准。"
    )
    assert len(normalize_text(answer)) > 90, "这条用例的前提是「长回答」"
    assert not is_hedged(answer)


def test_refusal_at_the_beginning_of_a_short_reply_counts():
    assert is_hedged("我这边看不到，请联系人工客服协助处理。")


def test_empty_answer_is_emptyish_not_hedged():
    assert is_emptyish("")
    assert not is_hedged("")


# ── ③ 数值判据（Faithfulness 的机械信号）─────────────────────────────────


def test_unsupported_numbers_finds_fabricated_figures():
    context = "第 1 条：自签收之日起 7 天内，商品完好、不影响二次销售的，可申请无理由退货。"
    answer = "自签收之日起 15 天内可以申请无理由退货。"
    assert unsupported_numbers(answer, context) == ("15",)


def test_single_digit_numbers_are_not_treated_as_fabrication():
    """一位数字（引用编号 `[1]`、`第 1 条`）不进判据：它们遍地都是、假阳性极高。

    本仓实测：一句干净的寒暄（`你好！我是电商平台的客服助手`）曾被判成
    「材料里没有的数字：['1'] ← 编造信号」（见 runbook §10.1 坑 5）。
    """
    assert unsupported_numbers("你好！我是客服助手[1]。", "无关材料") == ()


def test_unsupported_numbers_keeps_order_and_deduplicates():
    assert unsupported_numbers("15 天，30 元，15 天", "材料里什么都没有") == ("15", "30")


# ── ④ Recall（含 `expect_empty` 的两条不同判据）──────────────────────────


def test_ragas_recall_threshold(case):
    """文档 §7 第二条：Recall 必须达到阈值。

    这里测的是**判据**：命中期望来源算满分、漏一份打折、全漏为 0。
    """
    hit = retrieval_quality(
        case=case, retrieved=["docs/kb/退货政策.md"], scores=[0.9], k=5
    )
    assert hit.recall == 1.0 and hit.missing == ()

    partial = make_case(expected_sources=("docs/kb/退货政策.md", "docs/kb/常见问题.md"))
    half = retrieval_quality(case=partial, retrieved=["退货政策.md"], scores=[0.9], k=5)
    assert half.recall == pytest.approx(0.5)
    assert half.missing == ("常见问题",)

    miss = retrieval_quality(case=case, retrieved=["docs/kb/会员积分.md"], scores=[0.4], k=5)
    assert miss.recall == 0.0 and miss.missing == ("退货政策",)


def test_source_matching_ignores_directory_and_extension():
    """`docs/kb/退货政策.md` 与 `退货政策` 是同一份文档（期望来源可以写得短）。"""
    case = make_case(expected_sources=("退货政策",))
    hit = retrieval_quality(case=case, retrieved=["docs/kb/退货政策.md"], k=5)
    assert hit.recall == 1.0


def test_expect_empty_case_requires_no_hits():
    """`expect_empty` 的判据是「有没有静默答上来」，而不是命中率。"""
    case = make_case(expected_sources=(), expect_empty=True)
    clean = retrieval_quality(case=case, retrieved=[], k=5)
    assert clean.recall == 1.0 and clean.empty_ok

    noisy = retrieval_quality(case=case, retrieved=["docs/kb/物流政策.md"], k=5)
    assert noisy.recall == 0.0 and not noisy.empty_ok


def test_recall_not_applicable_sentinel_is_excluded_from_aggregate():
    """不适用的 Recall 用哨兵值表达，**不参与平均**（本仓实测修正过的一条）。

    不排除的后果：agent 套件里 6 条 `expect_empty` 用例把 Recall 从 0.90 拉到 0.36，
    报告看起来像「检索坏了」（见 runbook §7.3）。
    """
    case = make_case(expected_sources=(), expect_empty=True)
    quality = retrieval_quality(case=case, retrieved=[], k=5)
    assert recall_for(case, quality) != recall_for(case, quality)  # nan

    agg = aggregate(
        "全部",
        [(0.9, 0.9, 0.9), (RECALL_NA, 0.9, 0.9)],
        thresholds={"recall": 0.7, "faithfulness": 0.7, "answer_relevancy": 0.6},
    )
    assert agg.count == 2
    assert agg.recall == pytest.approx(0.9), "不适用的一条不参与 Recall 平均"
    assert agg.extra["recall_applicable"] == 1


# ── ⑤ Faithfulness 与 Relevance（含「不许编造」的判据）───────────────────


def test_faithfulness_no_hallucination(case):
    """文档 §7 第三条：Faithfulness 必须达到阈值（即「答案忠于材料」）。

    三份答案对照：逐字引自材料（满分）／改了一个数字（掉分）／凭空编条款（掉到底）。
    """
    context = (
        "第 1 条：自签收之日起 7 天内，商品完好、不影响二次销售的，可申请无理由退货。\n"
        "第 2 条：无理由退货的运费由买家承担。"
    )
    faithful = answer_quality(
        case=case,
        answer="自签收之日起 7 天内、商品完好不影响二次销售就可以申请无理由退货，运费由买家承担。",
        context=context,
    )
    assert faithful.faithfulness == pytest.approx(1.0)
    assert faithful.missing_facts == ()

    wrong_number = answer_quality(
        case=case,
        answer="自签收之日起 15 天内、商品完好就可以申请无理由退货。",
        context=context,
    )
    assert wrong_number.faithfulness < faithful.faithfulness
    assert "15" in wrong_number.unsupported_numbers

    fabricated = answer_quality(
        case=case,
        answer="根据第 9 条，签收后 30 天内可退货，运费由平台承担。",
        context=context,
    )
    assert fabricated.faithfulness < 0.7
    assert fabricated.missing_facts  # 事实点没命中


def test_faithfulness_penalises_refusal_but_not_to_zero(case):
    """拒答**不是幻觉**，但它也没答问题：折半而不是清零（清零会惩罚正确的「如实说不知道」）。"""
    context = "第 1 条：自签收之日起 7 天内可申请无理由退货。"
    refusal = answer_quality(case=case, answer="抱歉，我这边看不到。", context=context)
    assert 0.0 < refusal.faithfulness < 0.5
    assert refusal.hedge and refusal.relevance == 0.0


def test_empty_answer_scores_zero(case):
    quality = answer_quality(case=case, answer="   ", context="任意材料")
    assert quality.faithfulness == 0.0 and quality.relevance == 0.0
    assert not quality.answered


def test_relevance_rewards_answering_the_question(case):
    context = "第 2 条：无理由退货的运费由买家承担；因商品质量问题发起的退货，往返运费由本店承担。"
    on_topic = answer_quality(
        case=make_case(question="无理由退货的运费谁承担？", key_facts=("由买家承担",), expected_sources=("s",)),
        answer="无理由退货的运费由买家承担，质量问题导致的退货则由本店承担往返运费。",
        context=context,
    )
    off_topic = answer_quality(
        case=make_case(question="无理由退货的运费谁承担？", key_facts=("由买家承担",), expected_sources=("s",)),
        answer="我们的客服工作时间是每天 9 点到 21 点，欢迎随时联系我们，我们会尽快处理您的问题。",
        context=context,
    )
    assert on_topic.relevance > off_topic.relevance


def test_short_answer_is_discounted(case):
    """一句话回答一个需要条件的政策问题：相关性按篇幅折扣（不是清零）。"""
    long_answer = answer_quality(
        case=case,
        answer="自签收之日起 7 天内、商品完好且不影响二次销售的可申请无理由退货，退回运费由买家承担。",
        context="7 天 签收",
    )
    short_answer = answer_quality(case=case, answer="7 天。", context="7 天")
    assert 0.0 < short_answer.relevance < long_answer.relevance


def test_findings_are_self_explanatory(case):
    """判据明细必须带人话理由：只给布尔值时，掉分要复跑才知道为什么。"""
    quality = answer_quality(case=case, answer="不知道。", context="材料")
    names = [name for name, _, _ in quality.findings]
    assert {"answered", "not_hedged", "key_facts", "numbers_supported", "faithful", "relevant"} <= set(names)
    assert all(detail for _, _, detail in quality.findings)


# ── ⑥ 聚合与整体判定 ─────────────────────────────────────────────────────


def test_aggregate_counts_passes_per_metric():
    thresholds = {"recall": 0.7, "faithfulness": 0.7, "answer_relevancy": 0.6}
    agg = aggregate("全部", [(1.0, 0.8, 0.9), (0.5, 0.6, 0.7)], thresholds=thresholds)
    assert agg.count == 2
    assert agg.passed == {"recall": 1, "faithfulness": 1, "answer_relevancy": 2}
    assert agg.recall == pytest.approx(0.75)


def test_passed_overall_requires_every_metric_above_threshold():
    thresholds = {"recall": 0.7, "faithfulness": 0.7, "answer_relevancy": 0.6}
    good = aggregate("全部", [(1.0, 1.0, 1.0)], thresholds=thresholds)
    bad = aggregate("全部", [(1.0, 0.2, 1.0)], thresholds=thresholds)
    assert passed_overall(good, thresholds=thresholds)
    assert not passed_overall(bad, thresholds=thresholds)


def test_passed_overall_rejects_an_empty_run():
    """**一条都没跑**判为不通过：这是 CI 里最容易发生的假绿（路径写错、`--only` 挑空）。"""
    thresholds = {"recall": 0.0, "faithfulness": 0.0, "answer_relevancy": 0.0}
    empty = aggregate("全部", [], thresholds=thresholds)
    assert empty.count == 0
    assert not passed_overall(empty, thresholds=thresholds)
