/**
 * 后端契约的**前端镜像**（唯一一处）。
 *
 * 为什么要单独一个模块，而不是「用到哪就在哪写 interface」：
 * 第 9 步的 `app/api/schemas.py` 已经把「帧格式与错误格式只能有一份」写成了纪律，
 * 前端这一份是它的**对端**。两份对不上的表现是「偶尔解析不出来」——
 * 最难查的那类问题。所以这里：
 *
 * 1. 取值域（`flow_state`、帧 `event`、关闭码、子协议名）全部从后端抄成**字面量联合类型**，
 *    编译期就能发现「后端有 `degraded` 而前端没分支」；
 * 2. `parseFrame()` 是**唯一的入口**：所有来自网络的数据都先过它，
 *    而不是在组件里 `as Frame`（`as` 只是让编译器闭嘴，运行期该崩还是崩）。
 *
 * ⚠️ 镜像的代价是「后端改了前端要跟着改」。这是**有意的**：
 * 本步是演示层，要的是「一眼看出前后端契约是不是同一份」，
 * 而不是引入代码生成（`openapi-typescript` 那套）—— 那是第 19 步之后的事。
 */

// ── 取值域（与 app/api/schemas.py 逐字对应）──────────────────────────────────

/** 一轮业务的状态（HTTP 状态码回答「调用成不成」，它回答「这一轮做到哪一步」）。 */
export type FlowState = "delivered" | "blocked" | "approval_required";
/** WebSocket 帧的五类 `event`。 */
export type FrameEvent = "skeleton" | "node" | "answer" | "done" | "error";

/** WebSocket 子协议名（握手时**必须**带上，服务端据此确认对面是本项目前端）。 */
export const WS_SUBPROTOCOL = "shopsage.v1";

/** 令牌在子协议里的前缀（浏览器不能给 WebSocket 自定义请求头）。 */
export const WS_TOKEN_PREFIX = "bearer.";

/** 应用自定义关闭码（前端据此区分「该重新登录」与「该退避重试」）。 */
export const WS_CLOSE_UNAUTHORIZED = 4401;
export const WS_CLOSE_RATE_LIMITED = 4429;
export const WS_CLOSE_BAD_REQUEST = 4400;

/** 会话 id 的字符集（与后端 `SESSION_ID_PATTERN` 同源）。 */
export const SESSION_ID_PATTERN = /^[A-Za-z0-9_.\-]{1,64}$/;

/** `message` 的长度上限（后端 422 的判据；前端先行截断，省一次往返）。 */
export const MESSAGE_MAX_LENGTH = 2000;

// ── 数据形状 ────────────────────────────────────────────────────────────────

/** 源文档出处的**解析结果**：把 `退货政策 p.3#c12` 拆成可展示、可定位的三段。 */
export interface ParsedCitation {
  /** 原样字符串（回传、排障、与后端日志对齐时用它） */
  raw: string;
  /** 来源文档名（如 `退货政策.md`） */
  document: string;
  /** 页码（`p.3` → `3`），没有则为 null */
  page: number | null;
  /** 片段 id（`#c12` → `c12`），没有则为 null */
  chunkId: string | null;
}

/**
 * 敏感操作的审批闸门（`flow_state="approval_required"` 时才有）。
 *
 * ⚠️ **变更 003 之后它在生产路径上不可达**：对客闸门整体移除
 * （`GATED_ROLES` 置空），图里不再注册闸门节点。
 *
 * 契约**不收缩**（决策 D2：保留图侧原语），所以前端仍然解析它 ——
 * 万一将来给另一条业务加对话轮级审批，前端不用改解析。
 * 但**发起审批的那个接口已经删除了**（`POST /api/chat/approve` 允许发起人批准自己），
 * 所以 `resumeWith` 指向的是**单据级**审批入口。
 */
export interface ApprovalGate {
  threadId: string;
  question: string;
  interruptId: string;
  resumeWith: string;
}

/**
 * 退款申请的结果（变更 003；`route=refund` 的那一轮才有）。
 *
 * ⚠️ 两种形状用 `status` 区分，**必须分开处理**：
 *   * `submitted` → 已经落库，`refund_id` / `amount` / `state` 一定有值；
 *   * `needs_order_id` → **没有落库**，`candidates` 是候选订单号。
 * 把两者混着渲染（例如无条件显示「已提交」）会让客户以为申请成功了，
 * 而实际上系统还在等他确认是哪一笔。
 */
