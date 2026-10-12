"""第 14 步（eval）的测试装置。

## 三条设计口径（与 `tests/cache/conftest.py` 同一套）

1. **默认零依赖**：本套件的用例**不需要**数据库、不需要密钥、不加载任何模型。
   假的检索器 + 假的判官足以驱动整条评测链路（`evaluate_rag` / `evaluate_agent` /
   `compare_single_vs_multi`）—— 因为评测的每一段都是**注入式**的
   （`search=` / `judge=` / `answerer=` / `graph=`，与第 7/10 步同一条纪律）。
2. **真依赖的用例显式开门**：`@pytest.mark.rag_live`（注册在根 conftest）才跑真检索，
   `@pytest.mark.llm_live` 才跑真判官。默认全 skip，理由与第 10/12/13 步完全一样：
   「没起依赖就红一片」会让本步的验收基线不可用。
3. **合成语料而不是真语料**：判据的用例用一段**写死的三句文档**，
   于是「该命中哪句」是可以逐字断言的 —— 用真语料断言等于把测试绑在语料上
   （改一份政策文档就会让一堆判据用例变红，而它们本来与被测代码无关）。

> ⚠️ `tests/eval/` 里的用例**不许** import `app.eval.run.build_default_searcher`
> 之类的生产装配函数：那会连库、加载 1GB 模型（本仓实测首次 36s）。
> 生产装配由 `--rag-live` 那一组单独覆盖。
"""

from __future__ import annotations

import pytest

from app.eval.dataset import EvalCase, EvalSet

#: 合成语料：三份「文档」，每份 2~3 句（句子边界清晰，便于逐字断言抽了哪句）
CORPUS: dict[str, list[str]] = {
    "docs/kb/退货政策.md": [
        "第 1 条：自签收之日起 7 天内，商品完好、不影响二次销售的，可申请无理由退货。",
        "第 2 条：无理由退货的运费由买家承担；因商品质量问题发起的退货，往返运费由本店承担。",
    ],
    "docs/kb/物流政策.md": [
        "2.1 单笔订单实付满 99 元免运费；未满 99 元收取 10 元运费。",
        "3.2 包裹外包装明显破损时，可拒收并联系客服，本店免费重发。",
    ],
    "docs/kb/会员积分.md": [
        "3.3 积分有效期为获得之日起 24 个月，过期自动清零，不可恢复。",
    ],
}

#: 问题 → 应该命中哪份文档（假的「检索器」按这个表工作，**不依赖任何模型**）
KEYWORDS: dict[str, str] = {
    "退货": "docs/kb/退货政策.md",
    "运费": "docs/kb/退货政策.md",
    "破损": "docs/kb/物流政策.md",
    "免运费": "docs/kb/物流政策.md",
    "积分": "docs/kb/会员积分.md",
}


@pytest.fixture
def corpus() -> dict[str, list[str]]:
    """合成语料（深拷贝一份，避免用例之间互相污染）。"""
    return {source: list(sentences) for source, sentences in CORPUS.items()}


@pytest.fixture
def fake_search(corpus: dict[str, list[str]]):
    """假的检索器：按 `KEYWORDS` 决定命中哪份文档，`documents` 参数可覆盖。

    返回的对象**故意长得像** `RetrievalResult`（有 `hits` / `elapsed_ms`，
    每条命中是 `{"text","source","score"}`）—— 评测层只依赖这个形状，
    所以形状对了就能跑（`rag_eval._score_row` 的说明里写了这条契约）。
    """

    def make(documents: dict[str, list[str]] | None = None, *, miss: bool = False):
        docs = documents if documents is not None else corpus

        def search(*, query: str, top_k: int = 5, with_rerank: bool | None = None):
            del with_rerank
            if miss:
                return type("Result", (), {"hits": [], "elapsed_ms": 1.0})()
            chosen: list[tuple[str, float]] = []
            for keyword, source in KEYWORDS.items():
                if keyword in query and source in docs:
                    chosen.append((source, 0.9 - 0.1 * len(chosen)))
            if not chosen:
                # 没命中任何关键词：退回第一份文档（模拟「检索总能返回点什么」）
                source = next(iter(docs))
                chosen.append((source, 0.5))
            hits = [
                {"text": "\n".join(docs[source]), "source": source, "score": score}
                for source, score in chosen[:top_k]
            ]
            return type("Result", (), {"hits": hits, "elapsed_ms": 2.0})()

        return search

    return make


def make_case(**overrides) -> EvalCase:
    """造一条用例（默认是一条能被上面那份合成语料答上的政策问题）。"""
    payload = {
        "id": "synthetic",
        "question": "无理由退货是几天内可以申请？",
        "reference": "自签收之日起 7 天内，商品完好、不影响二次销售的，可以申请无理由退货。",
        "key_facts": ("7 天", "签收"),
        "expected_sources": ("docs/kb/退货政策.md",),
    }
    payload.update(overrides)
    return EvalCase(**payload)  # type: ignore[arg-type]


def make_dataset(cases=(), *, name: str = "synthetic", version: str = "1", kind: str = "rag") -> EvalSet:
    """造一份数据集（默认一条用例）。"""
    items = tuple(cases) or (make_case(),)
    return EvalSet(name=name, version=version, kind=kind, description="测试用", cases=items)


@pytest.fixture
def case() -> EvalCase:
    return make_case()


@pytest.fixture
def dataset() -> EvalSet:
    return make_dataset()
