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


__all__ = [
    "APPROVAL_QUESTION",
    "REJECTED_REASON",
    "SENSITIVE_ROUTES",
    "GraphRuntime",
    "InterruptPolicy",
]
