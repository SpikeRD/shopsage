/**
 * HTTP 侧的最小客户端：**一套错误解析 + 一次鉴权注入**，没有别的。
 *
 * 三条口径（都与第 9 步的实现对齐）：
 *
 * 1. **错误只有一个形状**：后端所有 4xx/5xx 都是
 *    `{"error": {code, message, detail}, request_id}`（`app/api/errors.py`）。
 *    所以这里只写一套解析；按 `code` 分支、按 `message` 显示 ——
 *    **不要用 `message` 做分支**（中文文案随时会改，而 `code` 是稳定标识）。
 * 2. **身份只来自令牌**：请求体里绝不出现 `user_id` / `role` / `thread_id`
 *    （后端 `ChatRequest` 是 `extra="forbid"`，多写一个字段就是 422）。
 * 3. **错误的 `request_id` 要留住**：排障时它是把「用户反馈的一句话」
 *    对到服务端日志的唯一凭据（后端会回 `X-Request-Id`）。
 */

import { apiUrl } from "./config.ts";
import {
  type ChatResponse,
  ContractError,
  parseChatResponse,
} from "./contracts.ts";

/** 后端错误信封里的 `code` 的已知取值（未知的照样透传，不猜）。 */
export type ApiErrorCode =
  | "unauthorized"
  | "forbidden"
  | "validation_error"
  | "rate_limited"
  | "not_found"
  | "nothing_to_approve"
  | "internal_error"
  | string;

export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;
  readonly detail: Record<string, unknown>;
  readonly requestId: string;
  readonly retryAfter: number | null;

  constructor(init: {
    status: number;
    code: ApiErrorCode;
    message: string;
    detail?: Record<string, unknown>;
    requestId?: string;
    retryAfter?: number | null;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.detail = init.detail ?? {};
    this.requestId = init.requestId ?? "";
    this.retryAfter = init.retryAfter ?? null;
  }

  /** 该不该让用户重新登录（401 是唯一该跳登录页的状态）。 */
  get isUnauthorized(): boolean {
    return this.status === 401 || this.code === "unauthorized";
  }
}

export interface ChatOptions {
  message: string;
  /** 会话 id：同一会话多轮带同一个值；缺省由服务端生成并在响应里回传 */
  sessionId?: string | null;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

export interface ApproveOptions {
  /** **原样回传**挂起那一轮的 `thread_id`（服务端会校验它属不属于这个用户） */
  threadId: string;
  approved: boolean;
  reason?: string;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

function authHeaders(token: string): HeadersInit {
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

/** 把响应的错误体翻成 `ApiError`；**响应不是 JSON 时也要有话说**（代理会回 HTML）。 */
async function toApiError(response: Response): Promise<ApiError> {
  const requestId = response.headers.get("X-Request-Id") ?? "";
  const retryAfterHeader = response.headers.get("Retry-After");
  const retryAfter = retryAfterHeader && !Number.isNaN(Number(retryAfterHeader)) ? Number(retryAfterHeader) : null;
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    return new ApiError({
      status: response.status,
      code: response.status === 401 ? "unauthorized" : "internal_error",
      message: `服务端返回了非 JSON 响应（HTTP ${response.status}）：如果这是反代的错误页，请看反代日志`,
      requestId,
      retryAfter,
    });
  }
  const envelope = payload as { error?: { code?: unknown; message?: unknown; detail?: unknown }; request_id?: unknown };
  const error = envelope?.error ?? {};
  return new ApiError({
    status: response.status,
    code: typeof error.code === "string" ? error.code : "internal_error",
    message: typeof error.message === "string" ? error.message : `HTTP ${response.status}`,
    detail: typeof error.detail === "object" && error.detail !== null ? (error.detail as Record<string, unknown>) : {},
    requestId: typeof envelope.request_id === "string" ? envelope.request_id : requestId,
    retryAfter,
  });
}

async function postJson<T>(
  path: string,
  token: string,
  body: Record<string, unknown>,
  parse: (value: unknown) => T,
  options: { signal?: AbortSignal; fetchImpl?: typeof fetch } = {},
): Promise<T> {
  const doFetch = options.fetchImpl ?? fetch;
  const response = await doFetch(apiUrl(path), {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
    signal: options.signal,
  });
  if (!response.ok) throw await toApiError(response);
  const payload: unknown = await response.json();
  try {
    return parse(payload);
  } catch (exc) {
    if (exc instanceof ContractError) {
      // 契约不符是**我们的 bug**（或后端改了契约），要带上原始值一起报
      throw new ApiError({
        status: response.status,
        code: "contract_mismatch",
        message: `响应不符合契约：${exc.message}`,
        requestId: response.headers.get("X-Request-Id") ?? "",
      });
    }
    throw exc;
  }
}

/** `POST /api/chat`（非流式）。对内页的报表/图表走这条（它带 `chart_spec`）。 */
export function postChat(token: string, options: ChatOptions): Promise<ChatResponse> {
  return postJson(
    "/api/chat",
    token,
    // ⚠️ 不带 session_id 时**不要**写 `session_id: null`：后端 `extra="forbid"` 只管多余字段，
    // 但显式 null 与「缺省」在语义上不同（前者是「我要一个叫 null 的会话」）。省掉键更干净。
    options.sessionId ? { message: options.message, session_id: options.sessionId } : { message: options.message },
    parseChatResponse,
    { signal: options.signal, fetchImpl: options.fetchImpl },
  );
}

/** `POST /api/chat/approve`（人工审批通道）。 */
export function postApprove(token: string, options: ApproveOptions): Promise<ChatResponse> {
  const body: Record<string, unknown> = { thread_id: options.threadId, approved: options.approved };
  if (options.reason) body.reason = options.reason;
  return postJson("/api/chat/approve", token, body, parseChatResponse, {
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

export interface HealthPayload {
  status: string;
  service: string;
  env: string;
  version: string;
  components: { name: string; status: string; detail: string; latency_ms: number | null }[];
}

/** `GET /health`（不吃鉴权、恒 200：依赖故障体现在 `components` 上）。 */
export async function getHealth(
  options: { fetchImpl?: typeof fetch; signal?: AbortSignal } = {},
): Promise<HealthPayload> {
  const doFetch = options.fetchImpl ?? fetch;
  const response = await doFetch(apiUrl("/health"), { signal: options.signal });
  if (!response.ok) throw await toApiError(response);
  const payload = (await response.json()) as HealthPayload;
  return {
    status: String(payload.status ?? ""),
    service: String(payload.service ?? ""),
    env: String(payload.env ?? ""),
    version: String(payload.version ?? ""),
    components: Array.isArray(payload.components)
      ? payload.components.map((item) => ({
          name: String(item.name ?? ""),
          status: String(item.status ?? ""),
          detail: String(item.detail ?? ""),
          latency_ms: typeof item.latency_ms === "number" ? item.latency_ms : null,
        }))
      : [],
  };
}
