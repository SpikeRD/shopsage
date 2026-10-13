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

import { isSelfReferentialBase, streamUrl } from "./config.ts";
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
  /**
   * 握手超时（毫秒）。**没有它，「一直连接中」会永久卡住输入框**（坑 16）：
   * 网络层吞掉 upgrade 时浏览器可能长时间既不 `open` 也不 `error`。
   * `0` = 关掉（只给测试用：假 socket 永远不会自己 open 是正常的）。
   */
  handshakeTimeoutMs?: number;
  /** 等回帧超时（毫秒），见 `handshakeTimeoutMs` 的说明；`0` = 关掉。 */
  frameTimeoutMs?: number;
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
 * **当前活着的那条连接**（模块级世代号）。
 *
 * ## 为什么需要它（实测踩到，见 runbook §10.1 坑 15）
 *
 * React 开发模式（StrictMode）会把 effect **跑两遍**：第一条连接被 cleanup `close()`
 * 掉，第二条接管。热更新（Fast Refresh）时也一样。而**被抛弃的那条连接**的事件
 * 仍会到达它的监听器 —— 其中 `error` 尤其麻烦：
 *
 * ```text
 * 用户视角：聊得好好的，忽然弹一条「WebSocket 连接失败：ws://…」
 * 后端日志：同一个客户端端口出现两条 /api/chat/stream accepted（一条被强行关掉）
 * ```
 *
 * 也就是说：**当前连接完全正常，报错来自一条已经没人要的旧连接**。
 * 处置：每次建连领一个世代号，事件回调先比对 —— 不是当前世代就**直接扔掉**。
 * 这与「最后写入者胜」的竞态是同一类问题，只是这里的「最后写入者」是一条死连接。
 */
let activeGeneration = 0;

/**
 * 握手超时的默认值（毫秒）。本机实测握手 < 100ms，8 秒已经非常宽松。
 * ⚠️ 它的作用不是「快点报错」，而是**保证最终一定会报错** —— 见坑 16。
 */
export const DEFAULT_HANDSHAKE_TIMEOUT_MS = 8_000;

/**
 * 一轮问答等回帧的超时（毫秒）。**按实测校准**：冷启动时后端要把
 * bge-m3 + bge-reranker 拉进内存，实测整轮可以到 **109.7s**（§11 的证据表），
 * 所以取 180s —— 比实测最慢的那次还宽一倍。
 */
