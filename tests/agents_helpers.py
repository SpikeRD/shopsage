"""Agent 测试的公共装置：假 LLM、state 构造、按名取回执。

为什么把假 LLM 写成「一个有状态的 callable」而不是 `unittest.mock.MagicMock`：
Agent 的契约是「按 prompt 产出结构化结果」，所以测试里最该断言的东西是
**「它收到了什么 prompt」+「它按顺序回什么」**。
MagicMock 能表达同样的意思，但要写一堆 `mock.call_args_list[0].args[0][-1]`；
一个显式的记录列表读起来就是测试意图本身（与 `tests/tools_helpers.py` 同一取舍）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.agents.state import GraphState, inject_identity, permissions_of


class FakeLLM:
    """按顺序返回预设结果的假 LLM。

    * `replies` 里可以是 `dict`（直接当结构化结果）或 `str`（走真实的 JSON 解析路径）。
    * 用完之后重复返回**最后一个**回执（而不是抛 StopIteration）：
      绝大多数用例只关心「前几次怎么回」，让最后一条粘住能让用例少写一长串重复。
      要断言「一共调用了几次」请用 `calls` 的长度。
    """

    def __init__(self, *replies: Mapping[str, Any] | str, name: str = "fake") -> None:
        self.replies: list[Mapping[str, Any] | str] = list(replies)
        self.calls: list[list[dict[str, Any]]] = []
        self.name = name

    def __call__(self, messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
        self.calls.append([dict(m) for m in messages])
        if not self.replies:
            raise AssertionError(f"{self.name} 假 LLM 没有任何回执，用例没准备好")
        if len(self.replies) == 1:
            return self.replies[0]
        return self.replies.pop(0)

    # ── 断言辅助 ──────────────────────────────────────────────────────────────

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def last_prompt(self) -> str:
        """最后一次调用的**全部消息文本**拼在一起（断言「prompt 里有没有 X」用）。"""
        return "\n".join(str(m.get("content", "")) for m in self.calls[-1])

    def all_prompts(self) -> str:
        return "\n---\n".join(
            "\n".join(str(m.get("content", "")) for m in call) for call in self.calls
        )


def schema_reply(sql: str, **params: Any) -> str:
    """造一条「LLM 返回的 SQL JSON 文本」（走真实的解析路径而不是绕过它）。"""
    return json.dumps({"sql": sql, "params": params, "reason": "test"}, ensure_ascii=False)


def state_of(
    text: str,
    *,
    user_id: int = 1,
    role: str = "customer",
    **extra: Any,
) -> GraphState:
    """造一个最小可用 state（含 router 注入的身份上下文）。

    默认带上 `permissions` / `is_customer`：**这不是为了让测试好看**，
    而是模拟第 8 步的真实 state —— 下游节点按契约消费这几个键，
    测试若不带就得让节点各自兜底，那就测不到真实路径了。
    """
    state: GraphState = {
        "messages": [{"role": "user", "content": text}],
        **inject_identity(user_id, role),
    }
    state.update(extra)  # type: ignore[typeddict-item]
    return state


def internal_state(text: str, *, user_id: int = 1, role: str = "internal_employee", **extra: Any) -> GraphState:
    """内部员工视角的 state（role 默认取 `read:all_orders` 的角色）。"""
    return state_of(text, user_id=user_id, role=role, **extra)


def assert_has_permission(state: GraphState, permission: str) -> None:
    assert permission in permissions_of(state), (
        f"造出来的 state 缺少权限 {permission}（role={state.get('role')}）："
        "用例的前提就不成立，别去改被测代码"
    )


def rows_of(pairs: Sequence[tuple[Any, ...]], columns: Sequence[str]) -> list[dict[str, Any]]:
    """按列名把元组列表转成行字典（写用例数据时比手写 dict 短且不易错位）。"""
    return [dict(zip(columns, pair, strict=True)) for pair in pairs]
