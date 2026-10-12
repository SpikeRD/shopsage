"""判官（`app/eval/judge.py`）：三套实现 + 失败口径 + prompt 口径。

为什么判官要单独一组用例：它是评测里**唯一带主观性**的部件，
而主观部件最容易出的问题是「悄悄变了口径」（prompt 改了一行、`temperature` 从 0 变成默认）。
这一组把「判词的形状」「失败的处置」「三种模式的选择」都钉住。
"""

from __future__ import annotations

import pytest

from app.eval.judge import (
    JUDGE_SYSTEM,
    Judgment,
    build_judge,
    build_judge_messages,
    build_llm_judge,
    parse_judgment,
    rules_judge,
    strict_llm_judge,
)

CONTEXT = (
    "第 1 条：自签收之日起 7 天内，商品完好、不影响二次销售的，可申请无理由退货。\n"
    "第 2 条：无理由退货的运费由买家承担。"
)
QUESTION = "无理由退货是几天内可以申请？"
REFERENCE = "自签收之日起 7 天内，商品完好、不影响二次销售的，可以申请无理由退货。"
FACTS = ("7 天", "签收")


async def _judge(judge, answer: str, *, context: str = CONTEXT) -> Judgment:
    result = judge(
        question=QUESTION, reference=REFERENCE, context=context, answer=answer, key_facts=FACTS
    )
    return await result if hasattr(result, "__await__") else result


# ── ① 规则判官 ───────────────────────────────────────────────────────────


async def test_rules_judge_passes_a_faithful_answer():
    verdict = await _judge(rules_judge(), "自签收之日起 7 天内可申请无理由退货，运费由买家承担。")
    assert verdict.faithful and verdict.relevant
    assert verdict.judge == "rules" and not verdict.degraded


async def test_rules_judge_rejects_a_refusal_and_an_empty_answer():
    for answer in ("抱歉，我这边看不到。", "   "):
        verdict = await _judge(rules_judge(), answer)
        assert not verdict.faithful and not verdict.relevant


async def test_rules_judge_rejects_when_facts_are_missing():
    verdict = await _judge(rules_judge(), "商品完好就可以退货。")
    assert not verdict.faithful
    assert "事实点命中率 0.00" in verdict.reason


# ── ② 严格判官（离线演示用；口径与真判官一致）──────────────────────────


async def test_strict_judge_passes_extracted_material():
    verdict = await _judge(strict_llm_judge(), "自签收之日起 7 天内，商品完好、不影响二次销售的，可申请无理由退货。")
    assert verdict.faithful and verdict.relevant
    assert verdict.judge == "strict"


async def test_strict_judge_rejects_an_answer_without_material():
    """材料为空时凭空给事实：这是「模型没材料还硬答」的签名，必须判不通过。"""
    verdict = await _judge(
        strict_llm_judge(),
        answer="第 9 条规定签收后 30 天内可以退货，运费由平台承担。",
        context="",
    )
    assert not verdict.faithful


async def test_strict_judge_does_not_know_the_experiment_group():
    """判官**不许**读「这份答案来自哪个方案」：知道分组的判官不是判官。

    这条守的是对照实验的可信度（`compare.py` 的机制表）：判据里只允许出现
    question / reference / context / answer / key_facts 五个量。
    """
    import inspect

    signature = inspect.signature(strict_llm_judge())
    assert set(signature.parameters) == {"question", "reference", "context", "answer", "key_facts"}


# ── ③ 真模型判官的接线（用假 LLM，不花钱）────────────────────────────


async def test_llm_judge_parses_model_output_and_labels_itself():
    async def fake_llm(messages, **kwargs):
        assert kwargs["temperature"] == 0.0, "判分必须 temperature=0（否则同一份答案两次分数不同）"
        assert messages[0]["content"].startswith(JUDGE_SYSTEM[:20])
        return {"faithful": True, "relevant": False, "reason": "切题但漏了一条"}

    judge = build_llm_judge(llm=fake_llm, model="fake/model")
    verdict = await _judge(judge, "随便答一句")
    assert verdict.faithful and not verdict.relevant
    assert verdict.judge == "llm:fake/model"


async def test_llm_judge_marks_degraded_and_fails_closed_on_error():
    """判官不可用时**判为不通过 + degraded**，而不是静默放行。

    理由：评测的职责是「发现变差」，判官挂掉时如果放行，CI 会在最需要它的时候变成绿的
    （假绿比假红危险得多，见 `judge.build_llm_judge` 的说明）。
    """

    async def broken_llm(messages, **kwargs):
        raise RuntimeError("模型不可用")

    verdict = await _judge(build_llm_judge(llm=broken_llm), "任意答案")
    assert not verdict.faithful and not verdict.relevant
    assert verdict.degraded
    assert "判官不可用" in verdict.reason


def test_parse_judgment_is_strict_by_default():
    """缺字段 / 类型不对 → 判 False（判分器的失败方向必须是「让可疑的不通过」）。"""
    assert not parse_judgment({}, judge="x").faithful
    assert not parse_judgment({"faithful": "yes", "relevant": 1}, judge="x").relevant is False
    assert parse_judgment({"faithful": True, "relevant": True}, judge="x").faithful


def test_build_judge_maps_modes_and_rejects_unknown():
    assert build_judge("rules").__name__ == "judge"
    assert build_judge("strict")( question=QUESTION, reference=REFERENCE, context=CONTEXT, answer="x", key_facts=()) is not None
    assert build_judge("llm").__name__ == "judge"
    with pytest.raises(ValueError, match="未知的判官模式"):
        build_judge("magic")


# ── ④ 判分 prompt 的口径（纯函数，可逐字断言）──────────────────────────


def test_judge_prompt_contains_every_needed_field():
    messages = build_judge_messages(
        question=QUESTION, reference=REFERENCE, context=CONTEXT, answer="候选答案", key_facts=FACTS
    )
    body = messages[-1]["content"]
    assert QUESTION in body and "候选答案" in body
    assert "必须命中的事实点" in body and "材料" in body
    assert messages[0]["role"] == "system"


def test_judge_prompt_states_the_empty_material_case():
    """材料为空时必须**写出来**：判官不知道「没材料」与「材料没写这条」的区别时，
    会把「材料为空」当成「材料里没有这条」而误判。"""
    body = build_judge_messages(
        question=QUESTION, reference=REFERENCE, context="", answer="答", key_facts=()
    )[-1]["content"]
    assert "没有检索到任何材料" in body


def test_judge_prompt_truncates_long_inputs():
    """判分 prompt 有长度上限：把整篇文档塞进去既贵又会顶爆上下文。"""
    from app.eval.judge import MAX_ANSWER_CHARS

    body = build_judge_messages(
        question=QUESTION, reference=REFERENCE, context="材" * 9000, answer="答" * 9000, key_facts=()
    )[-1]["content"]
    assert len(body) < 9000
    assert "已截断" in body


def test_judgment_serialises_for_the_report():
    payload = Judgment(True, False, "reason", judge="rules").as_dict()
    assert payload == {"faithful": True, "relevant": False, "reason": "reason", "judge": "rules", "degraded": False}
