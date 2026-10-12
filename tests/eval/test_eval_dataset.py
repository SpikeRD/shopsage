"""`14.eval.md` §7 的 `test_golden_dataset_schema` + 加载期硬校验（`app/eval/dataset.py`）。

文档 §7 的用例名是 `test_golden_dataset_schema`（每条含 question / reference / key_facts）。
这里把「契约」与「加载期三道硬校验」都钉住 —— 它们比字段存在性更重要：
缺字段会 KeyError（明显），而**事实点与标准答案无关**这种错误不会让任何东西报错，
只会让 Faithfulness 永久偏低（见 `dataset.py` docstring）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.dataset import (
    DATASET_FILES,
    DatasetError,
    EvalCase,
    parse_case,
    parse_dataset,
)
from tests.eval.conftest import make_case

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── ① 仓库里那两份 golden set 本身 ────────────────────────────────────────


@pytest.mark.parametrize("suite", sorted(DATASET_FILES))
def test_golden_dataset_schema(suite: str):
    """每条用例含 `question` / `reference` / `key_facts`，且**能过加载期校验**。

    这是文档 §7 的第一条用例。它同时是「数据集进 Git 之后没有漂移」的哨兵：
    有人改了 JSON 却改坏了（少字段、事实点与标准答案不匹配），CI 立刻红。
    """
    from app.eval.dataset import load_dataset

    dataset = load_dataset(suite, root=REPO_ROOT)
    assert len(dataset) >= 10, f"{suite} 的用例太少（{len(dataset)} 条）：覆盖不足的评测没有信度"
    for item in dataset.cases:
        assert item.question.strip(), f"{item.id} 的 question 为空"
        assert item.reference.strip(), f"{item.id} 的 reference 为空"
        assert isinstance(item.key_facts, tuple), f"{item.id} 的 key_facts 必须是元组"
        assert item.expected_sources or item.expect_empty, (
            f"{item.id} 既没有期望来源也没声明 expect_empty"
        )


def test_golden_rag_dataset_covers_required_intents():
    """`14.eval.md` §4 第 1 条要求覆盖「订单/售后/产品/政策/内部报表」等意图。

    这条断言是**覆盖度**的哨兵：它不检查用例写得好不好，只保证「没有整类问题被漏掉」——
    漏一整类的后果是「评测全绿但线上有一类问题一直答错」，而报告上完全看不出来。
    """
    from app.eval.dataset import load_dataset

    rag = load_dataset("rag", root=REPO_ROOT)
    agent = load_dataset("agent", root=REPO_ROOT)

    rag_tags = {tag for item in rag.cases for tag in item.tags}
    for intent in ("退货", "退款", "物流", "保修", "积分", "发票", "账户安全", "企业采购"):
        assert any(intent in tag for tag in rag_tags), f"rag golden set 缺少「{intent}」类的用例"

    agent_routes = {item.expected_route for item in agent.cases}
    assert {"data", "rag", "analysis", "final"} <= agent_routes, (
        f"agent golden set 必须覆盖四个路由分支，实际只有 {sorted(agent_routes)}"
    )
    assert any(item.expect_empty for item in agent.cases), "agent golden set 缺少「知识库里没有」的对照"


def test_dataset_versions_are_versioned_and_named():
    """`14.eval.md` §5 第三条：评测数据要版本化（version 必填，名字要能进报告）。"""
    from app.eval.dataset import load_dataset

    for suite in DATASET_FILES:
        dataset = load_dataset(suite, root=REPO_ROOT)
        assert dataset.version and dataset.version[0].isdigit()
        assert dataset.source_path.endswith(".json")
        assert dataset.description, "数据集缺 description：报告里无法说明这份数据是什么"


# ── ② 加载期的三道硬校验 ──────────────────────────────────────────────────


def test_missing_required_field_is_rejected():
    with pytest.raises(DatasetError, match="缺少必填字段"):
        parse_case({"id": "x", "question": "q"}, where="测试")


def test_expected_sources_or_expect_empty_is_required():
    """没有期望来源、又没声明 `expect_empty`：加载就炸（Recall 没有分母）。"""
    with pytest.raises(DatasetError, match="expected_sources"):
        parse_case(
            {"id": "x", "question": "q", "reference": "r", "key_facts": []}, where="测试"
        )


def test_key_facts_must_appear_in_reference():
    """**最值钱的一条校验**：事实点与标准答案必须对得上。

    不对的形状是「标注时复制粘贴了另一条用例的事实点」——
    它不会让任何测试变红，只会让 Faithfulness 永久偏低，
    而人会先去怀疑检索（见 `dataset.py` docstring）。
    """
    with pytest.raises(DatasetError, match="不在 reference 里"):
        parse_case(
            {
                "id": "x",
                "question": "q",
                "reference": "自签收之日起 7 天内可申请无理由退货。",
                "key_facts": ["48 小时"],
                "expected_sources": ["docs/kb/退货政策.md"],
            },
            where="测试",
        )


def test_key_facts_number_writing_differences_are_tolerated():
    """`7 天` / `7天` / `七天` 是同一个事实：归一之后必须都算命中。

    这条守的是「标注风格差异不该让用例失效」—— 否则事实点的写法会变成隐性规范，
    而那种规范没人知道（直到某天加了一条用例被 loader 拒了）。
    """
    for writing in ("7 天", "7天", "七天"):
        case = parse_case(
            {
                "id": "x",
                "question": "q",
                "reference": "自签收之日起 7 天内可申请。",
                "key_facts": [writing],
                "expected_sources": ["docs/kb/退货政策.md"],
            },
            where="测试",
        )
        assert case.key_facts == (writing,)


def test_duplicate_ids_are_rejected():
    """重复 id 会让「按 id 挑用例」与报告里的对照表静默错位（后一条覆盖前一条）。"""
    payload = {
        "name": "dup",
        "version": "1",
        "kind": "rag",
        "cases": [
            {"id": "a", "question": "q1", "reference": "r1", "expected_sources": ["s"]},
            {"id": "a", "question": "q2", "reference": "r2", "expected_sources": ["s"]},
        ],
    }
    with pytest.raises(DatasetError, match="id 重复"):
        parse_dataset(payload)


def test_version_is_required():
    payload = {"name": "n", "kind": "rag", "cases": [{"id": "a", "question": "q", "reference": "r", "expected_sources": ["s"]}]}
    with pytest.raises(DatasetError, match="version"):
        parse_dataset(payload)


def test_unknown_kind_is_rejected():
    payload = {"name": "n", "version": "1", "kind": "sql", "cases": [{"id": "a", "question": "q", "reference": "r", "expected_sources": ["s"]}]}
    with pytest.raises(DatasetError, match="kind"):
        parse_dataset(payload)


def test_single_string_key_fact_is_not_split_into_characters():
    """一条事实点写成字符串时按**一整条**处理（`"7天"` 不是 `("7","天")`）。"""
    case = parse_case(
        {
            "id": "x",
            "question": "q",
            "reference": "7天",
            "key_facts": "7天",
            "expected_sources": ["s"],
        },
        where="测试",
    )
    assert case.key_facts == ("7天",)


# ── ③ 子集与路径解析 ─────────────────────────────────────────────────────


def test_subset_by_id_and_limit():
    dataset = parse_dataset(
        {
            "name": "n",
            "version": "1",
            "kind": "rag",
            "cases": [
                {"id": f"c{i}", "question": "q", "reference": "r", "expected_sources": ["s"]}
                for i in range(5)
            ],
        }
    )
    assert [case.id for case in dataset.subset(["c1", "c3"]).cases] == ["c1", "c3"]
    assert len(dataset.subset(limit=2)) == 2
    with pytest.raises(DatasetError, match="没有这些用例 id"):
        dataset.subset(["nope"])


def test_dataset_path_accepts_logical_name_and_explicit_path():
    from app.eval.dataset import dataset_path

    assert dataset_path("rag", root=REPO_ROOT).name == DATASET_FILES["rag"]
    explicit = "eval/datasets/rag_golden_v1.json"
    assert dataset_path(explicit, root=REPO_ROOT).name == "rag_golden_v1.json"


def test_dataset_json_is_utf8_and_readable_as_plain_json():
    """数据集必须是**纯 JSON**（不能有 BOM / 注释）：它要能被别的工具直接读。"""
    for filename in DATASET_FILES.values():
        raw = (REPO_ROOT / "eval" / "datasets" / filename).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{filename} 带 BOM（别的工具会读失败）"
        json.loads(raw.decode("utf-8"))


def test_make_case_helper_matches_contract():
    """装置里的 `make_case` 必须产出合法用例（否则所有用例都在测一个假前提）。"""
    case: EvalCase = make_case()
    assert case.expected_sources and case.key_facts
