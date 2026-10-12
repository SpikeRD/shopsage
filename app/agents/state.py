"""节点间共享状态：`GraphState`（`7.agents_module.md` 第 2 节）。

本模块**只定义数据契约**，不含任何逻辑 —— 它必须能被「读完就知道节点之间传什么」。
六个 Agent 的签名统一是 `xxx_node(state) -> dict`（**只返回自己写的那几个键**），
由第 8 步的 LangGraph 负责合并进主 state。

⚠️ 两处与文档表格的差异，都是刻意的（详见 `docs/runbook/7.agents-runbook.md` §1.3）：

1. **位置**：文档 §2 把 `GraphState` 归在第 8 步的 `app/graph/state.py`。本步把它放在
   `app/agents/state.py`，理由是**第 7 步必须不依赖 langgraph**：`langgraph` 至今
   **不在** `pyproject.toml` 的依赖里（`uv.lock` 里也搜不到），若本步从 `app.graph` 取类型，
   六个 Agent 就连 import 都跑不起来，「六个 Agent 可独立调用、独立测试」这条验收直接落空。
   第 8 步落地时**从本模块 import 并 re-export** 即可，不必抄一份。
2. **节点返回「增量」而不是整个 state**：文档 §2 的写法是 `def xxx_node(state: GraphState) -> GraphState`。
   但 LangGraph 节点的真实语义就是「返回要更新的字段」（返回整个 state 也只会被合并那几个键），
   而且返回增量可以让「这个节点到底写了什么」在测试里一眼可断言。
   两者对第 8 步**等价**，对第 7 步则省掉大量 `{**state, ...}` 样板。

为什么用 `TypedDict` + `total=False` 而不是 dataclass：state 会随流程逐步填充
（router 之前没有 `route`，data 之前没有 `sql`），`total=False` 如实表达「这些键可能还没被写入」；
用 dataclass 就得给每个键一个假默认值，那是把「缺失」伪装成「有值」——
第 8 步的条件边恰恰要靠「这个键在不在」来分支。
"""

from __future__ import annotations

from typing import Any, Final, TypedDict

from app.security.policy import CUSTOMER_ROLE
from app.security.rbac import ROLE_PERMISSIONS, permissions_for

# ── 取值域（集中一处，router / synthesis / data 都从这里取，避免各写一份字符串）──

#: 意图路由的取值域。`final` 是「不需要查任何数据，直接回答/拒答」的出口：
#: 有了它，router 才不必把「你好」硬塞进 rag 或 data（那会白白花一次检索/一次 SQL）。
ROUTES: Final[tuple[str, ...]] = ("data", "rag", "analysis", "ticket", "final")

#: 对客角色的名字（`policy.CUSTOMER_ROLE` 的别名，供不方便 import policy 的调用方用）
CUSTOMER: Final[str] = CUSTOMER_ROLE

#: 「当前消息在哪」——`messages` 里最后一条 user 消息的下标，取不到就是「没有新问题」
NO_MESSAGE: Final[int] = -1


