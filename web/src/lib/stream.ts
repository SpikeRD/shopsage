/**
 * WebSocket 流式问答：**一个纯 reducer + 一个薄连接层**。
 *
 * ## 为什么拆成两半
 *
 * 帧时序（骨架 → 节点 → 回答 → 收尾）是这一步最值钱的认知，
 * 而它**必须能被离线测**（`web/tests/stream.test.ts` 就是喂一串帧进去看结果）。
 * 把「怎么摆弄 WebSocket 对象」和「帧序列意味着什么」揉在一起，
 * 就只能靠起浏览器 + 真后端来测 —— 那是本步最贵、最不稳的一类测试。
 * 所以：
 *
 * * `reduceStream()`：纯函数，`(state, frame) => state`，零依赖、可穷举；
 * * `openChatStream()`：薄壳，只做四件事 —— 拼子协议、连、把帧喂给 reducer、把状态推出去。
 *
 * ## 三条与后端对齐的口径
 *
 * 1. **令牌走子协议**：`Sec-WebSocket-Protocol: shopsage.v1, bearer.<jwt>`。
 *    浏览器不能给 WebSocket 自定义请求头，所以后端把令牌放在子协议里
 *    （`app/api/deps.py::ws_token`）。`shopsage.v1` **必须**一起带上：
 *    服务端只在「客户端提出的列表里」挑一个回（`accept(subprotocol=...)`），
 *    少带它会让服务端回一个空子协议（浏览器随即报握手失败）。
 * 2. **关闭码是唯一能编程处理的信息**：握手阶段的 HTTP 状态码浏览器拿不到，
 *    所以 4401 = 该重新登录、4429 = 该退避重试、4400 = 请求不合法。
 * 3. **一轮问答 = 一条连接上的多条消息**：同一连接可以连着问（多轮复用），
 *    但本实现的 reducer 用 `done` 帧收尾一轮；并发两轮会串帧，
 *    所以 `running` 期间**禁止再发**（UI 上把发送键禁掉，见 §4.4）。
 */

import { streamUrl } from "./config.ts";
import {
  type ApprovalGate,
  type ChatDebug,
  type ChartSpec,
  type FlowState,
  type Frame,
  parseFlowState,
  parseFrame,
  WS_CLOSE_BAD_REQUEST,
  WS_CLOSE_RATE_LIMITED,
  WS_CLOSE_UNAUTHORIZED,
  WS_SUBPROTOCOL,
  WS_TOKEN_PREFIX,
} from "./contracts.ts";

// ── 状态 ────────────────────────────────────────────────────────────────────

export type StreamPhase = "idle" | "connecting" | "streaming" | "done" | "failed";

/** 一条挂起中的审批请求（`flow_state=approval_required` 时才有）。 */
export interface PendingApproval extends ApprovalGate {
  route: string;
}

export interface StreamState {
  phase: StreamPhase;
  /** 连接建立（收到第一条帧）的时刻，用于展示「首帧 xxx ms」 */
  firstFrameMs: number | null;
  /** 服务端自报的首帧耗时（`done` 帧里） */
  serverFirstFrameMs: number | null;
  /** 服务端自报的整轮耗时（`done` 帧里） */
  latencyMs: number | null;
  /** 收到的帧序号池：用来发现丢帧/乱序（WebSocket 是可靠有序的，所以异常一定来自本地逻辑） */
  seqs: number[];
  /** 骨架帧的文案（按到达顺序追加；出现重复的只留最新一条），最后一条即当前提示语 */
  skeleton: string[];
  /** 走过的图节点（`node` 帧），去重保序 */
  nodes: string[];
  /** 节点名 → 给用户看的进度文案 */
  nodeProgress: Record<string, string>;
  /** `answer` 帧的回答正文（流式期间会先用骨架占位，收到才替换） */
  answer: string;
  /** `answer` 帧里的出处串 */
  citations: string[];
  /** `answer` 帧里是否带图表（WS 帧只带布尔；图表本体在 HTTP 响应里，见 §4.2） */
  hasChart: boolean;
  /** `answer` 帧里的 degraded（重放预算用尽 = 这一轮「没做到」） */
  degraded: boolean;
  flowState: FlowState | null;
  threadId: string;
  sessionId: string;
  route: string;
  blocked: boolean;
  approval: PendingApproval | null;
  debug: ChatDebug | null;
  /** 错误帧或关闭码带来的失败信息 */
  error: { code: string; message: string } | null;
  /** 连接被关闭的码（4401/4429/4400 见 §4.4） */
  closeCode: number | null;
}