export interface RefundOutcome {
  status: "submitted" | "needs_order_id" | "refused";
  refund_id?: number;
  order_id?: number;
  amount?: string;
  state?: string;
  remaining_after?: string;
  candidates?: number[];
  hint?: string;
  /** 幂等命中（图重放或重复提问）：这一单之前就提交过 */
  idempotent_hit?: boolean;
}

export interface ChatDebug {
  visited: string[];
  replayed: string[];
  exhausted: boolean;
  guardrailStage: string;
  guardrailRules: string[];
  rowCount: number | null;
  truncated: boolean | null;
  sql: string | null;
}

// ── 变更 003：订单与退款（与 app/api/schemas.py 的响应模型逐字对应）──────────

/** 订单里的一条退款记录（只读展示）。 */
export interface OrderRefund {
  refund_id: number;
  /** 金额是**十进制字符串**（后端不过 JSON float，避免二进制尾巴） */
  amount: string;
  status: RefundStatus;
  reason: string | null;
  decision_note: string | null;
  created_at: string | null;
  decided_at: string | null;
}

/** 退款单的四种状态（与后端 `models.REFUND_STATUSES` 同源）。 */
export type RefundStatus = "requested" | "approved" | "rejected" | "refunded";

export const REFUND_STATUSES: readonly RefundStatus[] = [
  "requested",
  "approved",
  "rejected",
  "refunded",
];

/** 退款状态的中文标签（**唯一一处**映射：订单页与审批台共用）。 */
export function refundStatusLabel(status: string): string {
  switch (status) {
    case "requested":
      return "待审批";
    case "approved":
      return "已通过";
    case "rejected":
      return "已驳回";
    case "refunded":
      return "已退款";
    default:
      return status || "未知";
  }
}

export interface OrderItem {
  order_item_id: number;
  product_id: number;
  product_name: string | null;
  quantity: number;
  unit_price: string;
}

export interface Order {
  order_id: number;
  status: string;
  total_amount: string;
  /** ⚠️ 按角色脱敏：清晰度不足时是 `***`（**不是** null —— 两者语义不同） */
  payment_method: string | null;
  created_at: string | null;
  items: OrderItem[];
  refunds: OrderRefund[];
  consumed_amount: string;
  /** 还能退多少 = 订单金额 − 已占用（`rejected` 不计入） */
  remaining_refundable: string;
  /** 此刻能不能申请退款（状态可退 **且** 还有额度） */
  refundable: boolean;
}

export interface OrderList {
  orders: Order[];
  total: number;
  limit: number;
}

/** 一张退款单的完整形态（客户订单页与员工审批台共用）。 */
export interface Refund {
  refund_id: number;
  order_id: number;
  user_id: number;
  amount: string;
  status: RefundStatus;
  reason: string | null;
  created_at: string | null;
  /** 审批人（`employees.emp_id`）；非员工主体审批时为 null */
  decided_by: number | null;
  decided_at: string | null;
  decision_note: string | null;
  /** 审批时的角色**快照** */
  approver_role: string | null;
}

export interface RefundList {
  refunds: Refund[];
  total: number;
}

/** 订单状态的中文标签（**唯一一处**映射；认不出来时原样显示，不猜）。 */
export function orderStatusLabel(status: string): string {
  switch (status) {
    case "pending":
      return "待付款";
    case "paid":
      return "已付款";
    case "shipped":
      return "已发货";
    case "delivered":
      return "已送达";
    case "cancelled":
      return "已取消";
    case "refunded":
      return "已退款";
    default:
      return status || "未知";
  }
}

/** 图表 spec（与 `app/tools/chart.py::build_chart_spec` 的产物同形）。 */
export interface ChartSpec {
  schema: string;
  type: "bar" | "line" | "pie" | "scatter" | "table";
  title: string;
  x: { field: string; label: string };
  series: { name: string; field: string; type: string }[];
  data: Record<string, string | number | null>[];
  meta: {
    audit_id: string;
    caller: string;
    source_rows: number;
    points: number;
    truncated_points: boolean;
    null_values: number;
    sorted_by: string | null;
    descending: boolean;
    warnings: string[];
  };
}

