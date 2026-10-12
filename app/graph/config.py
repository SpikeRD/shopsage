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
from app.security.policy import CUSTOMER_ROLE

#: 需要人工批准的 `route` 取值域（`8.graph.md` §5 第四条：退款/工单等敏感写操作）。
#:
#: 为什么按 **route** 而不是按「节点名」判定：本步骤还没有写节点（ticket 节点属第 9 步），
#: 而 route 是 router 已经产出的、可断言的事实。等第 9 步的 `ticket_node` 落地时，
#: 闸门仍然挂在同一个位置 —— 变化只是「批准之后跳到哪」。
SENSITIVE_ROUTES: Final[tuple[str, ...]] = ("ticket",)

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
#: 为什么按**角色**而不是全局开：
#:
#: * 对客路径上，敏感分支（`ticket` 这类写操作的入口）由客户自己发起，
#:   没有人工确认就直接执行 = 一个人可以反复触发写操作；
#: * 内部角色走的是标准作业流程（员工本来就是「被授权的人」），每一步都停下来等批准
#:   会把「分析一下上月退款率」变成一次审批流程 —— 那是流程倒退，不是安全加固。
#:
#: 这条规则放在 `graph/config.py` 而不是 API 层：它描述的是「图该怎么跑」，
#: 与 `SENSITIVE_ROUTES` 是同一类知识；API 层只负责**问**它，不负责定义它。
GATED_ROLES: Final[frozenset[str]] = frozenset({CUSTOMER_ROLE})


def interrupt_policy_for(role: str, *, enabled: bool = True) -> InterruptPolicy:
    """按角色造闸门策略（对客开、内部关）。

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
