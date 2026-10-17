"""缓存相关的两条防回归用例（issue 009 / 010）。

这两条都**不是**「逻辑写错了」，而是「接线漏了」——所以它们的判据必须落在
「护栏看到了什么」「版本号有没有动」这种**外部可观察的事实**上，
而不是「函数被调了几次」。
"""

from __future__ import annotations

import pytest

from app.agents.guardrail import _materials_digest


def _state(question: str = "退货政策 7天无理由退货") -> dict:
    return {
        "messages": [{"role": "user", "content": question}],
        "user_id": 2,
        "role": "customer",
        "docs": [],
        "rows": [],
        "citations": [
            "docs/kb/退货政策.md #c00bcbd2da0b2ebe6",
            "docs/kb/退货政策.md #c7e2d414f92714088",
        ],
        "refusals": [],
        "answer": "自签收之日起 7 天内可申请无理由退货[1]，运费由买家承担[2]。",
    }


def test_digest_tells_the_judge_when_an_answer_came_from_the_semantic_cache() -> None:
    """★缓存命中时必须告诉判定者「这是缓存里的真答案」，否则每一次命中都被判成编造。

    ## 这条用例防的是一个实测踩到的假阳性（issue 009）

    `rag_node` 命中语义缓存时返回 `docs: []`（缓存的是**整段答案**，不是检索结果），
    于是材料摘要里恒为「知识库片段：0 条」；而 `synthesis` 复用的缓存答案
    带着上一轮真检索生成的 `[1]`~`[5]`。判定器看到「材料 0 条 + 满篇条款引用」，
    判定「编造」在它那边**完全正确** —— 是我们漏喂了事实。

    实测症状：同一句连问两次，第 1 次 `delivered`、第 2 次起 `blocked`，
    而 `citations` **仍然非空** —— 所以「只看出处」的验收永远抓不到它
    （见 `docs/runbook/16-web-runbook.md` §6.2）。
    """
    state = _state()
    state["cache_info"] = {"outcome": "hit", "similarity": 1.0, "cached_answer_used": True}

    digest = _materials_digest(state)

    assert "缓存" in digest, "命中缓存却不告诉判定者：它只能看到「知识库 0 条」，于是判编造"
    # 出处条数必须与 state 里的一致（判定者靠它核对 [n] 引用是不是真的存在）
    assert "出处共 2 条" in digest
    assert "退货政策.md" in digest
    # 「知识库片段：0 条」这一行仍然要在（它描述的是「本次没有重新检索」这个事实，
    # 不能因为写了缓存就把基线行删掉 —— 那会让未命中与命中两种轮次的摘要无法对比）
    assert "知识库片段：0 条" in digest


def test_digest_does_not_mention_cache_when_nothing_was_cached() -> None:
    """没有缓存命中时**不许**出现「缓存」二字（否则判定者会以为答案来路不明）。"""
    state = _state()
    state["docs"] = [object()]  # 正常检索到片段
    state["cache_info"] = {"outcome": "miss"}

    digest = _materials_digest(state)

    assert "缓存" not in digest


def test_digest_accepts_either_cache_signal() -> None:
    """两个判据都要认：`cached_answer_used`（synthesis 写）与 `outcome=hit`（rag 写）。

    只认一个就会在「上游把另一个键覆盖掉」时静默失效 —— 而那种失效
    没有任何运行期表现（还是那句话：编造判定看起来总是很有道理）。
    """
    for info in (
        {"cached_answer_used": True},
        {"outcome": "hit"},
    ):
        state = _state()
        state["cache_info"] = info
        assert "缓存" in _materials_digest(state), f"判据漏了这个形状：{info}"


# ── issue 010：灌库必须推进知识库版本 ────────────────────────────────────────


def test_ingest_report_exposes_doc_changed() -> None:
    """`IngestReport.doc_changed` 必须存在。

    ⚠️ 这个属性曾经**不存在**，而 `ingest_document` 早就拿它当 `on_change` 的判据、
    且那句话当时在 `try` **之外** —— 于是任何调用方只要真的传了 `on_change=`，
    第一次入库就 `AttributeError`。结果不是「缓存偶尔不失效」，而是
    **这条接线根本没人敢接**：`load_knowledge_base` 一直没传 `on_change`，
    灌库从不推进 `corpus_revision`，旧缓存（含「检索回 0 条」那种）能讲一整周。
    """
    from app.rag.ingest import IngestReport

    changed = IngestReport(source="docs/kb/x.md", action="updated")
    unchanged = IngestReport(source="docs/kb/x.md", action="unchanged")
    deleted = IngestReport(source="docs/kb/x.md", action="deleted")

    assert changed.doc_changed is True
    assert unchanged.doc_changed is False
    # 下线也算变更（文档没了，缓存里引用它的答案同样要失效）
    assert deleted.doc_changed is True


def test_ingest_path_forwards_on_change() -> None:
    """`ingest_path` 必须把 `on_change` 透传给 `ingest_document`。

    灌库脚本走的是 `ingest_path`（整目录），它原先**没有**这个参数 ——
    于是即使补好了 `ingest_document` 的回调、脚本也传了，仍然会因为
    「这一层不接收」而静默丢掉（`TypeError` 还是最好的结局，静默丢掉更糟）。
    """
    import inspect

    from app.rag.ingest import delete_document, ingest_path

    for fn in (ingest_path, delete_document):
        params = inspect.signature(fn).parameters
        assert "on_change" in params, f"{fn.__name__} 丢掉了 on_change，灌库/下线时缓存不会失效"


def test_sync_worker_accepts_on_change() -> None:
    """定时同步是生产上**唯一会自动改知识库**的路径，它不接线等于每天都留旧缓存。"""
    import inspect

    from app.rag.sync import RagSyncWorker

    assert "on_change" in inspect.signature(RagSyncWorker.__init__).parameters


@pytest.mark.parametrize("action", ["created", "updated", "reindexed"])
def test_every_change_action_triggers_invalidation(action: str) -> None:
    """三种「真的变了」的 action 都要算变更；只有 `unchanged` 不算。"""
    from app.rag.ingest import IngestReport

    assert IngestReport(source="s", action=action).doc_changed is True
