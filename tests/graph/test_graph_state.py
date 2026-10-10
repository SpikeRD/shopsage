"""图侧状态契约的用例：**字段集与第 7 步逐字一致**，且每个键都带 reducer。

这一组防的是三件「错了也不报错」的事（都属于本步实测踩过的坑）：

1. **字段漏了一个**：`app/agents/state.GraphState` 加了键、`app/graph/state.GraphStateDoc`
   没加 —— 节点照写、图把它当隐式通道，`ainvoke` 的返回值里就没有它（§10.1 坑 5）。
2. **某个键忘了 reducer**：同上，静默丢失。所以这里**逐个键**断言带注解。
3. **`messages` 的两种形态混用**：`add_messages` 会把 dict 变成 `HumanMessage`；
   节点读的是 `message.get("role")` —— 不归一的症状是「第一轮正常、第二轮崩」（§10.1 坑 2）。

第 1 条用「两处字段集的对称差」来断言：这样无论是「agents 加了没同步」还是
「graph 多写了不存在的键」，都会立刻红，且报错里直接列出差异。
"""

from __future__ import annotations

from typing import Any, get_args, get_origin, get_type_hints

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage

from app.agents.state import GraphState as AgentGraphState
from app.graph.state import GraphState, GraphStateDoc, to_openai_messages

AGENT_KEYS = set(get_type_hints(AgentGraphState))
GRAPH_KEYS = set(get_type_hints(GraphStateDoc))


def test_field_sets_are_identical_on_both_sides() -> None:
    """两处声明的字段集必须**完全相等**（对称差为空）。"""
    only_in_agents = AGENT_KEYS - GRAPH_KEYS
    only_in_graph = GRAPH_KEYS - AGENT_KEYS
    assert not only_in_agents and not only_in_graph, (
        "图侧与 agent 侧的状态字段对不上：\n"
        f"  只在 app/agents/state.py：{sorted(only_in_agents)}\n"
        f"  只在 app/graph/state.py：{sorted(only_in_graph)}\n"
        "两边都要改（agents 是字段真源，graph 加 reducer）"
    )


def test_every_key_has_a_reducer_annotation() -> None:
    """**每个**键都要带 `Annotated[..., reducer]`（裸注解的键不进图的输出通道）。"""
    hints = get_type_hints(GraphStateDoc, include_extras=True)
    naked = [key for key, hint in hints.items() if get_origin(hint) is None]
    assert naked == [], (
        f"这些键没有 reducer 注解：{sorted(naked)} —— 它们不会出现在 `ainvoke` 的返回值里，"
        "而图跑起来完全正常（runbook §10.1 坑 5）"
    )


def test_messages_uses_add_messages_and_others_use_last_value() -> None:
    """`messages` 追加、其余**覆盖** —— 语义分工必须一字不差。"""
    from langgraph.channels import LastValue
    from langgraph.graph.message import add_messages

    hints = get_type_hints(GraphStateDoc, include_extras=True)
    assert add_messages in get_args(hints["messages"]), "messages 必须用 add_messages（追加）"
    for key, hint in hints.items():
        if key == "messages":
            continue
        assert LastValue in get_args(hint), f"{key} 的 reducer 不是 LastValue（本轮覆盖）"


def test_graph_state_alias_exposes_the_same_fields() -> None:
    """`from app.graph.state import GraphState` 拿到的类型与 schema 一致（§2 的 import 靠它）。"""
    assert set(get_type_hints(GraphState)) == GRAPH_KEYS


def test_to_openai_messages_normalises_objects_back_to_dicts() -> None:
    """归一函数：`BaseMessage` → dict，dict 原样，`RemoveMessage` 保留成对象。"""
    raw: list[Any] = [
        HumanMessage(content="问题"),
        AIMessage(content="回答"),
        SystemMessage(content="系统"),
        {"role": "user", "content": "已经是 dict"},
        RemoveMessage(id="x"),
    ]
    out = to_openai_messages(raw)

    assert out[0] == {"role": "user", "content": "问题"}
    assert out[1] == {"role": "assistant", "content": "回答"}
    assert out[2] == {"role": "system", "content": "系统"}
    assert out[3] == {"role": "user", "content": "已经是 dict"}
    assert isinstance(out[4], RemoveMessage), "删除指令不能被转成 dict（会被当成一条新消息）"


def test_to_openai_messages_tolerates_bad_input() -> None:
    """非列表输入返回空列表（而不是抛）—— 归一函数在节点入口，抛异常会误伤正常路径。"""
    assert to_openai_messages(None) == []
    assert to_openai_messages("不是列表") == []
    assert to_openai_messages([{"role": "user"}, "非法项", 42]) == [{"role": "user"}]


def test_node_names_are_unique_and_stable() -> None:
    """节点名常量不得重复；路由表的目标必须是已注册的节点名（或 synthesis）。"""
    from app.graph.state import (
        NODE_ANALYSIS,
        NODE_DATA,
        NODE_GATE,
        NODE_GUARDRAIL_INPUT,
        NODE_GUARDRAIL_OUTPUT,
        NODE_RAG,
        NODE_ROUTER,
        NODE_SYNTHESIS,
        ROUTE_NODES,
    )

    names = [
        NODE_GUARDRAIL_INPUT,
        NODE_ROUTER,
        NODE_DATA,
        NODE_RAG,
        NODE_ANALYSIS,
        NODE_SYNTHESIS,
        NODE_GUARDRAIL_OUTPUT,
        NODE_GATE,
    ]
    assert len(set(names)) == len(names), f"节点名有重复：{names}"
    assert set(ROUTE_NODES.values()) <= set(names)
