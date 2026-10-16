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

# 口令长度下限的**唯一真相**在 `app.security.accounts`（业务层），这里只引用它，
# 不重新写一个数字 —— 两处各写一个「至少 8 位/至少 12 位」迟早会漂移。
from app.security.accounts import MIN_PASSWORD_LENGTH

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


# ── 第 12 步：长任务的请求/响应契约 ───────────────────────────────────────────


class TaskRequest(BaseModel):
    """`POST /api/tasks` 的请求体（提交一个长任务）。

    **为什么没有 `user_id` / `role`**：与 `ChatRequest` 同一条纪律 ——
    身份只能来自已验签的令牌。任务结果会按发起人做归属判定
    （`GET /api/tasks/{job_id}` 的越权检查），请求体里放一个 `user_id`
    就等于「让调用方自己声明结果是给谁的」。

    **为什么没有 `thread_id`**：`agent` 任务的 `thread_id` 由服务端从
    「令牌 + `session_id`」派生（与 `POST /api/chat` 完全同一条规则）。
    客户端能自己指定 `thread_id` 就等于能推进别人的会话。

    **为什么没有 `idempotency_key` 的强制要求**：不传时服务端按
    「任务种类 + user_id + 参数」派生一个稳定键（`queue.jobs.derive_key`），
    于是「客户端网络抖动后重发一次」不会变成两个任务。传了就原样用 ——
    那是「我知道这次请求的业务幂等键」的场景（例如与上游单据号对齐）。
    """

    model_config = ConfigDict(extra="forbid")  # 写错字段名要 422，不要静默忽略

    kind: Literal["report", "agent", "kb_rebuild"] = Field(
        description="任务种类：`report`（报表）/ `agent`（长链路 Agent）/ `kb_rebuild`（知识库重建）"
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "任务入参（按 kind 不同）："
            "report 用 `title` / `sql` / `params`；agent 用 `message`；kb_rebuild 用 `source_dir` / `force`"
        ),
    )
    session_id: str | None = Field(
        default=None,
        pattern=SESSION_ID_PATTERN,
        description="仅 `agent` 任务用：决定 thread_id（与 POST /api/chat 同一条派生规则）",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=64,
        description="业务幂等键（可选）：同一个键只执行一次。缺省 = 服务端按参数派生",
    )
    defer_by: float = Field(
        default=0.0,
        ge=0.0,
        le=30 * 24 * 3600.0,
        description="延迟多少秒执行（0 = 立刻入队；上限 30 天，更远的定时任务请用 cron）",
    )

    def enqueue_kwargs(self, actor: Any) -> dict[str, Any]:
        """把请求体翻成 `enqueue_*` 的关键字参数（**唯一的翻译点**）。

        `agent` 任务需要 `thread_id`，其余任务不需要 —— 这条分支写在这里而不是
        三个路由里，因为「哪一个任务需要会话」是**契约**的一部分，
        而它一旦漂移（比如 report 也悄悄接受 thread_id）就没人看得出来。

        ⚠️ 本方法**不做授权判断**：`kb_rebuild` 是内部专属任务，而那个判断在
        `app/api/tasks.py` 里做（授权是路由的职责，本模块只描述「请求长什么样」）。
        第一版把它写在这里，结果是「客户提交 kb_rebuild → `PermissionError` 冒到 500」——
        一个本该是 403 的响应变成了「服务端未知错误」（见 runbook §10.1）。
        """
        common: dict[str, Any] = {
            "idempotency_key": self.idempotency_key,
            "defer_by": float(self.defer_by),
        }
        params = dict(self.params)
        if self.kind == "agent":
            message = str(params.pop("message", "") or "").strip()
            if not message:
                raise ValueError("agent 任务必须在 params 里给 message（这一轮要回答什么）")
            return {
                **common,
                "user_id": int(actor.user_id),
                "thread_id": str(actor.thread_id),
                "message": message,
                "role": str(actor.role),
                "replay_budget": int(params.pop("replay_budget", 1) or 1),
            }
        if self.kind == "report":
            return {
                **common,
                "user_id": int(actor.user_id),
                "title": str(params.pop("title", "") or "订单报表"),
                "params": params,
            }
        # kb_rebuild：知识库是全局资源（授权判断在路由里，见上面的说明）
        return {
            **common,
            "user_id": int(actor.user_id),
            "source_dir": str(params.pop("source_dir", "") or ""),
            "force": bool(params.pop("force", False)),
        }