/** `POST /api/chat` 与批准通道的统一响应信封。 */
export interface ChatResponse {
  answer: string;
  flow_state: FlowState;
  thread_id: string;
  session_id: string;
  request_id: string;
  route: string;
  blocked: boolean;
  citations: string[];
  refusals: string[];
  chart_spec: ChartSpec | null;
  approval: ApprovalGate | null;
  /** 变更 003：退款申请的结果（只有 `route=refund` 的那一轮才有） */
  refund: RefundOutcome | null;
  debug: ChatDebug | null;
}

/** 服务端发来的 WebSocket 帧。 */
export interface Frame {
  event: FrameEvent;
  seq: number;
  ts: number;
  data: Record<string, unknown>;
}

/** `GET /health` 里单个依赖的状态。 */
export interface ComponentHealth {
  name: string;
  status: "ok" | "degraded" | "down" | "skipped";
  detail: string;
  latency_ms: number | null;
}

// ── 解析（来自网络的任何值都要过这里）───────────────────────────────────────

/** 解析失败时抛这个：**带上原始值**，否则排障时只能靠猜（§10 坑 3）。 */
export class ContractError extends Error {
  readonly raw: unknown;

  constructor(message: string, raw: unknown) {
    super(message);
    this.name = "ContractError";
    this.raw = raw;
  }
}

const FRAME_EVENTS: readonly FrameEvent[] = ["skeleton", "node", "answer", "done", "error"];
const FLOW_STATES: readonly FlowState[] = ["delivered", "blocked", "approval_required"];

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ContractError(`${what} 不是对象：${JSON.stringify(value)}`, value);
  }
  return value as Record<string, unknown>;
}

