"""回合驱动（`run.py`）：**「断点重放」与「图级兜底」在这里同时成立**。

`8.graph.md` §4 第 5 条要的是两件事同时为真：

1. 节点失败后**能从断点重放**（不重头跑）；
2. 用户**看不到 500**（对客路径上一次 500 的代价远高于一句「这次没查到」）。

这两件事在图内部是**互相拉扯**的（本步实测踩到过，见 runbook §10.1 坑 8）：

* 若节点自己 `except` 并返回兜底增量，LangGraph 认为这一步**成功完成**，
  于是 `next` 变成空元组 —— 断点消失，重放无从谈起；
* 若让异常穿透，checkpoint 不推进、`next == (失败节点,)` —— 可重放，但异常会打到调用方脸上。

所以分工是这样的：

| 谁 | 负责 |
|---|---|
| 节点（`nodes.py`） | 什么都不兜，异常**穿透**（保住断点） |
| 本模块（`Turn`） | 接住异常 → 从断点重放 → 重放不成再把失败**写回 state** 兜底成一句实话 |

## 重放与重试的区别（本模块的语义边界）

* **重放**（本模块做的）：图从 checkpoint 里的 `next` 继续，**已完成的节点不再执行**。
  它修不了「代码有 bug」，但能修「进程挂了 / 上游抖动了一下」。
* **重试**（第 12 步队列做的）：把整个请求重新投一次，从头跑。
  第 6 步已经给可重试错误分过类（`RetryableToolError` / `RetryableLLMError`），
  本模块**不吞**它们：那类错误属于「等一下就会好」，交给队列的退避策略比在这里打转更对。

## 为什么默认只重放 1 次

「无限重放」显然不行：一个必然失败的节点会被反复执行，而那正是 C1（成本）最怕的形态。
默认 1 次（= 一共执行 2 次）与 `data` 节点的 `MAX_SQL_ATTEMPTS=3` 同源：
**能用具体报错改好的，第二次就改好了；改不好的，第三次也一样**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final

from langgraph.errors import GraphRecursionError

from app.graph.builder import graph_config
from app.graph.checkpointer import CheckpointUnavailable
from app.graph.nodes import failure_update
from app.tools.errors import RetryableToolError

logger = logging.getLogger(__name__)

#: 一轮最多重放多少次（理由见模块 docstring）。
DEFAULT_REPLAY_BUDGET: int = 1

#: LangGraph 把 `interrupt()` 的载荷放在这个保留键下（`stream_mode="updates"` 的一整块）。
#:
#: ⚠️ 它**不是** state 里的键 —— `aget_state().values` 里没有它（实测），
#: 载荷只出现在流的增量块与 `snapshot.tasks[*].interrupts` 里。
#: 所以「谁挂起了、挂起了什么」必须由驱动这一轮的人（本模块）从流里捞出来。
INTERRUPT_KEY: Final[str] = "__interrupt__"


class GraphTurnFailed(RuntimeError):
    """一轮的**永久性**失败（重放预算用尽）。

    单独一个类型的用途：调用方（第 9 步 API）要能区分「图跑完了，只是这次没查到」
    （`Turn.exhausted=False`）与「图没跑完，状态里已经写了兜底话术」。
    前者可以正常返回 200，后者需要看 `turn.blocked` 决定状态码。
    """


@dataclass
class Turn:
    """一次会话回合的执行结果（调用方只该用这个对象，不需要自己拼 config）。

    为什么要有这一层而不是让调用方直接 `ainvoke`：

    * `graph_config()` 里那个容易漏掉的 `durability="sync"` 由它统一带上；
    * **失败兜底**由它做（节点不能做，会把断点吃掉）；
    * `visited`（真正跑过的节点）与 `replayed`（重放过哪几次）是排障的第一现场，
      第 11 步的埋点也要它们。
    """

    thread_id: str
    state: dict[str, Any] = field(default_factory=dict)
    #: 真正被执行过的节点（按顺序）—— 来自 `stream_mode="updates"`，不是推断
    visited: list[str] = field(default_factory=list)
    #: 被重放过的节点（每次重放记一条）
    replayed: list[str] = field(default_factory=list)
    #: 重放预算用尽（这一轮是「没做到」而不是「答完了」）
    exhausted: bool = False
    #: 兜底写进 state 的那几个键（**压在最后合并**，理由见 `run_turn` 末尾的注释）
    fallback: dict[str, Any] | None = None
    #: 最后一次失败的异常（排障用；已经在 state 的 `refusals` 里留了话术）
    error: Exception | None = None
    #: 这一轮**挂起等人工批准**时的中断载荷（`{"question": ..., "interrupt_id": ...}`）。
    #:
    #: 为什么要在这一层记它：`interrupt()` 的载荷**不在** state 里
    #: （实测：`astream(stream_mode="updates")` 会单独吐一个 `{"__interrupt__": (Interrupt(...),)}` 块），
    #: 所以「谁挂起了、挂起了什么」只有跑这一轮的人看得见。第 9 步的 API 要把它翻成
    #: HTTP 202 + 一句给审批人看的问题；漏掉它的话用户得到的是一条「什么都没发生」的 200。
    interrupt: dict[str, Any] | None = None

    @property
    def answer(self) -> str:
        return str(self.state.get("answer") or "")

    @property
    def blocked(self) -> bool:
        return bool(self.state.get("blocked"))

    @property
    def pending_approval(self) -> bool:
        """这一轮是否停在人工审批闸门上（**不是**失败、也不是答完了）。"""
        return self.interrupt is not None

    def summary(self) -> str:
        """一行排障摘要（日志与演示脚本都用它，避免各处自己拼）。"""
        stages = "→".join(self.visited) or "(空)"
        tail = f" | 重放：{self.replayed}" if self.replayed else ""
        tail += " | 预算用尽" if self.exhausted else ""
        tail += " | 等人工批准" if self.pending_approval else ""
        return f"thread={self.thread_id} 路径={stages} 结论={'拦截' if self.blocked else '正常'}{tail}"


async def run_turn(
    graph: Any,
    state: dict[str, Any] | None,
    *,
    thread_id: str,
    replay_budget: int = DEFAULT_REPLAY_BUDGET,
    on_event: Any = None,
) -> Turn:
    """跑一轮：`state=None` 表示「从 checkpoint 继续」（重放的入口）。

    用 `astream(stream_mode="updates")` 而不是 `ainvoke`：它给出的增量里带**节点名**，
    于是「这一轮真正跑了哪几个节点」是**读出来的**，不是猜出来的
    （`ainvoke` 的返回值只告诉你状态变了，不告诉你是谁改的）。
    流的另一端由 `durability="sync"` 保证「读到即已落盘」。

    `on_event(node, update)` 是给演示脚本/第 9 步 SSE 的观察点：默认不传。
    """
    config = graph_config(thread_id)
    turn = Turn(thread_id=thread_id)
    payload: Any = state
    # 重放预算按「**真正重放了几次**」计，而不是按循环轮次 ——
    # 两者的区别在「兜底之后图还要继续跑几步」时是本质的：那几步不是重放
    # （它们是失败节点的下游，本来就要跑），不该吃掉重放预算。
    replays_left = max(0, int(replay_budget))
    executions_left = 20  # 循环护栏：正常一轮只有 3~7 步，超过这个数说明接线出了环

    while executions_left > 0:
        executions_left -= 1
        try:
            async for chunk in graph.astream(payload, config, stream_mode="updates"):
                for node, update in (chunk or {}).items():
                    if node == INTERRUPT_KEY:
                        # 闸门挂起：`interrupt()` 的载荷是**单独一块**（不在任何节点的增量里），
                        # 而且**不算「跑过一个节点」**。所以这里既记录载荷、也不往 visited 里塞，
                        # 否则「这一轮真正执行了哪几个节点」会被一条伪节点名污染
                        # （它还会连带影响 `_failed_node` 的兜底推断与第 11 步的埋点归并）。
                        turn.interrupt = _interrupt_payload(update)
                        break
                    turn.visited.append(str(node))
                    if on_event is not None:
                        on_event(str(node), update)
        except RetryableToolError:
            # 可恢复：原样抛给外层（第 12 步的队列要重投，不该在这里被当成「查不到」）
            raise
        except GraphRecursionError:
            raise  # 图自己转圈了：接线 bug，必须冒出来
        except CheckpointUnavailable:
            raise  # 后端问题：与「节点失败」是两类事，不兜
        except Exception as exc:  # noqa: BLE001 - 这里就是兜底的位置
            turn.error = exc
            snapshot = await graph.aget_state(config)
            pending = tuple(snapshot.next or ())
            if not pending:
                # 失败发生在收尾之后（或图已无可推进位置）：没有断点可重放
                logger.warning("本轮失败且无可重放位置：%s", exc)
                break
            target = _failed_node(exc, pending, turn.visited)
            if replays_left > 0:
                replays_left -= 1
                logger.warning(
                    "执行在 %s 失败：%s → 从断点重放（已完成的节点不再执行）",
                    target,
                    exc,
                )
                turn.replayed.append(target)
                turn.visited.append(target)
                payload = None  # None = 从 checkpoint 继续
                continue
            logger.warning("重放预算用尽（%d 次），把失败写回 state 兜底：%s", replay_budget, exc)
            turn.exhausted = True
            turn.visited.append(target)
            update = failure_update(target, exc, snapshot.values)
            turn.fallback = dict(update)
            # 兜底＝**写状态**（`aupdate_state`），而不是就地返回：
            # 「少一份材料」不等于「这一轮答不了」——图还要继续跑到收口节点去，
            # 由 synthesis 把 refusals 如实转述成一句给用户的话。
            #
            # ⚠️ `as_node=target` 不能省：`aupdate_state` 的默认语义是「把这次更新记成
            # **上一个写状态的节点**干的」，于是 `next` 会按那个节点的出边重算 ——
            # 结果是失败节点又被排进 `next`，重放无限循环（实测见 runbook §10.1 坑 9）。
            # 指定 `as_node=target` 才是「这一步我们替它收尾」，图从它的下游继续。
            await graph.aupdate_state(config, update, as_node=target)
            payload = None
            # 收口型节点（synthesis / guardrail_output）的失败已把这一轮标死：
            # 兜底节点就是终点，下面的 `next` 检查会看到图已结束、自然收尾。
            # 网络型节点的失败则要继续驱动（图从 `target` 的下游接着跑）。
            continue

        if turn.pending_approval:
            # 挂起了：`astream` 那一轮已经结束（图停在这个节点上，checkpoint 里 `next` 就是它），
            # 再驱动一次只会再次挂起。恢复必须由**批准通道**用 `Command(resume=...)` 发起。
            logger.info("闸门挂起：%s（等待人工批准）", turn.summary())
            break

        snapshot = await graph.aget_state(config)
        if not tuple(snapshot.next or ()):
            break
        payload = None  # 还有下一步：接着驱动（正常的顺序执行也会走到这里）

    final = await graph.aget_state(config)
    turn.state = dict(final.values)
    if turn.fallback is not None:
        # ⚠️ 兜底**必须最后再合一次**：`aupdate_state` 写下的 `blocked=True` 之后，
        # 图可能还会跑完剩下的一两步（收口型节点失败时通常不会，但 `guardrail_output`
        # 这类节点会顺手写 `blocked=False`），把兜底结论覆盖掉 ——
        # 症状是「用户看到安全话术，但 `turn.blocked` 是 False」，调用方据此返回 200，
        # 而实际上这一轮什么都没做成（实测见 runbook §10.1 坑 10）。
        # 所以兜底的语义是「这一轮的最终结论」，它压在最后。
        turn.state.update(turn.fallback)
    elif turn.blocked and not str(turn.state.get("answer") or "").strip():
        # 输入侧护栏短路时**根本没有节点写过 `answer`**（synthesis 一次都没跑），
        # 而护栏给的落地话术在 `guardrail_message` 里。出网那一段文本只能有一个来源，
        # 否则第 9 步要写 `answer or guardrail_message` 这种「两处都知道」的代码 ——
        # 那种代码一旦漏写，用户看到的是空白回答（比一句「我无法处理」更糟）。
        turn.state["answer"] = str(turn.state.get("guardrail_message") or "")
    if not turn.visited:
        # 极端情况（重放的第一步就没产生任何更新）：把 checkpoint 里的 pending 记进路径，
        # 否则 summary() 会显示「路径=(空)」，排障时看不出卡在哪
        turn.visited = [str(node) for node in (final.next or ())]
    return turn


def _interrupt_payload(chunk: Any) -> dict[str, Any]:
    """把 `{"__interrupt__": (Interrupt(...),)}` 里的载荷翻成一个普通 dict。

    为什么要在图这一层翻译（而不是让 API 去认识 `Interrupt`）：

    * `Interrupt` 是 LangGraph 的类型，它的 `value` 只承诺是 JSON 可序列化对象，
      形状由**我们自己的节点**（`make_gate`）决定 —— 「闸门长什么样」是图的事；
    * API 层要的是「一句能展示给审批人的问题 + 一个能回传的中断 id」，
      这两个字段的取舍（哪个进日志、哪个回给前端）放在这里最省事。

    ⚠️ 载荷里的值**不保证是 dict**（节点可以 `interrupt("一句话")`），
    所以非 dict 载荷被包成 `{"value": ...}` 而不是直接 `dict(...)`（那会抛 TypeError，
    把一次正常的挂起变成 500）。
    """
    first = (chunk[0] if isinstance(chunk, (list, tuple)) and chunk else chunk)
    value = getattr(first, "value", None)
    if value is None and isinstance(first, dict):
        value = first.get("value")
    payload: dict[str, Any] = dict(value) if isinstance(value, dict) else {"value": value}
    payload["interrupt_id"] = str(getattr(first, "id", "") or "")
    return payload


def _failed_node(error: Exception, pending: tuple[str, ...], visited: list[str]) -> str:
    """认出「失败的是哪个节点」。

    优先用 `agent`/节点名线索：LangGraph 的异常里带 `during task with name 'router'`
    这类信息；`pending` 是 checkpoint 记的「下一步」——正常情况下它就是那个失败节点。
    `visited` 的最后一项是次优线索（顺序执行的图里，失败节点正好是最后被调度的那一个）。
    """
    text = str(error)
    marker = "task with name '"
    if marker in text:
        name = text.split(marker, 1)[1].split("'", 1)[0]
        if name:
            return name
    if pending:
        return pending[0]
    return visited[-1] if visited else "unknown"


__all__ = ["DEFAULT_REPLAY_BUDGET", "INTERRUPT_KEY", "GraphTurnFailed", "Turn", "run_turn"]