class TaskAcceptedResponse(BaseModel):
    """`POST /api/tasks` 的响应（**202 Accepted**：已受理，还没完成）。

    三个字段回答客户端最关心的三件事：**凭据**（`job_id`）、
    **要不要重新等**（`created`）、**去哪查**（`status_url`）。
    """

    job_id: str = Field(description="任务凭据：用它查状态、也用它做幂等（同一个键只执行一次）")
    kind: str
    status: str = Field(description="`queued`（立刻可取）或 `deferred`（延迟任务，还没到点）")
    created: bool = Field(description="True = 本次真的投了新任务；False = 幂等命中（之前已投过）")
    deferred_seconds: float = 0.0
    poll_after_seconds: int = Field(default=5, description="建议的轮询间隔（秒）")
    status_url: str = Field(description="查状态的相对路径（前缀与 API_PREFIX 一致）")


class DeadLetterView(BaseModel):
    """死信任务的失败现场（**给运维看**，不含 traceback 全文）。

    ⚠️ 刻意回传 `error_type` 与截断后的 `error_message`，而**不**回传 `error_traceback`：
    traceback 里可能带业务数据（SQL 文本、行内容），而它进 HTTP 响应就意味着出网。
    要全文请去库里查 `queue_dead_letters.error_traceback`（那是有审计的入口）。
    """

    attempts: int = 1
    error_type: str = ""
    error_message: str = ""
    retryable: bool = False
    failed_at: Any = None
    replay_count: int = 0
    resolution: str = ""
    replay_hint: str = ""


class TaskStatusResponse(BaseModel):
    """`GET /api/tasks/{job_id}` 的响应。"""

    job_id: str
    status: str = Field(description="queued / deferred / in_progress / complete / not_found / dead_letter")
    description: str = Field(description="状态的中文说明（前端可直接展示）")
    result: dict[str, Any] | None = Field(
        default=None, description="完成后的结果（**只保留 24 小时**；过期后为 null 但 status 仍是 complete）"
    )
    dead_letter: DeadLetterView | None = Field(default=None, description="进死信时的失败现场")


class QueueStatusResponse(BaseModel):
    """`GET /api/tasks/queue` 的响应（队列现状，给人看的读数）。"""

    depth: int = Field(description="立刻可取的积压条数")
    deferred: int = Field(description="延迟任务条数（还没到执行时间，**不算积压**）")
    backlog: int = Field(description="`depth + deferred`")
    dead_letters: int = Field(description="待处理死信条数（人工兜底的入口）")
    oldest_dead_letter_seconds: float | None = Field(
        default=None, description="最老的一条待处理死信躺了多久（**它才值得告警**）"
    )
    scale_hint: str = Field(description="给扩容决策的一句话（背压信号）")
    kinds: list[str] = Field(default_factory=list, description="登记过的任务种类")


# ── 账号接口的请求/响应（变更 001）───────────────────────────────────────────
#
# 三条贯穿这一节的口径：
#
# 1. **请求体里绝不出现调用者的 `role` / `user_id` / `account_id`**：身份只能来自
#    「凭据校验」或「已验签令牌」。请求体里放一个 `role` 就等于让调用方自报权限
#    （`ChatRequest` 的 docstring 已经为同一件事给过理由）。管理接口里的 `role` 是
#    **被操作对象的目标值**，不是调用者的身份 —— 这个区别必须写清楚，否则下一个人
#    会照着 `/api/chat` 的样子在请求体里加一个 `role`。
# 2. **响应里绝不出现 `password_hash` / `token_version`**：前者是凭据本身，
#    后者是「令牌作废计数」（暴露它等于告诉攻击者这个账号最近被改过几次）。
#    这条与 `app/security/accounts.AccountView` 的字段集**同源**（那边是真相）。
# 3. `entry` 由服务端算好返回（`accounts.entry_for_role`），前端不复制分流规则 ——
#    副本必然漂移（`web/src/lib/auth.ts` 自己已经标注「这是副本不是真相」）。