export function initialState(): StreamState {
  return {
    phase: "idle",
    firstFrameMs: null,
    serverFirstFrameMs: null,
    latencyMs: null,
    seqs: [],
    skeleton: [],
    nodes: [],
    nodeProgress: {},
    answer: "",
    citations: [],
    hasChart: false,
    degraded: false,
    flowState: null,
    threadId: "",
    sessionId: "",
    route: "",
    blocked: false,
    approval: null,
    debug: null,
    error: null,
    closeCode: null,
  };
}

/** 开始一轮：保留会话与连接级信息（`sessionId` / `threadId`），清掉上一轮的产物。 */
export function beginTurn(state: StreamState): StreamState {
  return {
    ...initialState(),
    phase: "streaming",
    sessionId: state.sessionId,
    threadId: state.threadId,
  };
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

/**
 * **唯一的状态推进点**。任何一帧进来都只经过这里。
 *
 * 前端**不做业务判断**（`16.web.md` §4 第 4 条）：这一层只负责
 * 「把服务端说的话摆到该摆的位置」，不含任何权限/合规判断 ——
 * `blocked` / `refusals` 都是**照实展示**，不是前端算出来的。
 */
export function reduceStream(state: StreamState, frame: Frame, now: number = Date.now()): StreamState {
  const next: StreamState = {
    ...state,
    seqs: [...state.seqs, frame.seq],
    firstFrameMs: state.firstFrameMs ?? now,
  };
  const data = frame.data;
  switch (frame.event) {
    case "skeleton": {
      const text = typeof data.text === "string" ? data.text : "";
      const route = typeof data.route === "string" ? data.route : "";
      if (route) next.route = route;
      // 同样的提示语不重复追加（服务端可能在路由确定时再发一条骨架）
      if (text && next.skeleton[next.skeleton.length - 1] !== text) next.skeleton = [...next.skeleton, text];
      next.phase = next.phase === "idle" ? "streaming" : next.phase;
      return next;
    }
    case "node": {
      const node = typeof data.node === "string" ? data.node : "";
      const progress = typeof data.progress === "string" ? data.progress : "";
      if (node) {
        if (!next.nodes.includes(node)) next.nodes = [...next.nodes, node];
        if (progress) next.nodeProgress = { ...next.nodeProgress, [node]: progress };
      }
      const delta = (data.delta ?? {}) as Record<string, unknown>;
      if (typeof delta.route === "string" && delta.route) next.route = delta.route;
      // ⚠️ 不把 `delta` 里的其他内容塞进状态：它是后端**裁剪过的白名单**
      // （`app/api/chat.py::FRAME_SAFE_KEYS`），前端再抄一份等于又把「哪些字段能出网」的
      // 判断搬到了前端 —— 那正是白名单想避免的。
      return next;
    }
    case "answer": {
      next.answer = typeof data.text === "string" ? data.text : "";
      next.citations = asStringArray(data.citations);
      next.hasChart = Boolean(data.chart);
      next.degraded = Boolean(data.degraded);
      return next;
    }
    case "done": {
      try {
        next.flowState = parseFlowState(data.flow_state);
      } catch {
        next.flowState = null;
      }
      next.threadId = typeof data.thread_id === "string" ? data.thread_id : state.threadId;
      next.sessionId = typeof data.session_id === "string" ? data.session_id : state.sessionId;
      next.blocked = Boolean(data.blocked);
      if (typeof data.route === "string" && data.route) next.route = data.route;
      next.serverFirstFrameMs = typeof data.first_frame_ms === "number" ? data.first_frame_ms : null;
      next.latencyMs = typeof data.latency_ms === "number" ? data.latency_ms : null;
      next.debug = parseDebug(data.debug);
      next.approval = parseApproval(data.approval);
      next.phase = "done";
      return next;
    }
    case "error": {
      next.error = {
        code: typeof data.code === "string" ? data.code : "unknown",
        message: typeof data.message === "string" ? data.message : "服务端返回了一个错误帧",
      };
      // ⚠️ 错误帧**不代表整轮结束**：后端对「这一条消息不合法」（如空消息、超长）
      // 只回一条 error 帧，然后继续等下一次输入（`app/api/chat.py` 的消息循环）。
      // 所以这里不把 phase 改成 failed —— 否则用户改一句话再发也发不出去。
      return next;
    }
    default:
      return next;
  }
}

function parseApproval(value: unknown): PendingApproval | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  const threadId = typeof record.thread_id === "string" ? record.thread_id : "";
  if (!threadId) return null;
  return {
    threadId,
    question: typeof record.question === "string" ? record.question : "",
    interruptId: typeof record.interrupt_id === "string" ? record.interrupt_id : "",
    resumeWith: typeof record.resume_with === "string" ? record.resume_with : "POST /api/chat/approve",
    route: typeof record.route === "string" ? record.route : "",
  };
}

