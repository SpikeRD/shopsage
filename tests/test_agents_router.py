"""Router Agent 的用例：分流、权限注入、保守兜底（`7.agents_module.md` §7 头两条）。

对应文档用例：
    test_router_routes_to_data()   # "查我的订单" → route=data, role=customer
    test_router_routes_to_rag()    # "退货政策是什么" → route=rag
"""

from __future__ import annotations

import logging

from app.agents.router import (
    FALLBACK_ROUTE,
    ROUTER_EXAMPLES,
    build_router_messages,
    classify,
    router_node,
)
from app.agents.state import GraphState
from app.security.rbac import PERM_READ_ALL_ORDERS, PERM_READ_OWN
from tests.agents_helpers import FakeLLM, internal_state, state_of


def _reply(route: str, certainty: float = 0.9, reason: str = "test") -> dict[str, object]:
    return {"route": route, "certainty": certainty, "reason": reason}


async def test_router_routes_to_data() -> None:
    """文档用例 1：查自己的订单 → data，且对客身份被注入 state。"""
    state = state_of("我最近的三笔订单是什么状态？")
    llm = FakeLLM(_reply("data"))

    out = await router_node(state, llm=llm)

    assert out["route"] == "data"
    assert out["route_confidence"] == "high"
    assert out["role"] == "customer"
    assert out["is_customer"] is True
    # 权限是**查表得来的**，不是模型说的：对客角色只有 read:own + write:ticket
    assert PERM_READ_OWN in out["permissions"]
    assert PERM_READ_ALL_ORDERS not in out["permissions"]
    # 且模型的输出里根本没有权限字段——即使它想给也给不了
    assert "permissions" not in llm.calls[0][-1]["content"]


async def test_router_routes_to_rag() -> None:
    """文档用例 2：政策类问题 → rag（即使问法里带业务名词）。"""
    llm = FakeLLM(_reply("rag"))
    out = await router_node(state_of("退货政策是什么？七天无理由怎么算的？"), llm=llm)
    assert out["route"] == "rag"


async def test_router_routes_to_analysis_only_for_internal() -> None:
    """analysis 是对内能力：内部员工走 analysis，对客请求降级为 data。"""
    internal = await router_node(
        internal_state("最近三个月各类目的销售占比"), llm=FakeLLM(_reply("analysis"))
    )
    assert internal["route"] == "analysis"
    assert internal["is_customer"] is False

    customer = await router_node(state_of("我的订单各类目占比"), llm=FakeLLM(_reply("analysis")))
    assert customer["route"] == "data"
    assert "analysis 仅对内部开放" in customer["route_reason"]


async def test_router_unknown_route_falls_back_conservatively(caplog) -> None:
    """模型给出未登记的取值（如 "sql"）→ 落到 rag，并**留下 WARNING**。

    兜底不能静默：静默兜底会把「路由准确率」这个指标悄悄拉高，
    第 16 步看到的数字就成了假的（见 runbook §9 坑 3）。
    """
    with caplog.at_level(logging.WARNING, logger="app.agents.router"):
        out = await router_node(state_of("帮我看看订单"), llm=FakeLLM(_reply("sql")))

    assert out["route"] == FALLBACK_ROUTE == "rag"
    assert out["route_confidence"] == "low"
    assert "无法识别" in out["route_reason"]
    assert any("未登记的路线" in r.getMessage() for r in caplog.records)


async def test_router_low_certainty_is_marked_but_keeps_route() -> None:
    """置信度低只**标记**，不改分支 —— 分支依据是取值域校验，不是自评分数。"""
    out = await router_node(state_of("嗯…那个"), llm=FakeLLM(_reply("data", certainty=0.2)))
    assert out["route"] == "data"
    assert out["route_confidence"] == "low"


async def test_router_without_user_message_goes_final() -> None:
    """没有 user 消息 → final（不查任何东西），且仍然注入身份。"""
    state: GraphState = {"messages": [{"role": "assistant", "content": "在的"}], "user_id": 3}
    out = await router_node(state, llm=FakeLLM(_reply("data")))
    assert out["route"] == "final"
    assert out["route_reason"] == "没有用户消息，无需检索"
    assert out["is_customer"] is True  # 缺 role 时按最保守的对客处理


async def test_router_prompt_contains_few_shot_and_scope() -> None:
    """few-shot 与身份范围必须真的进 prompt（否则分流质量无从谈起）。"""
    llm = FakeLLM(_reply("rag"))
    await router_node(state_of("退货政策是什么"), llm=llm)
    prompt = llm.all_prompts()

    for question, route in ROUTER_EXAMPLES:
        assert question in prompt, f"示例「{question}」没进 prompt"
        assert f"路线：{route}" in prompt
    assert "外部客户" in prompt
    assert "当前问题：退货政策是什么" in prompt


async def test_router_prompt_marks_internal_scope() -> None:
    llm = FakeLLM(_reply("analysis"))
    await router_node(internal_state("近三个月销售趋势"), llm=llm)
    assert "内部员工" in llm.last_prompt()


async def test_router_output_schema_is_json_documented() -> None:
    """输出契约必须写在 system prompt 里（结构化输出的前提）。"""
    messages = build_router_messages("x", role="customer", is_internal=False)
    system = messages[0]["content"]
    assert messages[0]["role"] == "system"
    assert '"route"' in system and '"certainty"' in system
    assert "只输出 JSON" in system


async def test_router_repairs_once_then_succeeds() -> None:
    """第一次给散文、第二次给 JSON：修复逻辑生效（这正是 §5 第三条要的能力）。"""
    llm = FakeLLM("我认为应该走 data 路线。", '{"route": "data", "certainty": 0.8, "reason": "修好了"}')
    out = await router_node(state_of("我的订单呢"), llm=llm)

    assert out["route"] == "data"
    assert llm.call_count == 2
    # 修复轮必须**回显模型的原文**并说明问题，而不是笼统「再试一次」
    repair_turn = llm.calls[1]
    assert repair_turn[-2] == {"role": "assistant", "content": "我认为应该走 data 路线。"}
    assert "无法被程序解析" in repair_turn[-1]["content"]
    assert "只**重新输出一个 JSON 对象" in repair_turn[-1]["content"]


async def test_classify_is_independent_of_state() -> None:
    """`classify()` 可单独调用（第 16 步评测直接用它，不必构造 state）。"""
    result = await classify("退货政策是什么", role="customer", llm=FakeLLM(_reply("rag")))
    assert result["route"] == "rag"
    assert set(result) == {"route", "certainty", "reason", "fell_back"}


def test_router_does_not_require_database() -> None:
    """router 必须能在没有数据库的环境里跑（否则「六个 Agent 独立可测」不成立）。

    做法：把 `app.db.session` 的模块级连接池换成「一用就炸」的替身，
    再跑一次 router —— 它不该碰数据库。
    """
    import app.agents.router as router_mod

    assert not hasattr(router_mod, "execute_readonly_query")
    assert not hasattr(router_mod, "get_read_engine")


async def test_router_unknown_role_gets_zero_permissions() -> None:
    """未登记角色 → **零权限**，不抛异常。

    「角色名写错」应该表现为「什么都看不到」，而不是在 router 里 500 ——
    后者会让一个配置错误看起来像系统故障，排查方向从一开始就错了。
    """
    out = await router_node(
        state_of("你好", role="role-that-does-not-exist"), llm=FakeLLM(_reply("final"))
    )
    assert out["permissions"] == frozenset()
    assert out["is_customer"] is False  # 未登记角色不是 customer，但也没有任何对内权限
    assert out["role"] == "role-that-does-not-exist"