class LoginRequest(BaseModel):
    """`POST /api/auth/login` 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(
        min_length=1,
        max_length=64,
        description="登录标识：员工填用户名（`zhangsan`），客户填手机号。大小写不敏感。",
    )
    password: str = Field(
        min_length=1,
        max_length=200,
        description="口令。**上限是刻意的**：无限长的口令会让每次 Argon2 计算变成一次 DoS 放大器。",
    )


class TokenResponse(BaseModel):
    """登录 / 刷新成功后的令牌信封。"""

    access_token: str = Field(description="访问令牌（`Authorization: Bearer <它>`）")
    refresh_token: str = Field(
        description="刷新令牌：**不能**直接访问业务接口（会被 401），只能拿去换新的访问令牌"
    )
    token_type: str = Field(default="bearer", description="固定 `bearer`（OAuth2 惯例）")
    expires_in: int = Field(description="访问令牌还有多少秒过期（前端据此提前续期）")
    refresh_expires_in: int = Field(description="刷新令牌还有多少秒过期")
    account_id: int = Field(description="账号主键（审计/排障用；**不是**业务主体 id）")
    subject: str = Field(
        description="主体标识串（`c2` 客户 / `e1` 员工）：`thread_id` 的前缀就是它"
    )
    subject_type: str = Field(description="`customer` / `employee`")
    role: str = Field(description="授权角色（来自 `accounts.role`，不是请求体）")
    entry: str = Field(
        description="登录后该进哪个界面：`admin` / `internal` / `customer`（**服务端算好的**）"
    )
    permissions: list[str] = Field(default_factory=list, description="该角色的权限点（供界面显隐）")


class RefreshRequest(BaseModel):
    """`POST /api/auth/refresh` 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1, description="登录时拿到的刷新令牌")


class AccountViewResponse(BaseModel):
    """账号的对外形态（**没有凭据、没有世代号**，见本节口径 2）。"""

    account_id: int
    identifier: str
    role: str
    status: str = Field(description="`active` / `disabled`")
    subject_type: str
    subject_id: int
    entry: str = Field(description="该角色对应的入口（服务端算的）")
    is_admin: bool = Field(description="是否持有 `admin` 权限点（**按权限点判，不按角色名**）")
    created_at: str | None = Field(default=None, description="ISO8601 字符串（缺省为 null）")
    last_login_at: str | None = Field(default=None, description="最近一次成功登录时间")


class AccountListResponse(BaseModel):
    accounts: list[AccountViewResponse]
    total: int = Field(description="当前筛选条件下的条数")


class AccountCreateRequest(BaseModel):
    """`POST /api/admin/accounts` 的请求体（**管理员开账号**）。"""

    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=1, max_length=64)
    # ⚠️ 长度下限与 `accounts.MIN_PASSWORD_LENGTH` 是**同一个数**（issue 003 的 P1-3）：
    # 修复前这里是 `min_length=1`，实测管理员能用 `password="x"` 建出可登录的账号。
    # schema 这一层的作用是「更早失败 + 进 OpenAPI 文档」；
    # 真正的强度校验（弱口令、与标识相同）在 `accounts.validate_password_strength()`，
    # 因为脚本/后台任务不经过 pydantic。
    password: str = Field(
        min_length=MIN_PASSWORD_LENGTH,
        max_length=200,
        description=f"初始口令（至少 {MIN_PASSWORD_LENGTH} 位，由管理员告知本人）",
    )
    role: str = Field(max_length=32, description="取值必须是 `rbac.ROLE_PERMISSIONS` 的键")
    subject_type: str = Field(description="`customer` 或 `employee`")
    subject_id: int = Field(
        gt=0, description="对应业务主体的主键（`users.user_id` / `employees.emp_id`）"
    )
    status: str = Field(default="active", description="`active` / `disabled`")


class AccountUpdateRequest(BaseModel):
    """`PATCH /api/admin/accounts/{id}` 的请求体：只允许改状态与角色。

    ⚠️ 为什么这里**没有** `password`：改口令是独立的一个动作（`/password` 路径）。
    两者语义不同 —— 改口令要让本人知道新口令，改角色/停用不需要；而且它们触发的
    后果也不同（都会自增 `token_version`，但改口令是「夺回账号」类动作）。
    混在一个请求里会让「我只想停用一下」意外重置别人的口令。
    """

    model_config = ConfigDict(extra="forbid")

    status: str | None = Field(default=None, description="`active` / `disabled`")
    role: str | None = Field(default=None, max_length=32)


class PasswordResetRequest(BaseModel):
    """`POST /api/admin/accounts/{id}/password` 的请求体（管理员重置口令）。"""

    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(
        min_length=MIN_PASSWORD_LENGTH,
        max_length=200,
        description=f"新口令（至少 {MIN_PASSWORD_LENGTH} 位）",
    )


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
    "AccountCreateRequest",
    "AccountListResponse",
    "AccountUpdateRequest",
    "AccountViewResponse",
    "ApprovalDecision",
    "ApprovalGate",
    "ChatDebug",
    "ChatRequest",
    "ChatResponse",
    "ComponentHealth",
    "DeadLetterView",
    "ErrorDetail",
    "ErrorResponse",
    "FlowState",
    "Frame",
    "HealthResponse",
    "LoginRequest",
    "PasswordResetRequest",
    "QueueStatusResponse",
    "RefreshRequest",
    "TaskAcceptedResponse",
    "TaskRequest",
    "TaskStatusResponse",
    "TokenResponse",
]