function parseDebug(value: unknown): ChatDebug | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  const asList = (item: unknown) => (Array.isArray(item) ? item.map(String) : []);
  return {
    visited: asList(record.visited),
    replayed: asList(record.replayed),
    exhausted: Boolean(record.exhausted),
    guardrailStage: typeof record.guardrail_stage === "string" ? record.guardrail_stage : "",
    guardrailRules: asList(record.guardrail_rules),
    rowCount: typeof record.row_count === "number" ? record.row_count : null,
    truncated: typeof record.truncated === "boolean" ? record.truncated : null,
    sql: typeof record.sql === "string" ? record.sql : null,
  };
}

/** 收到的帧序号有没有断档（`[1,2,4]` → 缺 3）。用于自检，不用于业务判断。 */
export function missingSeqs(seqs: readonly number[]): number[] {
  if (seqs.length === 0) return [];
  const seen = new Set(seqs);
  const missing: number[] = [];
  for (let i = 1; i <= Math.max(...seqs); i += 1) if (!seen.has(i)) missing.push(i);
  return missing;
}

/** 关闭码 → 给用户看的话术（**每一句都要指向一个动作**，不写「未知错误」）。 */
export function closeCodeMessage(code: number): string {
  switch (code) {
    case WS_CLOSE_UNAUTHORIZED:
      return "登录状态已失效（4401）：请重新登录后再试。";
    case WS_CLOSE_RATE_LIMITED:
      return "请求过于频繁（4429）：稍等一会儿再发，或联系管理员调整 RATE_LIMIT_CHAT_STREAM。";
    case WS_CLOSE_BAD_REQUEST:
      return "请求不合法（4400）：消息不能为空、也不能超过 2000 字。";
    default:
      return `连接被关闭（code=${code}）：如果是 1006，通常是网络中断或后端进程重启。`;
  }
}

// ── 连接层（薄壳）───────────────────────────────────────────────────────────

/** 浏览器 / Node 都有的 WebSocket 构造函数（Node 22+ 自带）。 */
export type WebSocketLike = {
  send(data: string): void;
  close(code?: number, reason?: string): void;
  addEventListener(type: string, listener: (event: unknown) => void): void;
  readyState: number;
};

export interface OpenStreamOptions {
  token: string;
  url?: string;
  /** 注入点：测试用假实现，浏览器用全局 `WebSocket` */
  socketFactory?: (url: string, protocols: string[]) => WebSocketLike;
  onState: (state: StreamState) => void;
  /** 连接建立（不是收到帧，而是 `open` 事件）时回调 */
  onOpen?: () => void;
  /** 连接关闭时回调（带关闭码） */
  onClose?: (code: number) => void;
  /** 注入时间源：测试里要能算出确定的 `firstFrameMs` */
  now?: () => number;
}

export interface ChatStreamHandle {
  /** 发一轮问题（连接未建立时抛 `Error`：调用方应先 `await ready()`） */
  ask(message: string, sessionId?: string | null): void;
  ready(): Promise<void>;
  close(): void;
  /** 当前状态（连接层自己维护一份，避免调用方要自己记账） */
  state(): StreamState;
}