class GraphState(TypedDict, total=False):
    """节点间共享状态。

    分四组，读的时候按组理解，别把它当成一锅字段：

    | 组 | 字段 | 谁写 | 谁读 |
    |---|---|---|---|
    | 输入 | `messages` / `user_id` / `role` | 入口（第 9 步 API） | 全部 |
    | 身份 | `permissions` / `is_customer` | **只有 router** | data / analysis / synthesis / ticket |
    | 分支 | `route` / `route_confidence` / `route_reason` | router | 第 8 步的条件边 |
    | 产物 | `sql` / `sql_attempts` / `rows` / `row_meta` / `docs` / `chart_spec` / `analysis` / `answer` / `citations` / `refusals` | 各自的 Agent | synthesis |

    第 11 步补上 `rag` / `analysis` 之后，这两组会多出 `docs` 与 `analysis` 的填充者，
    但字段本身不变 —— 这也是「先定契约、后加节点」的价值。

    第 13 步又加了**第五组**（`cached_answer` / `cache_info`），同样不改已有字段：
    缓存是横切在 rag 与 synthesis 之间的一条缝（rag 写、synthesis 读），
    而它的证据（「这一轮命中了」）必须能被断言 —— 从一个 `answer` 字符串上是看不出来的。
    """

    # ── 输入 ──────────────────────────────────────────────────────────────────
    #: 对话历史（OpenAI 格式：`{"role": "user"|"assistant"|"system", "content": str}`）。
    #: 用普通 `list` 而不是 LangGraph 的 `Annotated[..., add_messages]`：第 7 步不该依赖 langgraph，
    #: 第 8 步在 `app/graph/state.py` 里加上 reducer 即可（reducer 是「图怎么合并」的事，
    #: 不是「状态长什么样」的事）。
    messages: list[dict[str, Any]]
    #: 当前请求者的用户 id（对客路径的行级过滤依据）。
    user_id: int
    #: 角色名，取值必须是 `rbac.ROLE_PERMISSIONS` 里的键（未登记角色 → 零权限，默认拒绝）。
    role: str

    # ── 身份（router 写，别人只读）─────────────────────────────────────────────
    #: 角色 → 权限点（**从 `rbac` 查表得来**，不是模型说的）。见 `inject_identity()`。
    permissions: frozenset[str]
    #: 是否对客路径。后续节点用它决定「要不要强制行级过滤」「用哪套语气合成」，
    #: **不自己判断身份**（`7.agents_module.md` §4 第 4 条：越权判断不许分散在各处）。
    is_customer: bool

    # ── 分支 ─────────────────────────────────────────────────────────────────
    route: str
    #: `"high"` / `"low"`；`low` 表示模型给不出确定分类（走保守分支，见 `router.py`）。
    route_confidence: str
    route_reason: str

    # ── 产物 ─────────────────────────────────────────────────────────────────
    #: 本轮生成的 SQL（已过校验、可直接执行的那条）。
    sql: str
    #: 生成尝试次数（含重试）。`> 1` 说明发生过「校验失败 → 回喂报错 → 重写」。
    sql_attempts: int
    #: 查询结果（驱动原样：`Decimal` / `datetime` 都在，见 `tools/sql_executor.py`）。
    rows: list[dict[str, Any]]
    #: 执行元信息：`row_count` / `truncated` / `audit_id` / `duration_ms`…
    row_meta: dict[str, Any]
    #: 知识库检索命中（`RetrievedDoc`，**每条都带 source**）。
    docs: list[Any]
    #: 图表 spec（`tools/chart.py` 的 `shopsage.chart/v1`）。
    chart_spec: dict[str, Any] | None
    #: 对内分析的结论（结构化，不是自由文本）。
    analysis: dict[str, Any]
    #: 最终回答（对用户可见的那段文本）。
    answer: str
    #: 回答里引用到的出处串（`RetrievedDoc.citation()`）。
    citations: list[str]
    #: 「为什么没答上」的记录（权限不足/能力未就绪/没有数据），**不许编造答案时用它兜底**。
    refusals: list[str]

    # ── 缓存（第 13 步；只有 rag 节点写，synthesis 读）─────────────────────────
    #: 语义缓存命中的**整段答案**。命中时 synthesis 直接用它，**不再打模型**（E1 的落点）。
    #:
    #: ⚠️ 它为空串与「没有这个键」在图上等价（`total=False` 的语义），
    #: 所以 `synthesis_node` 的判断是 `if state.get("cached_answer")`。
    #: 刻意**不放进 `row_meta`**：那是 SQL 执行元信息的字典，混进缓存状态会让
    #: 「查询耗时多少」这种问题在排障时读到一个不相干的键。
    cached_answer: str
    #: 缓存账目：`{"outcome": "hit"|"miss"|"stale"|"bypassed", "age_seconds": …, "detail": …}`。
    #: 它进 state 的唯一理由是**可断言**：「这一轮用了缓存吗」在测试里必须能一眼看到
    #: （只看 `answer` 是看不出来的 —— 缓存里的答案与刚生成的答案文本一样）。
    cache_info: dict[str, Any]
    #: **这一轮用的检索参数**（`rag` 写、`synthesis` 读回去用于写缓存）。
    #:
    #: ⚠️ 为什么它必须在 state 里（而不是让两个节点各自拼一份）：
    #: 这些参数进缓存的**版本指纹**，两处拼出不同的组合 = 写进去的条永远命中不了。
    #: 唯一的构造处是 `app/cache/params.retrieval_params`，`rag_node` 调它一次、
    #: 放进这里，`synthesis` 原样带回去（见 `app/agents/rag.remember_answer` 的说明 ——
    #: 那条纪律是本仓实测踩坑之后补上的）。
    cache_params: dict[str, Any]
    #: 写缓存的**上下文**（`rag` 写、`synthesis` 读）：问题串 / scope / 指纹 / 版本 / 分档 / TTL。
    #:
    #: ⚠️ 它必须是**可序列化的字段**（字符串 / 整数 / 简单 dict）：
    #: LangGraph 的 checkpointer 会对 state 值做 msgpack 序列化，
    #: 把 `Prepared`（dataclass）放进来会被**静默丢掉**、放闭包会直接抛
    #: `TypeError: Type is not msgpack serializable: function` ——
    #: 两种情况的表现都是「缓存写了但永远不命中」（本仓实测踩过，见 `rag.remember_answer`）。
    cache_context: dict[str, Any]

    # ── 护栏（横切）──────────────────────────────────────────────────────────
    #: 输入侧被拦时为 True，第 8 步据此短路后续节点。
    blocked: bool
    #: 由哪个阶段拦下的：`input` / `output`。
    guardrail_stage: str
    #: 命中的规则名（注入关键词）或不合规判定（输出侧）。
    guardrail_rules: list[str]
    #: 被拦时给用户的落地话术（**不含内部细节**，可直接作为 `answer`）。
    guardrail_message: str


