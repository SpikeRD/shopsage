"""单 Agent vs 多 Agent 对照（`app/eval/compare.py`）。

`14.eval.md` §7 的第四条用例是 `test_multi_agent_beats_single`：
「多 Agent 成功率 > 单 Agent 成功率」。

⚠️ 这条用例有一个陷阱必须写清楚：**用一个「偏心的假模型」让它通过是没有意义的**。
本仓的做法是让对照实验的两条路在**模型能力上完全相同**，只差一件事 ——
消息里有没有检索材料（见 `compare.py` 的机制表）。于是：

* 假 LLM 的行为是「**只据 prompt 里的材料作答**」——
  这是真模型的诚实抽象（模型不会凭空知道一份自建语料的条款）；
* 多 Agent 那条路把检索命中塞进 prompt → 假 LLM 能逐字引用 → 分数高；
* 单 Agent 那条路不给材料 → 假 LLM 只能凭「参数记忆」瞎猜 → 分数低。

差距因此来自**机制**（有没有检索），而不是来自「我们给两条路配了不同的模型」。
"""

from __future__ import annotations

import re

import pytest

from app.eval.compare import (
    MULTI_AGENT,
    SINGLE_AGENT,
    compare_single_vs_multi,
    run_multi_agent,
    run_single_agent,
)
from app.eval.dataset import EvalSet
from app.eval.settings import RagEvalSettings
from tests.eval.conftest import make_case, make_dataset


def material_faithful_llm(*, fallback: str = "我记得大概是 15 天吧，具体以客服为准。"):
    """一个「只据 prompt 里的材料作答」的假 LLM（两条路共用同一个）。

    它**不知道**自己是单 Agent 还是多 Agent，也不知道标准答案 ——
    它只做一件事：把 user 消息里**含数字的正文句**抄进 answer，抄不到就瞎猜。
    这正是真模型在「有材料 / 没材料」两种处境下的核心差别。

    ⚠️ 「排除出处行」这条过滤是必需的：材料块的每一段都以
    `[1]（出处：docs/kb/退货政策.md）` 开头，而它**也含数字**（片段编号）。
    不过滤时假 LLM 会把出处抄成答案，于是「多 Agent 一侧」拿到 0 分 ——
    那不是机制差异，是**假模型太笨**（本仓实测见 runbook §10.2）。
    """

    async def fake_llm(messages, **kwargs):
        del kwargs
        body = messages[-1]["content"]
        sentences = [item.strip() for item in re.split(r"[。\n]+", body) if item.strip()]
        candidates = [
            item
            for item in sentences
            if re.search(r"\d", item)
            and "用户问题" not in item
            and not item.endswith("？")
            and not item.startswith("[")
            and "出处：" not in item
            and "要求：" not in item
            and "知识库片段" not in item
        ]
        # 取**最长**的那条：材料里的正文句比小标题长，而小标题往往也含数字（`第 2 条`）
        if candidates:
            best = max(candidates, key=len)
            return {"answer": best + "。", "cited": [1]}
        return {"answer": fallback, "cited": []}

    return fake_llm


async def test_multi_agent_beats_single(fake_search):
    """文档 §7 第四条：多 Agent 必须在质量维上胜过单 Agent。"""
    dataset: EvalSet = make_dataset(
        [
            make_case(
                id="return",
                key_facts=("7 天", "签收"),
                expected_sources=("docs/kb/退货政策.md",),
            ),
            make_case(
                id="shipping",
                question="满多少钱免运费？",
                reference="单笔订单实付满 99 元免运费；未满 99 元收取 10 元运费。",
                key_facts=("99 元", "10 元"),
                expected_sources=("docs/kb/物流政策.md",),
            ),
            make_case(
                id="points",
                question="积分有效期是多久？",
                reference="积分有效期为获得之日起 24 个月，过期自动清零。",
                key_facts=("24 个月",),
                expected_sources=("docs/kb/会员积分.md",),
            ),
        ]
    )
    comparison = await compare_single_vs_multi(
        dataset,
        search=fake_search(),
        settings=RagEvalSettings(suite="compare"),
        llm=material_faithful_llm(),
    )

    variants = {item.name: item for item in comparison.variants}
    multi = variants[MULTI_AGENT]
    single = variants[SINGLE_AGENT]
    assert multi.key_fact_hit_rate > single.key_fact_hit_rate
    assert multi.faithfulness > single.faithfulness
    assert comparison.delta["faithfulness"] > 0
    assert comparison.passed, comparison.verdict
    assert "多 Agent" in comparison.verdict


async def test_single_agent_gets_no_material(fake_search):
    """基线的定义就是「没有检索材料」：它的 Recall 因此是**定义上的 0**。"""
    case = make_case()
    answer, hits = await run_multi_agent(
        case, search=fake_search(), settings=RagEvalSettings(suite="compare")
    )
    assert hits, "多 Agent 一侧必须真的检索"
    assert body_of(answer), "多 Agent 一侧必须有答案"

    single_answer = await run_single_agent(case, llm=material_faithful_llm(fallback="不知道"))
    assert "7 天" not in single_answer, "基线不许凭空知道语料里的条款"


def body_of(text: str) -> str:
    return str(text).strip()


async def test_comparison_uses_the_same_cases_and_judge_for_both(fake_search):
    """两条路必须跑同一批用例、同一套判据：任何一处不同，差距就不再归因于机制。"""
    dataset = make_dataset([make_case(id="a"), make_case(id="b", question="满多少钱免运费？")])
    comparison = await compare_single_vs_multi(
        dataset,
        search=fake_search(),
        settings=RagEvalSettings(suite="compare"),
        llm=material_faithful_llm(),
    )
    counts = {item.name: item.count for item in comparison.variants}
    assert counts == {MULTI_AGENT: 2, SINGLE_AGENT: 2}


async def test_failed_gain_is_reported_not_hidden(fake_search):
    """增益不够时 `passed=False` 且理由写明（对照实验的结论不能被藏起来）。"""
    dataset = make_dataset([make_case(id="a")])
    # 把阈值抬到一个不可能达到的高度 → 必然不通过
    comparison = await compare_single_vs_multi(
        dataset,
        search=fake_search(),
        settings=RagEvalSettings(suite="compare", min_multi_agent_gain=2.0),
        llm=material_faithful_llm(),
    )
    assert not comparison.passed
    assert "未通过" in comparison.verdict
    assert comparison.as_dict()["delta"]["faithfulness"] > 0


def test_comparison_serialises_for_the_report():
    from app.eval.compare import Comparison, Variant

    comparison = Comparison(
        dataset_name="d",
        dataset_version="1",
        variants=[
            Variant(name=MULTI_AGENT, label="多 Agent", count=3, recall=1.0, faithfulness=0.9,
                    answer_relevancy=0.8, key_fact_hit_rate=1.0),
            Variant(name=SINGLE_AGENT, label="单 Agent", count=3, recall=0.0, faithfulness=0.2,
                    answer_relevancy=0.1, key_fact_hit_rate=0.0, refusals=3),
        ],
        delta={"faithfulness": 0.7},
        verdict="多 Agent 更好",
    )
    payload = comparison.as_dict()
    assert payload["variants"][0]["name"] == MULTI_AGENT
    assert payload["delta"]["faithfulness"] == pytest.approx(0.7)
    assert payload["variants"][1]["refusals"] == 3