export const DEFAULT_FRAME_TIMEOUT_MS = 180_000;

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
  const handshakeTimeoutMs = options.handshakeTimeoutMs ?? DEFAULT_HANDSHAKE_TIMEOUT_MS;
  const frameTimeoutMs = options.frameTimeoutMs ?? DEFAULT_FRAME_TIMEOUT_MS;

  // 本连接的世代号：**所有事件回调都先问一句「我还是当前那条连接吗」**
  const generation = (activeGeneration += 1);
  const isCurrent = () => generation === activeGeneration;

  let state = initialState();
  /** 调用方是否主动 `close()` 过（见 `close()` 的注释：CONNECTING 阶段关闭会得到 1006）。 */
  let intentionalClose = false;
  const push = (next: StreamState) => {
    if (!isCurrent()) return; // 旧连接不许再改状态（它已被 close / 被新连接取代）
    state = next;
    options.onState(next);
  };
  push({ ...state, phase: "connecting" });

  // ── 看门狗（坑 16）：没有它，「一直连接中」会**永久**卡住输入框 ──────────────
  //
  // 两个真实的挂起窗口，各配一个超时：
  //   ① **握手不回来**：网络层把这次 upgrade 吞掉（代理/防火墙/半开连接）时，
  //      浏览器可能在几十秒内既不 `open` 也不 `error` —— 而 `running` 为真会禁用输入框，
  //      用户看到的是「一直显示正在连接，没办法输入问题」；
  //   ② **握手成功但这一轮没有回帧**：服务端进程被杀 / 连接被中间设备静默切断时，
  //      客户端可能长时间收不到任何帧（也没有 close）。界面会永远转圈。
  //
  // 超时的取值是**实测校准**的，不是拍的：冷启动时后端要把 bge-m3 + reranker 拉进内存，
  //   实测「真溯源」那一轮整轮可以到 **109.7s**（§11 的证据表），所以一轮的上限取 180s；
  //   握手则应当很快（本机实测 < 100ms），8s 已经很宽松。
  let handshakeTimer: ReturnType<typeof setTimeout> | null = null;
  let frameTimer: ReturnType<typeof setTimeout> | null = null;
  const clearTimers = () => {
    if (handshakeTimer !== null) clearTimeout(handshakeTimer);
    if (frameTimer !== null) clearTimeout(frameTimer);
    handshakeTimer = null;
    frameTimer = null;
  };
  const failWith = (code: string, message: string) => {
    clearTimers();
    push({ ...state, phase: "failed", error: { code, message } });
  };

  if (handshakeTimeoutMs > 0) {
    handshakeTimer = setTimeout(() => {
      if (!isCurrent() || state.phase !== "connecting") return;
      // 主动收线（避免那条连接继续挂在后台），并显式说明「不是你的操作问题」
      intentionalClose = true;
      try {
        // ⚠️ 传 1000 而不是裸 `close()`：关掉一条 CONNECTING 的连接时，
        // 浏览器给的仍是 1006，但**我们这边**要表明「是主动放弃的」——
        // 裸 `close()` 与 `close(1000)` 在真实浏览器里都会走 `error → close(1006)`，
        // 差别只在于读代码的人能不能一眼看出意图（以及测试能不能断言）。
        socket.close(1000);
      } catch {
        /* 已经坏了就算了 */
      }
      failWith(
        "connect_timeout",
        `握手超时：${Math.round(handshakeTimeoutMs / 1000)} 秒内没有收到服务端的 WebSocket 应答（${url}）。` +
          "这种「既不成功也不报错」多半是网络层把这次 upgrade 吞了（代理/防火墙/半开连接）。" +
          "先跑 `uv run python -m scripts.check_web_cors`（⑤ 会告诉你 upgrade 到底通不通），" +
          "然后点页面上的「重连」再试一次。",
      );
    }, handshakeTimeoutMs);
  }

  const armFrameWatchdog = () => {
    if (frameTimeoutMs <= 0) return;
    if (frameTimer !== null) clearTimeout(frameTimer);
    frameTimer = setTimeout(() => {
      if (!isCurrent() || state.phase !== "streaming") return;
      failWith(
        "frame_timeout",
        `等回帧超时：${Math.round(frameTimeoutMs / 1000)} 秒内没有收到任何帧。` +
          "服务端可能正在被重启、或者这条连接被中间设备静默切断了（这种情况下浏览器不会给 close 事件）。" +
          "点「重连」再试一次；若刚改过后端代码，确认它已经重启（本仓后端不带 --reload）。",
      );
    }, frameTimeoutMs);
  };

  const socket = factory(url, protocols);
  const opened = new Promise<void>((resolve) => {
    socket.addEventListener("open", () => {
      if (!isCurrent()) return;
      if (handshakeTimer !== null) {
        clearTimeout(handshakeTimer);
        handshakeTimer = null;
      }
      // ⚠️ **握手成功必须把 phase 推出 `connecting`**（实测踩到，见 runbook §10.1 坑 20）：
      // 这一层以前只清看门狗 + 回调 `onOpen`，而 `phase` 唯一能离开 `connecting` 的路径
      // 是「收到一帧」。两者一叠就成了**死锁**：
      //   * `useChatStream.send()` 判 `phase === "connecting"` → 把消息**排队**、不发送；
      //   * 而那条排队只在 `open` 事件里冲一次，此时（用户还没点）队列是空的；
      //   * 后端不会主动发帧 → 永远收不到帧 → 永远收不到帧就不会有帧让 phase 前进。
      // 结果：**页面上一条消息都发不出去**，徽章永远「正在连接… Ns」秒数一直涨。
      // `push` 带 `isCurrent()` 守卫，且这里是「当前连接」才会走到，所以安全。
      push({ ...state, phase: "idle" });
      options.onOpen?.();
      resolve();
    });
  });

  socket.addEventListener("message", (event: unknown) => {
    if (!isCurrent()) return;
    // 收到帧 = 这一轮还活着：把看门狗推后（见上面的两个挂起窗口）
    armFrameWatchdog();
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
    clearTimers();
    const code = (event as { code?: number }).code ?? 1006;
    // 「主动关闭」不算失败（§10.1 坑 15）：CONNECTING 阶段被我们关掉时，
    // 浏览器给的就是 1006 —— 那句话面意思是「异常断开」，但成因是我们自己收的线
    const failed = state.phase !== "done" && !intentionalClose;
    push({
      ...state,
      phase: failed ? "failed" : state.phase,
      closeCode: code,
      error:
        failed && !state.error
          ? { code: `close_${code}`, message: closeCodeMessage(code) }
          : state.error,
    });
    // ⚠️ 回调也要挡（`onClose` 会改调用方的状态）：旧连接的关闭不该触发界面上的反应
    if (isCurrent()) options.onClose?.(code);
  });

  socket.addEventListener("error", () => {
    if (!isCurrent()) return; // 被抛弃的连接：直接扔掉（见 `activeGeneration` 的注释）
    // ⚠️ 消息里**必须带上试过的 URL 与子协议**（第一版只有一句「先确认后端在跑」）：
    // 实测那次故障里后端完全正常，真正的原因是「URL 指到了前端自己」——
    // 而「后端在跑吗」这个方向把人带偏了很久（runbook §10.1 坑 14）。
    // 判据（写进话里）：`ws:` 连不上 + HTTP 侧正常 = 十有八九是**地址**问题，不是后端问题。
    const sameOrigin = isSelfReferentialBase(url);
    push({
      ...state,
      phase: "failed",
      error: {
        code: "socket_error",
        message:
          `WebSocket 连接失败：${url}（子协议 ${WS_SUBPROTOCOL}）` +
          "。按这个顺序查三件事：" +
          "① 地址对不对 —— 页面上那行「后端 …」是不是后端的地址（不是前端自己的 3000）；" +
          "② 后端在不在 —— 另开终端跑 `uv run python -m scripts.check_web_cors`（它会一次问完后端/前端/跨源/预检）；" +
          "③ 浏览器 DevTools → Console 里那条握手失败的具体原因（子协议、混合内容、连接被拒都会写在那里）。" +
          (sameOrigin
            ? "⚠️ 当前 URL 指向**前端自己**：Next 的 dev server 不代理 WebSocket upgrade，" +
              "所以它一定连不上 —— 用后端地址重启前端：" +
              '$env:NEXT_PUBLIC_API_BASE="http://127.0.0.1:8000"; npm run dev'
            : "如果 ① ② 都正常，再看后端日志里有没有这次握手（没有任何日志 = 请求根本没到后端，多半是被代理/防火墙拦了）；" +
              "地址本身没问题的话，多半就是 `NEXT_PUBLIC_API_BASE` 指向了前端自己的端口（见 ①）。"),
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
      // 发出问题后开始等回帧（`beginTurn` 把 phase 设成 streaming）
      armFrameWatchdog();
      socket.send(JSON.stringify(body));
    },
    close() {
      clearTimers();
      // ⚠️ **在 CONNECTING 阶段 close 会得到一个 `error`（实测，见 runbook §10.1 坑 15）**：
      // 浏览器还没握手就收到关闭请求，于是报一次 `error` 并以 1006 结束 —— 事件序列是
      // `error → close(1006)`，**看起来像连接失败，其实只是「还没来得及连就被收掉了」**。
      // React 开发模式的双挂载（cleanup 在同一个 tick 里 close）正好命中这个序列。
      //
      // 这里的处置：**仍然按 1000 关**（语义是「我们主动放弃」），
      // 但把这个 `close()` 标成「主动关闭」—— 之后那条 1006 只作为**信息**记录，
      // 不再变成「连接失败」的报错（真正的失败由 `error` 处理器判断，而它已经被世代守卫过滤）。
      intentionalClose = true;
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