def messages_of(state: GraphState) -> list[dict[str, Any]]:
    """安全取出 `messages`（缺失/类型不对时返回空列表）。"""
    raw = state.get("messages")
    return list(raw) if isinstance(raw, list) else []


def last_user_message(state: GraphState) -> str:
    """取最后一条 **user** 消息的文本；没有就返回空串。

    为什么只认 `user` 角色：注入检测与意图分类都必须作用于「用户说的话」。
    把 assistant 的历史回答也一起扫，会让模型自己上一轮的措辞触发本轮拦截
    （实测会出现「上一轮答了句含『忽略之前的指令』的说明，这一轮就没法说话了」）。
    """
    for message in reversed(messages_of(state)):
        if str(message.get("role", "")) == "user":
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


def permissions_of(state: GraphState) -> frozenset[str]:
    """取当前权限点集合。**缺失时按角色现查**（而不是返回空集）。

    为什么不用空集兜底：state 里没有 `permissions` 的调用方（例如直接单测 data 节点）
    会得到「零权限」，症状是「明明是对客查询却报权限不足」——
    让人去怀疑 RBAC 表，而真正的原因是状态没填。按角色现查更符合直觉，
    且 `permissions_for()` 对未登记角色本来就返回空集（默认拒绝不会因此失守）。
    """
    stored = state.get("permissions")
    if isinstance(stored, frozenset):
        return stored
    return permissions_for(role_of(state))


def role_of(state: GraphState) -> str:
    """取角色名；缺失时**按对客处理**（最保守的假设：看不到全站数据）。"""
    role = state.get("role")
    return str(role) if role else CUSTOMER


def is_customer_path(state: GraphState) -> bool:
    """是否对客路径。

    判据优先用 `is_customer`（router 注入的显式标记），退回「`role == customer`」。
    两条口径都在这里收口，避免 downstream 各写一个判断 ——
    `7.agents_module.md` §4 第 4 条要防的正是「越权判断分散在各处」。
    """
    flag = state.get("is_customer")
    if isinstance(flag, bool):
        return flag
    return role_of(state) == CUSTOMER


def inject_identity(user_id: int, role: str) -> dict[str, Any]:
    """把「谁、什么角色、有哪些权限点」算成要写进 state 的那几个键。

    这是 `7.agents_module.md` §4 第 4 条的落点：**权限只在 router 注入一次**，
    后续 data/analysis/synthesis 只消费 `state["permissions"]`，不各自判断身份。
    集中注入的好处不是「少写几行」，而是「越权判断只有一处可审」——
    分散判断时，漏掉任意一处就是一个越权口子，而漏掉的部分不会有任何报错。

    ⚠️ 权限**从 `rbac.ROLE_PERMISSIONS` 查表得来**，绝不采信模型输出。
    router 的 LLM 只回答「走哪条路」，身份是调用方给的既成事实 ——
    若让模型同时输出角色，就等于让被攻击的目标自己决定权限。
    """
    role = str(role or CUSTOMER)
    if role not in ROLE_PERMISSIONS:
        # 未登记角色不报错，但**必须留下痕迹**：它是「配置写错了」与「有人在试探」的共同症状。
        import logging

        logging.getLogger(__name__).warning(
            "角色 %r 未在 rbac.ROLE_PERMISSIONS 里登记，按零权限处理（默认拒绝）", role
        )
    return {
        "permissions": permissions_for(role),
        "is_customer": role == CUSTOMER,
        "role": role,
        "user_id": int(user_id),
    }


class MissingIdentityError(Exception):
    """state 里没有可用的 `user_id`。

    单独一个异常而不是 `PermissionDenied`：后者会被第 9 步映射成 403（「你没这个权限」），
    而真相是「调用方没告诉我是谁」—— 那是 500 级的接线错误，映射错了会让人去查 RBAC 表。
    """


def require_user_id(state: GraphState) -> int:
    """取 `user_id`；缺失/非法直接抛（**不许用 0 或 None 兜底**）。

    为什么要显式抛：对客路径的行级过滤全靠这个值。若它悄悄变成 `None`，
    `WHERE user_id = :uid` 会带一个 NULL 进去 —— 查询正常返回 0 行、
    看起来像「你没有订单」，而真正的问题是「没人告诉我你是谁」。
    """
    user_id = state.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise MissingIdentityError(
            f"state 缺少合法的 user_id（收到 {user_id!r}）：行级过滤没有依据，"
            "拒绝继续。请在入口（第 9 步 API）把通过鉴权的 user_id 写进 state。"
        )
    return user_id


__all__ = [
    "CUSTOMER",
    "NO_MESSAGE",
    "ROUTES",
    "GraphState",
    "MissingIdentityError",
    "inject_identity",
    "is_customer_path",
    "last_user_message",
    "messages_of",
    "permissions_of",
    "require_user_id",
    "role_of",
]
