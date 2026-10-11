"""API 层的**数据契约**：请求体、响应信封、WebSocket 帧、健康与错误结构。

为什么契约要单独成一个模块（而不是散在各路由里）：

1. **契约是「给别人看的」**：《`9.api.md` §2》列了四个对外接口，第 16 步的 Web 端要照它写前端。
   契约分散在路由函数签名里时，前端只能读 Python 源码或 OpenAPI 里那一堆内联 schema。
2. **一处可审**：`extra="forbid"` 这类「安全相关」的模型配置必须统一 ——
   它拦的是「客户端写错字段名却拿到 200」这种静默失败（详见 `ChatRequest`）。
3. **帧格式与错误格式只能有一份**：WebSocket 的四类帧、HTTP 的六种错误码，
   各写一遍必然漂移，而漂移的表现是「前端偶尔解析不出来」。

## 约定：`flow_state`（不要与 HTTP 状态码混起来）

HTTP 状态码回答「**这次调用**成功了没有」；`flow_state` 回答「**这一轮业务**做到了哪一步」。
两者刻意分开，因为它们的组合是有意义的：

| HTTP | `flow_state` | 含义 |
|---|---|---|
| 200 | `delivered` | 正常答完 |
| 200 | `blocked` | 图跑完了，但**这一轮被拦下/没做成**（护栏拒绝、兜底话术） |
| 202 | `approval_required` | 敏感操作挂起，等人工批准；`thread_id` 就是批准时要带回来的凭据 |
| 4xx / 5xx | （无） | 调用本身失败，看错误信封 |

⚠️ 为什么不把 `blocked` 也映射成 4xx：「被输出侧护栏拦下」是**正常的一轮对话**
（用户说了一句被拒的话，系统给了安全话术），把它打成错误会让前端弹红框、
也会让监控把「合规拦截」统计成故障 —— 那会污染告警信噪比。
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

# ── 取值域（前端与后端共用一份字面量，避免各写各的字符串）────────────────────

#: 会话 id 允许的字符：字母/数字/下划线/连字符/点。
#: ⚠️ **必须**排除 `:` 与空白 —— `thread_id` 的形状是 `u{user_id}:{session_id}`，
#: 允许 `:` 就意味着客户端能造出一个「看起来属于别人」的会话键（见 `app/api/deps.py`）。
SESSION_ID_PATTERN: Final[str] = r"^[A-Za-z0-9_.\-]{1,64}$"

#: 一轮业务的状态（见模块 docstring 的表）
FlowState = Literal["delivered", "blocked", "approval_required"]

#: WebSocket 帧的四种 `event` 取值
FRAME_SKELETON: Final[str] = "skeleton"
FRAME_NODE: Final[str] = "node"
FRAME_ANSWER: Final[str] = "answer"
FRAME_DONE: Final[str] = "done"
FRAME_ERROR: Final[str] = "error"

#: WebSocket 子协议名（握手时客户端必须带上它，服务端据此确认「对面是本项目的前端」）
WS_SUBPROTOCOL: Final[str] = "shopsage.v1"

#: 令牌在子协议里的前缀。浏览器**不能**给 WebSocket 自定义请求头，所以令牌只能
#: 走子协议（或 URL 查询串）—— 子协议优于查询串：查询串会进 access log、会留在历史记录里。
WS_TOKEN_PREFIX: Final[str] = "bearer."

#: WebSocket 关闭码（4000–4999 是应用自定义段）。前端据此区分「该重新登录」与「该重试」。
WS_CLOSE_UNAUTHORIZED: Final[int] = 4401
WS_CLOSE_RATE_LIMITED: Final[int] = 4429
WS_CLOSE_BAD_REQUEST: Final[int] = 4400

#: 请求 ID 响应头（排障时用它把一条用户反馈对到日志）
REQUEST_ID_HEADER: Final[str] = "X-Request-Id"


class ChatRequest(BaseModel):
    """`POST /api/chat` 的请求体。

    **为什么没有 `user_id` / `role` 字段**：身份只能来自**已验签的令牌**。
    请求体里放一个 `user_id` 就等于「让被攻击的对象自己报身份证」，
    是 D1 越权最经典的入口。角色同理（否则谁都能自称 `admin` 去读全站订单）。

    **为什么没有 `thread_id`**：它是 checkpoint 的隔离键（`8.graph.md` §4 第 3 条），
    由服务端从「鉴权结果 + 请求里的 `session_id`」派生。客户端能任选 `thread_id`
    就等于能读写别人的会话状态（那是比越权查询更严重的一类问题：
    读到的可能是别人上一轮的回答）。
    """

    model_config = ConfigDict(extra="forbid")  # 写错字段名要 422，不要静默忽略

    message: str = Field(
        min_length=1,
        max_length=2000,
        description="用户这一轮说的话（纯文本）",
    )
    session_id: str | None = Field(
        default=None,
        pattern=SESSION_ID_PATTERN,
        description=(
            "会话 id：同一会话的多轮请求带同一个值，服务端据此复用对话历史与 checkpoint。"
            f"缺省 = 服务端生成一个（响应里回传）。允许字符：{SESSION_ID_PATTERN}"
        ),
    )
    stream: bool = Field(
        default=False,
        description="是否希望流式（仅作提示；真正的流式走 WS /api/chat/stream）",
    )


class ApprovalDecision(BaseModel):
    """人工审批结果（`POST /api/chat/approve` 的请求体）。"""

    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=200, description="挂起那一轮的 thread_id（原样回传）")
    approved: bool = Field(description="是否批准继续（False = 按未执行处理，仍会得到一句如实的话术）")
    reason: str | None = Field(default=None, max_length=500, description="审批意见（进审计日志）")


class ApprovalGate(BaseModel):
    """敏感操作的审批闸门信息（`flow_state="approval_required"` 时才有）。"""

    thread_id: str
    question: str
    interrupt_id: str = ""
    #: 可执行的下一步提示（把「调用哪个接口、带什么」写进响应里，前端不用去翻文档）
    resume_with: str = "POST /api/chat/approve"


class ChatDebug(BaseModel):
    """排障信息。**默认只在 `APP_DEBUG_PAYLOAD` 打开时填充**（见 `app/api/settings.py`）。

    为什么要有个开关而不是一直带上：`visited` / `sql` 这类字段对前端是有用的，
    但它们同时暴露了内部结构（走过哪些节点、用的什么 SQL）。对客默认关闭、
    内部与开发环境打开 —— 这与「脱敏按角色分级」是同一条纪律。
    """

    model_config = ConfigDict(extra="allow")

    visited: list[str] = Field(default_factory=list, description="这一轮真正执行过的图节点（按顺序）")
    replayed: list[str] = Field(default_factory=list, description="从断点重放过的节点")
    exhausted: bool = Field(default=False, description="重放预算用尽（这一轮「没做到」而不是「答完了」）")
    guardrail_stage: str = ""
    guardrail_rules: list[str] = Field(default_factory=list)
    row_count: int | None = None
    truncated: bool | None = None
    sql: str | None = Field(default=None, description="本轮执行的 SQL（仅内部角色可见）")


class ChatResponse(BaseModel):
    """`POST /api/chat` 与批准通道的统一响应信封。"""

    model_config = ConfigDict(extra="allow")

    answer: str
    flow_state: FlowState
    thread_id: str
    session_id: str
    request_id: str = ""
    route: str = ""
    blocked: bool = False
    citations: list[str] = Field(default_factory=list)
    refusals: list[str] = Field(default_factory=list)
    #: text 模式下的回答类型（`tools/chart.py` 的 spec 会走 chart 帧/字段）
    chart_spec: dict[str, Any] | None = None
    approval: ApprovalGate | None = None
    debug: ChatDebug | None = None


class ComponentHealth(BaseModel):
    """单个依赖的健康状态。

    `status` 的三个取值（第 11 步补上 `degraded`）：

    * `ok` —— 正常；
    * `degraded` —— **能用，但有代价**（例如限流退化到内存：多副本下额度被放大）；
    * `skipped` —— 这个依赖没启用（`status` 不该报 down，见 `health.py` 的说明）；
    * `down` —— 真的不可用。

    ⚠️ 第 11 步之前这里只有 `ok`/`down`/`skipped`，而 `HealthResponse.status` 早就有
    `degraded` —— 于是「整体 degraded、但组件里说不出是谁 degraded」，
    排障时只能反过来猜。补上它是让两层的词汇表一致（**同一个词在两层都可用**）。
    """

    model_config = ConfigDict(extra="allow")

    name: str
    status: Literal["ok", "degraded", "down", "skipped"]
    detail: str = ""
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    """`GET /health` 的响应体。

    ⚠️ **HTTP 状态码恒为 200**，依赖故障体现在 `components` 与 `status` 上。
    理由：`/health` 同时被「存活探针」和「就绪探针」使用，而存活探针在依赖故障时
    返回非 2xx 会导致编排系统**重启一个本身没问题的进程**（把依赖故障放大成全站重启）。
    要「依赖不通就别切流量」的话，用 `components` 里的状态做决策（第 19 步的部署编排）。
    """

    model_config = ConfigDict(extra="allow")

    status: Literal["ok", "degraded", "down"]
    service: str = "shopsage-api"
    env: str = "dev"
    version: str = ""
    request_id: str = ""
    components: list[ComponentHealth] = Field(default_factory=list)


class ErrorDetail(BaseModel):
    """错误信封的内层。`code` 是**稳定的机器可读标识**，`message` 是给人看的。

    为什么要有 `code`：前端要按错误类型分支（401 去登录、429 退避、403 提示无权），
    靠 `message`（中文、随时可能改）做分支是脆的。
    """

    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """所有 4xx/5xx 的统一错误信封。"""

    error: ErrorDetail
    request_id: str = ""


class Frame(BaseModel):
    """WebSocket 帧。

    四类业务帧 + 一类错误帧，**形状统一**（都有 `event` / `seq` / `ts`）：

    | `event` | 什么时候发 | `data` 里有什么 |
    |---|---|---|
    | `skeleton` | 连接建立、鉴权通过后**立刻** | `route`(可空)、`steps`（固定骨架）、`text`（骨架文本） |
    | `node` | 图每完成一个节点 | `node`、该节点的增量（已裁剪，见 `app/api/chat.py`） |
    | `answer` | 合成出回答时 | `text`、`done`、`degraded`、`citations` |
    | `done` | 一轮结束 | `flow_state`、`thread_id`、`latency_ms`、`first_frame_ms` |
    | `error` | 任何失败 | `code`、`message` |

    `seq` 从 1 单调递增：前端据此发现丢帧（WebSocket 是可靠有序的，所以丢帧只可能来自
    客户端自己的处理逻辑 —— 有了 `seq` 才看得出这种问题）。
    """

    event: Literal["skeleton", "node", "answer", "done", "error"]
    seq: int
    ts: float
    data: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "FRAME_ANSWER",
    "FRAME_DONE",
    "FRAME_ERROR",
    "FRAME_NODE",
    "FRAME_SKELETON",
    "REQUEST_ID_HEADER",
    "SESSION_ID_PATTERN",
    "WS_CLOSE_BAD_REQUEST",
    "WS_CLOSE_RATE_LIMITED",
    "WS_CLOSE_UNAUTHORIZED",
    "WS_SUBPROTOCOL",
    "WS_TOKEN_PREFIX",
    "ApprovalDecision",
    "ApprovalGate",
    "ChatDebug",
    "ChatRequest",
    "ChatResponse",
    "ComponentHealth",
    "ErrorDetail",
    "ErrorResponse",
    "FlowState",
    "Frame",
    "HealthResponse",
]