function asString(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function asNumberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asBool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

/**
 * `JSON.parse` 的安全版：**先判形状再断言**。
 *
 * ⚠️ 这一层不是洁癖：WebSocket 上收到的任何东西都可能不是合法 JSON
 * （代理返回的 HTML 错误页、被中间设备改写的内容），
 * 而 `JSON.parse` 在组件渲染里抛出会直接白屏。
 */
export function parseFrame(raw: string | unknown): Frame {
  let value: unknown = raw;
  if (typeof raw === "string") {
    try {
      value = JSON.parse(raw);
    } catch (exc) {
      throw new ContractError(`帧不是合法 JSON：${(exc as Error).message}`, raw);
    }
  }
  const record = asRecord(value, "帧");
  const event = asString(record.event);
  if (!FRAME_EVENTS.includes(event as FrameEvent)) {
    throw new ContractError(`未知的帧类型 ${JSON.stringify(event)}（后端新增了 event 而前端没跟上？）`, value);
  }
  const seq = record.seq;
  if (typeof seq !== "number" || !Number.isInteger(seq) || seq < 1) {
    throw new ContractError(`帧的 seq 必须是 >= 1 的整数，收到 ${JSON.stringify(seq)}`, value);
  }
  return {
    event: event as FrameEvent,
    seq,
    ts: typeof record.ts === "number" ? record.ts : 0,
    data: typeof record.data === "object" && record.data !== null ? (record.data as Record<string, unknown>) : {},
  };
}

export function parseFlowState(value: unknown): FlowState {
  const text = asString(value);
  if (!FLOW_STATES.includes(text as FlowState)) {
    throw new ContractError(`未知的 flow_state ${JSON.stringify(text)}`, value);
  }
  return text as FlowState;
}

export function parseChatResponse(value: unknown): ChatResponse {
  const record = asRecord(value, "响应信封");
  const approvalRaw = record.approval;
  const debugRaw = record.debug;
  return {
    answer: asString(record.answer),
    flow_state: parseFlowState(record.flow_state),
    thread_id: asString(record.thread_id),
    session_id: asString(record.session_id),
    request_id: asString(record.request_id),
    route: asString(record.route),
    blocked: asBool(record.blocked),
    citations: asStringList(record.citations),
    refusals: asStringList(record.refusals),
    chart_spec: isChartSpec(record.chart_spec) ? (record.chart_spec as ChartSpec) : null,
    approval: isApprovalGate(approvalRaw)
      ? {
          threadId: asString(approvalRaw.thread_id),
          question: asString(approvalRaw.question),
          interruptId: asString(approvalRaw.interrupt_id),
          resumeWith: asString(approvalRaw.resume_with, "POST /api/refunds/decision"),
        }
      : null,
    refund: parseRefundOutcome(record.refund),
    debug: isChatDebug(debugRaw)
      ? {
          visited: asStringList(debugRaw.visited),
          replayed: asStringList(debugRaw.replayed),
          exhausted: asBool(debugRaw.exhausted),
          guardrailStage: asString(debugRaw.guardrail_stage),
          guardrailRules: asStringList(debugRaw.guardrail_rules),
          rowCount: asNumberOrNull(debugRaw.row_count),
          truncated: typeof debugRaw.truncated === "boolean" ? debugRaw.truncated : null,
          sql: typeof debugRaw.sql === "string" ? debugRaw.sql : null,
        }
      : null,
  };
}

/**
 * 解析退款结果；形状不认识时**返回 null 而不是抛**。
 *
 * 理由与 `parseCitation` 的兜底同源：这一块解析不出来只该让「退款卡片」不显示，
 * 不该让整轮对话的渲染崩掉（`answer` 仍然是有价值的）。
 * ⚠️ 但 `status` 必须在白名单里 —— 我们不认识的状态宁可当成「没有退款结果」，
 * 也不要瞎猜一个卡片出来（那会显示错的信息，比不显示更糟）。
 */
export function parseRefundOutcome(value: unknown): RefundOutcome | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const status = asString(record.status);
  if (status !== "submitted" && status !== "needs_order_id" && status !== "refused") return null;
  const candidates = Array.isArray(record.candidates)
    ? record.candidates
        .map((item) => (typeof item === "number" ? item : Number(item)))
        .filter((item) => Number.isFinite(item))
    : undefined;
  return {
    status,
    refund_id: typeof record.refund_id === "number" ? record.refund_id : undefined,
    order_id: typeof record.order_id === "number" ? record.order_id : undefined,
    amount: typeof record.amount === "string" ? record.amount : undefined,
    state: typeof record.state === "string" ? record.state : undefined,
    remaining_after:
      typeof record.remaining_after === "string" ? record.remaining_after : undefined,
    candidates,
    hint: typeof record.hint === "string" ? record.hint : undefined,
    idempotent_hit: typeof record.idempotent_hit === "boolean" ? record.idempotent_hit : undefined,
  };
}

/** 解析订单列表（`GET /api/orders`）。字段缺失时给**安全默认**，不抛。 */
export function parseOrderList(value: unknown): OrderList {
  const record = asRecord(value, "订单列表");
  const raw = Array.isArray(record.orders) ? record.orders : [];
  return {
    orders: raw.map(parseOrder),
    total: typeof record.total === "number" ? record.total : raw.length,
    limit: typeof record.limit === "number" ? record.limit : raw.length,
  };
}

export function parseOrder(value: unknown): Order {
  const record = asRecord(value, "订单");
  return {
    order_id: Number(record.order_id ?? 0),
    status: asString(record.status),
    total_amount: asString(record.total_amount, "0.00"),
    // ⚠️ **不能**用 `asString(..., "")` 兜底：`null`（没有支付方式）与
    // `"***"`（有但看不到）是两种不同的语义，必须原样保留。
    payment_method: typeof record.payment_method === "string" ? record.payment_method : null,
    created_at: typeof record.created_at === "string" ? record.created_at : null,
    items: Array.isArray(record.items) ? record.items.map(parseOrderItem) : [],
    refunds: Array.isArray(record.refunds) ? record.refunds.map(parseOrderRefund) : [],
    consumed_amount: asString(record.consumed_amount, "0.00"),
    remaining_refundable: asString(record.remaining_refundable, "0.00"),
    refundable: asBool(record.refundable),
  };
}

function parseOrderItem(value: unknown): OrderItem {
  const record = asRecord(value, "订单明细");
  return {
    order_item_id: Number(record.order_item_id ?? 0),
    product_id: Number(record.product_id ?? 0),
    product_name: typeof record.product_name === "string" ? record.product_name : null,
    quantity: Number(record.quantity ?? 0),
    unit_price: asString(record.unit_price, "0.00"),
  };
}

