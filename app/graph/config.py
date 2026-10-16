"""图侧运行参数与 **human-in-the-loop 策略**（`8.graph.md` §4 第 5 条、§6 第 6 步）。

两件事分开：`GraphRuntime` 是「这一次跑图用什么依赖与预算」，`InterruptPolicy` 是
「哪些分支必须先让人点头」。它们都不是配置项（不读 `.env`），因为两者都是**调用方
的语义选择**：评测跑图要注入假模型、对客入口要强制人工审批某些分支 ——
把这种差异塞进全局配置，等于让「测试环境」与「生产环境」的行为靠环境变量对齐。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from app.agents.analysis import MAX_ANALYSIS_ROWS
from app.agents.data import DEFAULT_ROW_LIMIT, MAX_SQL_ATTEMPTS
from app.agents.rag import DEFAULT_MIN_SCORE, DEFAULT_TOP_K

#: 需要人工批准的 `route` 取值域（`8.graph.md` §5 第四条：退款/工单等敏感写操作）。
#:
#: ⚠️ **变更 003 把它清空了**，理由必须留住：
#: 变更 003 之前这里是 `("ticket",)`，配合 `GATED_ROLES={customer}` 的实际效果是
#: 「**客户自己点按钮批准自己的敏感操作**」（`POST /api/chat/approve` 的调用者就是发起人）。
#: 那不是人机审批，是**自审批** —— 一线与客户之间没有任何第二个人。
#: 需求 2 的原始描述「对客入口现在可以自己审批退款申请，明显有问题」指的就是它。
#:
#: 审批搬到了**退款单据**上（`refunds` 表 + `/api/refunds/decision`）：
#: 审批对象从「一轮对话」变成「一笔有 id、有金额、有状态机的记录」，
#: 而批准人必须是持有 `approve:refund` 的**员工**（客户永远拿不到这个权限点）。
#: 于是「谁批的、批的哪一笔、批了多少钱」第一次变成了可审计的事实
#: —— 旧的对话轮闸门连「批的是哪笔单」都说不出来。
SENSITIVE_ROUTES: Final[tuple[str, ...]] = ()

#: 中断时给审批人看的问题（**固定文本 + 可核对的事实**，不让模型现编）。
APPROVAL_QUESTION: Final[str] = (
    "该请求被判定为需要人工确认的敏感操作。请确认是否继续执行（批准后才会调用写路径）。"
)

#: 审批未通过时落进 `refusals` 的话术（会由 synthesis 如实转述给用户）。
REJECTED_REASON: Final[str] = (
    "该敏感操作未获人工批准，已按未执行处理。如确需办理，请联系人工客服走标准流程。"
)


@dataclass(frozen=True)
class InterruptPolicy:
    """human-in-the-loop 策略：哪些分支要停、停了之后怎么处置。

    默认**关闭**（`enabled=False`）的理由：中断会改变 `ainvoke` 的返回形态
    （`__interrupt__` 而不是最终 state），把默认值设成开会让所有调用方都得先处理它。
    真实入口（第 9 步 API）按角色决定是否开启：对客路径开、内部只读路径关。
    """

    enabled: bool = False
    #: 需要审批的 route 集合
    routes: tuple[str, ...] = SENSITIVE_ROUTES

    def requires_approval(self, state: Any) -> bool:
        """这个 state（router 跑完之后）是否需要人工点头。"""
        if not self.enabled:
            return False
        return str((state or {}).get("route") or "") in self.routes


#: 「人工审批闸门」按角色开关的规则（第 9 步落地）：**对客路径开**、内部只读路径关。
#:
#: ⚠️ **变更 003 把它清空了**（`GATED_ROLES = frozenset()`）：对客路径不再有任何 interrupt。
#:
#: 变更前它是 `{CUSTOMER_ROLE}`，含义是「客户发起敏感分支时停下来等人点头」。
#: 但它停下来的**审批人就是客户自己**（`POST /api/chat/approve` 只校验 thread_id 属于
#: 当前用户，不校验批准人是不是另一个人），于是「人机审批」在实践中退化成自审批。
#: 需求 2 要修的正是这件事。
#:
#: 现在的落点：审批变成**单据级**动作（`app/tools/refund.decide_refund`，
#: 要 `approve:refund`），挂在 `/api/refunds/decision` 上。图里不再需要闸门 ——
#: 客户的敏感请求（申请退款）**直接就落成 `requested` 状态**，等员工在审批台处理。
#:
#: ⚠️ 保留 `InterruptPolicy` / `make_gate` / `NODE_GATE` 这套**原语**（不删）：
#: 它们是「图能不能停下来」的框架能力，有独立用例覆盖（`tests/graph/test_graph_interrupt.py`
#: 里用最小图与显式 `InterruptPolicy(enabled=True, routes=("ticket",))` 验证），
#: 将来若真出现「必须由另一个人批准才能继续的一轮对话」，重建它的代价远高于留着。
#: 这是变更 003 决策 D2 的显式取舍：**宁可留一段不使用的原语，也不删掉已验的框架能力**。
GATED_ROLES: Final[frozenset[str]] = frozenset()


def interrupt_policy_for(role: str, *, enabled: bool = True) -> InterruptPolicy:
    """按角色造闸门策略。

    ⚠️ 变更 003 之后 `GATED_ROLES` 是**空集**，所以本函数在生产路径上**恒返回
    `enabled=False`**（对任何角色）。保留它的形状（而不是删掉这个函数）是为了：
      * `app/api/chat.py::_runtime_with_policy` 的接线不用改（它仍然「问一下策略」）；
      * 将来往 `GATED_ROLES` 里加回一个角色时，只需改那一个**数据**常量，
        不需要重建调用链 —— 这正是当初把它做成「查表」而不是硬编码 `if role == ...` 的价值。

    `enabled=False` 是「这次调用完全不用闸门」（例如离线评测、只读探活），
    与「角色不在 `GATED_ROLES` 里」是两件事：前者连节点都不注册（图更小、更省），
    后者注册了节点但永远放行。两者在 `build_graph` 里的表现不同，所以不能合成一个参数。
    """
    return InterruptPolicy(enabled=bool(enabled) and str(role) in GATED_ROLES)


@dataclass(frozen=True)
class GraphRuntime:
    """跑一次图所需的全部外部依赖与预算（**全部可注入**）。

    与六个节点的注入点是同一套参数，只是集中成一个对象：图的构建函数拿它造七个节点，
    测试拿它换掉模型与检索后端。字段默认值刻意从节点侧 import（而不是再写一遍数字）：
    「重试几次」「最多几行」只有一处真相，否则图与节点会各自漂移。
    """

    #: 模型出口（`JsonLLM` 协议：`await llm(messages, **kw)`）。`None` = 走生产实现。
    llm: Any = None
    #: 检索后端注入点（`app.tools.retriever` 的 `search=`）。`None` = 走生产实现。
    search: Any = None
    #: data 节点向工具要多少行
    row_limit: int = DEFAULT_ROW_LIMIT
    #: NL→SQL 的最大尝试次数（含首发）
    max_attempts: int = MAX_SQL_ATTEMPTS
    #: 检索取几条 / 相似度下限
    top_k: int = DEFAULT_TOP_K
    min_score: float | None = DEFAULT_MIN_SCORE
    #: 图表类型（analysis 出图时用）
    chart_type: str = "bar"
    #: 分析最多吃多少行（超过截断）
    max_analysis_rows: int = MAX_ANALYSIS_ROWS
    #: 人工审批策略
    interrupts: InterruptPolicy = field(default_factory=InterruptPolicy)
    #: ── 第 13 步：语义缓存 ──────────────────────────────────────────────────
    #: 缓存注入点（`app.cache.Cache`）。语义与第 10 步的 `search=` 完全一样：
    #:   * `None` → 走生产实现（`app.cache.default_cache()`，会连 Redis 与业务库）；
    #:   * 传一个实例 → 测试/演示用内存后端（**不碰任何真依赖**）。
    #:
    #: ⚠️ 为什么它是 `Any` 而不是 `Cache | None`：`app.cache` 会 import
    #: `app.observability`，而本模块（`graph/config.py`）被第 8 步的**所有**图用例
    #: import —— 在配置模块顶层拖进一个缓存包的 import，等于让「只想跑图结构断言」
    #: 的用例也付一次 `app.cache`（连带 `app.llm`、`app.observability`）的 import 代价。
    #: 注入点的类型标注价值远小于「图配置保持零依赖」这条纪律。
    cache: Any = None


__all__ = [
    "APPROVAL_QUESTION",
    "GATED_ROLES",
    "REJECTED_REASON",
    "SENSITIVE_ROUTES",
    "GraphRuntime",
    "InterruptPolicy",
    "interrupt_policy_for",
]