/**
 * 建立一条流式连接。
 *
 * ⚠️ **子协议的顺序与内容都不是可选的**：`shopsage.v1` 必须在列表里
 * （服务端从客户端提出的列表里挑），`bearer.<token>` 是令牌唯一的通道。
 */
export function openChatStream(options: OpenStreamOptions): ChatStreamHandle {
  const factory =
    options.socketFactory ??
    ((url: string, protocols: string[]) => new WebSocket(url, protocols) as unknown as WebSocketLike);
  const now = options.now ?? (() => Date.now());
  const url = options.url ?? streamUrl();
  const protocols = [WS_SUBPROTOCOL, `${WS_TOKEN_PREFIX}${options.token}`];

  let state = initialState();
  const push = (next: StreamState) => {
    state = next;
    options.onState(next);
  };
  push({ ...state, phase: "connecting" });

  const socket = factory(url, protocols);
  const opened = new Promise<void>((resolve) => {
    socket.addEventListener("open", () => {
      options.onOpen?.();
      resolve();
    });
  });

  socket.addEventListener("message", (event: unknown) => {
    const payload = (event as { data?: unknown }).data;
    try {
      push(reduceStream(state, parseFrame(typeof payload === "string" ? payload : String(payload)), now()));
    } catch (exc) {
      // 契约不符（不是合法 JSON / 未知的 event）：当成一条本地错误展示，
      // **不要**让异常从事件回调里冒出去（那会静默失败，界面上什么都不发生）
      push({
        ...state,
        phase: "failed",
        error: { code: "bad_frame", message: `收到无法解析的帧：${(exc as Error).message}` },
      });
    }
  });

  socket.addEventListener("close", (event: unknown) => {
    const code = (event as { code?: number }).code ?? 1006;
    // 正常收尾（done 之后服务端不会主动关；客户端主动关也不该报错）
    const failed = state.phase !== "done";
    push({
      ...state,
      phase: failed ? "failed" : state.phase,
      closeCode: code,
      error:
        failed && !state.error
          ? { code: `close_${code}`, message: closeCodeMessage(code) }
          : state.error,
    });
    options.onClose?.(code);
  });

  socket.addEventListener("error", () => {
    push({
      ...state,
      phase: "failed",
      error: {
        code: "socket_error",
        message:
          "WebSocket 连接失败：先确认后端在跑（uv run uvicorn app.main:app --port 8000）、" +
          "再确认 NEXT_PUBLIC_API_BASE 指向它，最后看浏览器控制台里那条握手失败的原因。",
      },
    });
  });

  return {
    async ready() {
      await opened;
    },
    ask(message: string, sessionId?: string | null) {
      const body: Record<string, unknown> = { message };
      if (sessionId) body.session_id = sessionId;
      push(beginTurn(state));
      socket.send(JSON.stringify(body));
    },
    close() {
      socket.close(1000);
    },
    state() {
      return state;
    },
  };
}

/** 一轮问答的最终产物（UI 用它组装气泡）。 */
export interface TurnResult {
  answer: string;
  citations: string[];
  flowState: FlowState | null;
  threadId: string;
  sessionId: string;
  route: string;
  blocked: boolean;
  approval: PendingApproval | null;
  debug: ChatDebug | null;
  degraded: boolean;
  hasChart: boolean;
  latencyMs: number | null;
  firstFrameMs: number | null;
}

/** 把 reducer 的最终状态翻成一轮的产物（`done` 帧之后调）。 */
export function toTurnResult(state: StreamState): TurnResult {
  return {
    answer: state.answer,
    citations: state.citations,
    flowState: state.flowState,
    threadId: state.threadId,
    sessionId: state.sessionId,
    route: state.route,
    blocked: state.blocked,
    approval: state.approval,
    debug: state.debug,
    degraded: state.degraded,
    hasChart: state.hasChart,
    latencyMs: state.latencyMs,
    firstFrameMs: state.serverFirstFrameMs,
  };
}

export type { ChartSpec };