function parseOrderRefund(value: unknown): OrderRefund {
  const record = asRecord(value, "订单退款记录");
  return {
    refund_id: Number(record.refund_id ?? 0),
    amount: asString(record.amount, "0.00"),
    status: asString(record.status) as RefundStatus,
    reason: typeof record.reason === "string" ? record.reason : null,
    decision_note: typeof record.decision_note === "string" ? record.decision_note : null,
    created_at: typeof record.created_at === "string" ? record.created_at : null,
    decided_at: typeof record.decided_at === "string" ? record.decided_at : null,
  };
}

/** 解析退款列表（`GET /api/refunds`，员工审批台）。 */
export function parseRefundList(value: unknown): RefundList {
  const record = asRecord(value, "退款列表");
  const raw = Array.isArray(record.refunds) ? record.refunds : [];
  return {
    refunds: raw.map(parseRefund),
    total: typeof record.total === "number" ? record.total : raw.length,
  };
}

export function parseRefund(value: unknown): Refund {
  const record = asRecord(value, "退款单");
  return {
    refund_id: Number(record.refund_id ?? 0),
    order_id: Number(record.order_id ?? 0),
    user_id: Number(record.user_id ?? 0),
    amount: asString(record.amount, "0.00"),
    status: asString(record.status) as RefundStatus,
    reason: typeof record.reason === "string" ? record.reason : null,
    created_at: typeof record.created_at === "string" ? record.created_at : null,
    decided_by: typeof record.decided_by === "number" ? record.decided_by : null,
    decided_at: typeof record.decided_at === "string" ? record.decided_at : null,
    decision_note: typeof record.decision_note === "string" ? record.decision_note : null,
    approver_role: typeof record.approver_role === "string" ? record.approver_role : null,
  };
}

function isChartSpec(value: unknown): boolean {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.type === "string" && Array.isArray(record.data) && Array.isArray(record.series);
}

function isApprovalGate(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && typeof (value as Record<string, unknown>).thread_id === "string";
}

function isChatDebug(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * 出处串 → 结构化信息（`退货政策.md p.3#c12`）。
 *
 * 格式的**唯一真相在后端**（`app/tools/retriever.py::RetrievedDoc.citation()`：
 * `来源 [p.N] [#chunk]`）。前端解析它的理由见 `docs/runbook/16.web-runbook.md` §4.3：
 * 「来源文档 + 片段」要能分别展示、分别定位，而 `p.3` 与 `c12` 混在一串里做不到。
 *
 * ⚠️ `source` 的真实取值是**相对仓库根的路径**（实测：`docs/kb/退货政策.md`，
 * 不是光秃秃的文件名 —— 这一点是查了 `rag_chunks` 表才知道的，见 §10.1 坑 6）。
 * 展示与跳转只需要文件名，所以这里最后取一次 basename。
 *
 * 解析失败的兜底**必须保留原文**（返回 `{document: raw}`）：
 * 出处解析不出来是**展示层的问题**，不该让用户看不到来源 ——
 * 来源显示不出来比显示得不好看严重得多。
 */
export function parseCitation(raw: string): ParsedCitation {
  const text = String(raw ?? "").trim();
  const pageMatch = text.match(/\bp\.(\d+)\b/);
  const chunkMatch = text.match(/#([A-Za-z0-9_-]+)\b/);
  let document = text;
  for (const piece of [pageMatch?.[0], chunkMatch?.[0]]) {
    if (piece) document = document.replace(piece, "");
  }
  document = document.replace(/\s+/g, " ").trim();
  // `docs/kb/退货政策.md` → `退货政策.md`（正反斜杠都处理：来源可能是 Windows 侧的路径）
  const base = document.split(/[\\/]/).pop() ?? document;
  return {
    raw: text,
    document: base || document,
    page: pageMatch ? Number(pageMatch[1]) : null,
    chunkId: chunkMatch ? chunkMatch[1] : null,
  };
}

export function parseCitations(raw: readonly string[]): ParsedCitation[] {
  return raw.map(parseCitation);
}
