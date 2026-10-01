"""Agent 的公共零件：**结构化输出**的取用、修复与失败口径（`7.agents_module.md` §4 第 1 条）。

六个 Agent 里五处都要「让 LLM 吐一个 JSON 对象、解析它、解析不了就回喂让它改」。
这段逻辑写五遍必然漂移（某处记得 `strip()`、某处忘了代码围栏、某处解析失败直接崩），
所以收在本模块。它**不认识任何业务字段** —— 只做「要一个 dict 出来」。

三条设计口径
------------

1. **注入点是 `llm` 参数，不是 monkeypatch**：每个节点都接受 `llm=<可调用对象>`，
   测试传假函数即可跑全部代码路径（与 `tools/retriever.py` 的 `search=` 同一思路）。
   这比 `monkeypatch.setattr(litellm, ...)` 好在：假 LLM 的**返回值**就是契约本身，
   读测试即可知道 prompt 该产出什么结构。

2. **同步或异步都吃**（`_maybe_await`）：生产实现 `default_llm` 是 async（`llm_call` 是 async），
   而测试里的假 LLM 写成同步函数最好读。让节点内部统一 `await _maybe_await(llm(...))`，
   两种写法都能跑 —— 这省掉的是「每个测试都要再包一层 async wrapper」的噪声。

3. **解析失败要留证据，并允许一次修复**：`AgentOutputError` 带**原始文本**与**失败原因**。
   只抛「解析失败」，排障时就得去翻 provider 的原始响应；带上 raw，
   一眼能看出「模型在 JSON 外面加了一段解释」还是「返回了空串（被 max_tokens 截断）」。
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any, Final, Protocol

import sqlglot

from app.llm import LLMResponse, llm_call
from app.security.sql_guard import DIALECT

logger = logging.getLogger(__name__)

#: 默认修复次数（**总尝试 = 1 + 1**）。刻意只有一次：
#: 第二次修复仍失败说明模型根本给不出这个结构（prompt 有问题，或模型不支持），
#: 继续重试只是把同一个错误花三倍 token 犯三遍 —— 与 `llm_call` 的退避口径一致：
#: 「重试」要能改变结果，改不了的重复不是重试。
DEFAULT_REPAIRS: Final[int] = 1

#: 解析失败时回喂给模型的原文上限。太长会把上下文顶爆（E2），太短又看不出问题。
MAX_ECHO_CHARS: Final[int] = 1200

#: 判定「模型把 JSON 包在代码围栏里」用的标记。
_FENCE: Final[str] = "```"


class AgentOutputError(RuntimeError):
    """LLM 没有按要求给出**可解析的结构化输出**。

    带上 `raw` / `reason` / `attempts` 三个字段，是为了让这条红**自解释**：
    看到日志就能区分「模型啰嗦」（raw 里有一段散文）与「模型被截断」（raw 是空串），
    这两者的修法完全不同（改 prompt vs 调大 `max_tokens`）。
    """

    def __init__(
        self,
        message: str,
        *,
        agent: str = "",
        raw: str = "",
        reason: str = "",
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.agent = agent
        self.raw = raw
        self.reason = reason
        self.attempts = attempts


class JsonLLM(Protocol):
    """结构化 LLM 的调用契约：给消息列表，回一个 dict（或可直接解析成 dict 的文本）。

    `**kwargs`（如 `max_tokens` / `temperature`）透传给底层模型；
    实现方必须能忽略不认识的键 —— 假实现常写成 `def fake(messages, **kw)`。
    """

    def __call__(
        self, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Mapping[str, Any] | str | Awaitable[Any]:
        ...


def extract_json_object(text: str) -> Mapping[str, Any]:
    """从模型输出里抠出一个 JSON 对象；抠不到就抛 `AgentOutputError`。

    按**宽容度递增**的三步走（每一步都是实测踩出来的需求）：

    1. 直接 `json.loads`（模型规矩时走这条，零成本）；
    2. 剥掉 markdown 代码围栏再试 —— 这是最常见的形态（```json ... ```）；
    3. 从第一个 `{` 到最后一个 `}` 之间再试一次 —— 兜住「JSON 前后各有一句解释」。

    第 3 步为什么用「第一个 { 到最后一个 }」而不是「第一个 { 到第一个配对的 }」：
    后者需要写一个括号匹配器，而模型输出里的花括号**必然**出现在 JSON 字符串中
    （例如 SQL 里的 `{}`、正则），手写匹配器在字符串转义上出错时会切出非法 JSON ——
    用最宽的范围去试，失败了照样报错，但**不会**把一个本来合法的 JSON 切坏。
    """
    if not isinstance(text, str) or not text.strip():
        raise AgentOutputError(
            "LLM 返回空内容：可能是 prompt 被拒（内容过滤）或 max_tokens 太小"
            "（推理模型会先把 token 花在 reasoning 上，见 runbook §9 坑 4）",
            raw=text if isinstance(text, str) else repr(text),
            reason="empty",
        )

    candidates = [text.strip()]
    fenced = _strip_fence(text.strip())
    if fenced is not None:
        candidates.append(fenced)
    body = text.strip()
    if "{" in body and "}" in body:
        candidates.append(body[body.find("{") : body.rfind("}") + 1])

    last_error = ""
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        if isinstance(data, Mapping):
            return data
        last_error = f"顶层不是对象，而是 {type(data).__name__}"
    raise AgentOutputError(
        f"LLM 输出不是合法 JSON 对象（{last_error}）", raw=text[:MAX_ECHO_CHARS], reason="not_json"
    )


def _strip_fence(text: str) -> str | None:
    """剥掉首尾的 markdown 代码围栏；没有围栏返回 None。"""
    if not text.startswith(_FENCE):
        return None
    first_newline = text.find("\n")
    if first_newline < 0:
        return None
    body = text[first_newline + 1 :]
    end = body.rfind(_FENCE)
    return (body[:end] if end >= 0 else body).strip()


def plan_json_call(
    messages: list[dict[str, Any]], *, raw: str = "", problem: str = ""
) -> tuple[list[dict[str, Any]], bool]:
    """算出「第一次要发什么」或「修复时追加什么」，返回 `(消息, 是否修复轮)`。

    **纯函数**，不碰 LLM —— 于是「修复 prompt 里到底写了什么」可以单独断言，
    不必真的跑一次模型（`7.agents_module.md` §5 的「报错重试的 prompt 怎么写」就在这里被钉住）。

    修复轮追加两条消息：一条 assistant（**回显模型上次的原话**）+ 一条 user（说清哪里不合格）。
    为什么不把原文塞进一条 user 消息完事：回显成 assistant 消息能维持正常的角色交替，
    且让模型看到「这是我自己刚说的话」，比在 user 消息里引用它更不容易被它当成新指令。
    """
    if not problem:
        return list(messages), False
    repair = list(messages)
    repair.append({"role": "assistant", "content": raw[:MAX_ECHO_CHARS]})
    repair.append({"role": "user", "content": problem})
    return repair, True


def repair_prompt(problem: str) -> str:
    """把「哪里不合格」翻译成**可照做**的修复指令（区别于笼统的「再试一次」）。

    `7.agents_module.md` §5 的原话是「把具体报错回喂模型，让它针对性修复」。
    实测这句必须落在 `problem` 的措辞上：告诉它「不是 JSON 对象」它就会去补 JSON；
    只说「请重新输出」，模型大概率把上一轮的散文重写一遍（连错都一样）。
    """
    return (
        "你上一次的回复无法被程序解析，请**只**重新输出一个 JSON 对象（不要解释、不要代码围栏）。\n"
        f"具体问题：{problem}"
    )


async def _maybe_await(value: Any) -> Any:
    """兼容「同步假 LLM」与「异步真 LLM」。

    为什么值得一个函数：生产路径上 `llm_call` 是 async，而单测里写
    `def fake(messages): return {...}` 最省事；若节点强制要求 awaitable，
    每个测试都得写一层 `async def` 包装，噪声会掩盖真正被测的东西。
    """
    if inspect.isawaitable(value):
        return await value
    return value


async def generate_json(
    messages: list[dict[str, Any]],
    *,
    llm: JsonLLM | None = None,
    agent: str,
    repairs: int = DEFAULT_REPAIRS,
    **kwargs: Any,
) -> Mapping[str, Any]:
    """调 LLM 并拿到一个 JSON 对象；失败时回喂问题修复一次，仍失败则抛 `AgentOutputError`。

    参数：
        messages: 已经拼好的 prompt（system + user）。
        llm: 注入点；`None` = `default_llm`（真正打模型）。
        agent: 出错信息里用的节点名（`router` / `data` / …），便于日志归并。
        repairs: 允许的修复轮数（默认 1，理由见 `DEFAULT_REPAIRS`）。
        **kwargs: 透传给 LLM（如 `max_tokens`）。
    """
    caller = llm if llm is not None else default_llm
    current = list(messages)
    raw = ""
    failed_reason = ""
    attempts = 0

    for _ in range(repairs + 1):
        attempts += 1
        payload = await _maybe_await(caller(current, **kwargs))
        raw = _content_of(payload)
        try:
            return payload if isinstance(payload, Mapping) else extract_json_object(raw)
        except AgentOutputError as exc:
            failed_reason = exc.reason or str(exc)
            current, _ = plan_json_call(messages, raw=raw, problem=repair_prompt(failed_reason))

    raise AgentOutputError(
        f"{agent} 节点的 LLM 连续 {attempts} 次未给出可解析的 JSON 对象，已放弃"
        f"（最后一次原因：{failed_reason}）",
        agent=agent,
        raw=raw[:MAX_ECHO_CHARS],
        reason="exhausted",
        attempts=attempts,
    )


def _content_of(payload: Any) -> str:
    """把 LLM 的返回归一成**文本**（`LLMResponse` / `str` / `Mapping` 都吃）。

    为什么要认 `Mapping`：假 LLM 直接返回 dict 是最自然的写法，
    若强制要求它返回 JSON 字符串，测试里到处是 `json.dumps(...)`，反而看不出契约。
    """
    if isinstance(payload, Mapping):
        return json.dumps(payload, ensure_ascii=False)
    if isinstance(payload, LLMResponse):
        return payload.content or ""
    return payload if isinstance(payload, str) else str(payload)


async def default_llm(messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
    """生产用的 LLM 实现：**只允许**经 `app.llm.llm_call`（`5.llm.md` 第 1 节的硬约束）。

    这里不 catch 任何异常：`RetryableLLMError` / `PermanentLLMError` 的分类是第 12 步
    队列「重投 vs 死信」的依据，在 Agent 层吞掉它，队列就永远看不到真实错误类型。
    """
    return await llm_call(messages, **kwargs)


# ── NL→SQL 的行级过滤自检（对客路径的硬约束）─────────────────────────────────


def customer_row_filter_error(sql: str, *, user_id: int, agent: str = "data") -> str | None:
    """对客 SQL 缺少 `user_id = :绑定参数` 行级过滤时返回**可回喂的报错**，否则 None。

    为什么在建 SQL 之后、执行之前再加这一道（`validate_sql` 已经不缺校验了）：
    `sql_guard` 只知道「有 WHERE 且不恒真」，它**不知道你是谁**——
    `SELECT order_id FROM orders WHERE status = 'pending'` 完全合法，却会把别人的订单一并读出来。
    对客路径的行级过滤是 D1 的核心，而「LLM 记得写 user_id」不能作为机制。

    ⚠️ 为什么这里可以用 sqlglot 解析（而不是像 `db/queries.py` 那样坚持「过滤只有一处真相」）：
    本函数**只读不生成 SQL** —— 它校验模型给的那条语句，不改写它。
    唯一真相（WHERE 怎么写）仍在模型/caller 手里，这里只负责「不满足就拒绝」。
    一旦这里开始**改写** SQL（比如自动补 WHERE），就会出现第二处 SQL 生成逻辑，那才是要防的。

    判据只要两条（宽严适度）：
      ① 存在一个列名 `user_id` 的比较，且等号右边是**参数占位符**（`:uid` / `%(uid)s`）——
         不要字面量：字面量意味着「user_id 写死在 SQL 里」，模型可以写 1 而调用方是 2；
      ② 该比较必须落在**最外层 SELECT 的 WHERE**里（子查询里的不算：外层仍可能全表扫）。
    """
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception as exc:  # noqa: BLE001 - 解析失败＝校验不了，一律拒绝（与 sql_guard 同口径）
        return f"SQL 无法解析，无法核实行级过滤：{type(exc).__name__}: {exc}"

    select = tree if isinstance(tree, sqlglot.exp.Select) else _last_select(tree)
    if select is None:
        return "SQL 里找不到最外层 SELECT，无法核实行级过滤"

    where = select.args.get("where")
    condition = getattr(where, "this", None) if where is not None else None
    if condition is None:
        return _row_filter_hint(user_id, "最外层 SELECT 没有 WHERE 子句")

    for column, value in _equalities(condition):
        if column.name.lower() != "user_id":
            continue
        if isinstance(value, sqlglot.exp.Placeholder):
            return None  # ✅ 形如 `user_id = :uid`
        return _row_filter_hint(
            user_id,
            f"`user_id` 是用字面量 {value.sql(dialect=DIALECT)!r} 比较的；"
            "对客 SQL 必须用绑定参数（不能把身份写死在 SQL 文本里）",
        )
    return _row_filter_hint(user_id, "最外层 WHERE 里没有 `user_id = 绑定参数`")


def _row_filter_hint(user_id: int, problem: str) -> str:
    return (
        f"{problem}。对客查询必须自己限定到本人："
        f"`WHERE user_id = :uid ...` 并把 `{{'uid': {user_id}}}` 一起绑定。"
    )


def _last_select(tree: sqlglot.exp.Expression) -> sqlglot.exp.Select | None:
    selects = list(tree.find_all(sqlglot.exp.Select))
    return selects[-1] if selects else None


def _equalities(condition: sqlglot.exp.Expression) -> list[tuple[sqlglot.exp.Column, sqlglot.exp.Expression]]:
    """列出条件里的等值对（`a = b`），两侧都试 —— 谁在左谁在右不许假设。"""
    pairs: list[tuple[sqlglot.exp.Column, sqlglot.exp.Expression]] = []
    for node in condition.walk():
        if not isinstance(node, sqlglot.exp.EQ):
            continue
        left, right = node.this, node.expression
        if isinstance(left, sqlglot.exp.Column):
            pairs.append((left, right))
        if isinstance(right, sqlglot.exp.Column):
            pairs.append((right, left))
    return pairs


def format_rows(rows: Sequence[Mapping[str, Any]], *, limit: int = 20) -> str:
    """把查询结果渲染成 prompt 里的一段紧凑文本（给 synthesis / analysis 用）。

    为什么不用 `json.dumps(rows)`：`Decimal` / `datetime` **不是** JSON 类型，
    `json.dumps` 会直接 `TypeError`（`6.tools-runbook.md` §9.1 坑 6 同一个坑）。
    这里用 `str()` 落成表格文本 —— 模型只需要看懂值，不需要机器可解析的 JSON。

    `limit` 是给上下文的硬上限（E2）：结果最多 200 行，全塞进 prompt 既贵又没用，
    所以这里只给前 N 行，并在截断时**明确写出来**（默认值见调用方）。
    """
    if not rows:
        return "（查询结果是 0 行）"
    shown = list(rows[:limit])
    lines = [" | ".join(str(key) for key in shown[0].keys())]
    for row in shown:
        lines.append(" | ".join(_cell(value) for value in row.values()))
    if len(rows) > len(shown):
        lines.append(f"…（共 {len(rows)} 行，此处只展示前 {len(shown)} 行）")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    return str(value)


def build_system_prompt(role_text: str, *, output_schema: str) -> str:
    """拼 system prompt：角色说明 + **输出契约**。

    把输出契约放进 system 而不是散在 user 里，是因为它对本节点是**不变量**
    （每一轮都一样），而 user 消息是变量。分开还有一个实际好处：
    修复轮只追加 user 消息，system 里的契约不会在追加时被挤出上下文。
    """
    return f"{role_text}\n\n输出格式（必须严格遵守）：\n{output_schema}"


__all__ = [
    "DEFAULT_REPAIRS",
    "MAX_ECHO_CHARS",
    "AgentOutputError",
    "JsonLLM",
    "build_system_prompt",
    "customer_row_filter_error",
    "default_llm",
    "extract_json_object",
    "format_rows",
    "generate_json",
    "plan_json_call",
    "repair_prompt",
]
