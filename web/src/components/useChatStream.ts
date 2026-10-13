"use client";

/**
 * 把「流式连接」和「消息列表」缝在一起的**唯一一处**（React hook）。
 *
 * 两条纪律：
 *
 * 1. **业务判断一件都不做**：`blocked` / `refusals` / `approval` 全部**照实展示**。
 *    这里只在「服务端说待审批」时多给我一个按钮 —— 那个按钮调的是
 *    `POST /api/chat/approve`，批准与否的后果由后端的闸门决定（`8.graph.md` §5 第四条）。
 * 2. **会话 id 由前端记住、`thread_id` 不记**：`thread_id` 是服务端派生的
 *    （`u{user_id}:{session_id}`），审批通道要用它，但**只能用响应里回传的那个**，
 *    前端不自己拼（`16.web.md` §4 第 4 条）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, postApprove } from "@/lib/api";
import {
  browserStorage,
  newSessionId,
  readSession,
  readSessionId,
  writeSessionId,
  type Entry,
  type StorageLike,
} from "@/lib/auth";
import { openChatStream, toTurnResult, type ChatStreamHandle, type StreamState, initialState } from "@/lib/stream";
import type { Message } from "@/components/MessageList";

export interface UseChatStreamOptions {
  entry: Entry;
  /** 已经在页面里读到的令牌（由登录组件写入 localStorage） */
  token: string;
  /** 注入点：测试里给假 storage / 假 socket */
  storage?: StorageLike;
  socketFactory?: Parameters<typeof openChatStream>[0]["socketFactory"];
  url?: string;
}

export interface UseChatStreamResult {
  messages: Message[];
  stream: StreamState;
  running: boolean;
  /** 连接还在握手（页面据此显示计时与「重连」出口；**不**等于 running） */
  connecting: boolean;
  sessionId: string;
  error: string;
  /** 已经自动重连过几次（页面上显示出来，别让它成为静默行为） */
  connectionAttempts: number;
  send: (text: string) => void;
  approve: (approved: boolean, reason?: string) => Promise<void>;
  reset: () => void;
  /** 手动重连：**换一条连接**（不是只清状态 —— 那正是坑 17 里「点新会话就好了」的假修法） */
  reconnect: () => void;
}

function messageId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
}

/** 从 stream 的收尾状态造一条 assistant 消息（含出处、拦下原因、降级标记）。 */
function toAssistantMessage(state: StreamState): Message {
  const turn = toTurnResult(state);
  return {
    id: messageId("assistant"),
    role: "assistant",
    text: turn.answer || "（这一轮没有产生回答：看下面的状态与拦下原因）",
    citations: turn.citations,
    flowState: turn.flowState,
    route: turn.route,
    blocked: turn.blocked,
    degraded: turn.degraded,
    refusals: [],
  };
}

export function useChatStream(options: UseChatStreamOptions): UseChatStreamResult {
  const storage = options.storage ?? browserStorage();
  const [messages, setMessages] = useState<Message[]>([]);
  const [stream, setStream] = useState<StreamState>(initialState());
  const [error, setError] = useState("");
  const [sessionId, setSessionId] = useState("");
  const handleRef = useRef<ChatStreamHandle | null>(null);
  /** 收到 `done` 帧的那一轮是在「发问题」还是「审批」时产生的 —— 决定要不要追加气泡 */
  const waitingRef = useRef<"ask" | "approve" | null>(null);
  /**
   * 重建连接的钥匙：`reconnect()` 把它 +1，下面的 effect 随之重建（并 clean 掉旧连接）。
   *
   * ⚠️ 不能只靠「清状态」让界面恢复可用 —— 那正是坑 16 里「点了新会话就好了」的假修法：
   * 状态清了，**socket 还是那条坏的**，下一轮照样卡。要换的是连接本身。
   */
  const [connectionKey, setConnectionKey] = useState(0);
  /** 这条连接一共尝试过几次（> 1 = 发生过自动重连，页面上会显示出来，不许静默） */
  const [connectionAttempts, setConnectionAttempts] = useState(1);
  /** 自动重连次数（用 ref 读，见 effect 里的注释：state 在闭包里是旧值） */
  const attemptsRef = useRef(1);
  /** 握手还没完成时用户就发的那条消息：等 `open` 之后自动发出（见 `send`） */
  const pendingRef = useRef<{ text: string; sessionId: string } | null>(null);

  useEffect(() => {
    const existing = readSessionId(options.entry, storage) ?? newSessionId();
    writeSessionId(options.entry, existing, storage);
    setSessionId(existing);
  }, [options.entry, storage]);

  useEffect(() => {
    let settled = false;
    const handle = openChatStream({
      token: options.token,
      url: options.url,
      socketFactory: options.socketFactory,
      onOpen: () => {
        settled = true;
        // 连接建立：把「等它好了再发」的那条消息发出去（见 `send` 的排队分支）
        const queued = pendingRef.current;
        if (queued !== null) {
          pendingRef.current = null;
          waitingRef.current = "ask";
          handle.ask(queued.text, queued.sessionId || null);
        }
      },
      onState: (next) => {
        setStream(next);
        if (next.error && next.phase === "failed") {
          setError(next.error.message);
          // ⚠️ 连不上时把排队的那条消息丢掉并**如实说明**（坑 17）：
          // 留着它会在下一次连上时突然发出去一条「很久以前打的字」——
          // 那比不发更让人困惑。用户重新发一遍成本很低，误导成本很高。
          if (pendingRef.current !== null) {
            pendingRef.current = null;
            setError(`${next.error.message}\n（排队中的那条消息没有发出，请重连后重新发送。）`);
          }
          // ── 连接根本没建立起来时的**自动重连一次**（坑 16 / 坑 17）────────────
          // ⚠️ 用 ref 记次数而不是读 `connectionAttempts` state：后者在闭包里是**旧值**，
          // 会让「最多重试一次」变成「重试无限次」（每次都是 1）。
          if (
            !settled &&
            (next.error.code === "connect_timeout" || next.error.code === "socket_error") &&
            attemptsRef.current < 2
          ) {
            attemptsRef.current += 1;
            setConnectionAttempts(attemptsRef.current);
            setTimeout(() => setConnectionKey((key) => key + 1), 800);
          }
        }
        if (next.phase === "done" && waitingRef.current === "ask") {
          // 把这一轮的产物落成一条消息（**只在 done 时落**：中途的 answer 帧可能被替换）
          waitingRef.current = null;
          setMessages((current) => [...current, toAssistantMessage(next)]);
        }
      },
    });
    handleRef.current = handle;
    return () => {
      handle.close();
      handleRef.current = null;
    };
    // ⚠️ **`connectionKey` 必须在依赖里**（坑 17）：上一版漏了它，于是
    // `setConnectionKey` 改了一个没人看的 state —— 自动重连与「重连」按钮**都是哑的**，
    // 而唯一能摆脱「正在连接」的办法变成了 `reset()`（它只清 state）：
    // 这正是用户报的「点新会话就正常了」的机制。
    // 现在这条接线由 `web/tests/hook-wiring.test.ts` 的源码级断言钉住。
    // token/url 在登录后是常量，放进来不会造成每次重渲染重建连接；
    // `options.socketFactory` 只在测试里注入，也不参与（否则每渲染一次就重连）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.token, options.url, connectionKey]);

  const send = useCallback(
    (text: string) => {
      const handle = handleRef.current;
      const phase = handle?.state().phase ?? "connecting";
      // ── 连接还没就绪时**排队**，而不是拒绝、更不是把消息丢进黑洞（坑 16/17）──────
      // 为什么不是「禁用输入框」：禁用会让「连接卡住」直接表现为「用户什么都做不了」，
      // 而那正是这次报的故障。排队 + 连接建立后自动发出，用户的操作永远不会白做。
      if (phase === "failed") {
        // 这条连接已经死了：**换一条**并把消息排队（`onOpen` 会把它发出去）
        pendingRef.current = { text, sessionId };
        setError("这条连接已经断了：已自动换一条连接，消息会在连上后发出。");
        setMessages((current) => [...current, { id: messageId("user"), role: "user", text }]);
        attemptsRef.current = 1;
        setConnectionKey((key) => key + 1);
        return;
      }
      if (phase === "connecting" || !handle) {
        pendingRef.current = { text, sessionId };
        setError("");
        setMessages((current) => [...current, { id: messageId("user"), role: "user", text }]);
        return;
      }
      setError("");
      setMessages((current) => [...current, { id: messageId("user"), role: "user", text }]);
      waitingRef.current = "ask";
      handle.ask(text, sessionId || null);
    },
    [sessionId],
  );

  const approve = useCallback(
    async (approved: boolean, reason?: string) => {
      const gate = stream.approval;
      if (!gate) return;
      setError("");
      setMessages((current) => [
        ...current,
        {
          id: messageId("system"),
          role: "system",
          text: approved ? "已提交「批准」" : "已提交「拒绝」",
        },
      ]);
      try {
        const response = await postApprove(options.token, {
          threadId: gate.threadId,
          approved,
          reason,
        });
        // 审批的结果走 HTTP 返回（不是 WS 帧）：所以这里直接把响应落成一条 assistant 消息
        setMessages((current) => [
          ...current,
          {
            id: messageId("assistant"),
            role: "assistant",
            text: response.answer || "（审批完成，但没有产生回答）",
            citations: response.citations,
            flowState: response.flow_state,
            route: response.route,
            blocked: response.blocked,
            refusals: response.refusals,
          },
        ]);
      } catch (exc) {
        const message =
          exc instanceof ApiError
            ? `${exc.message}${exc.requestId ? `（request_id=${exc.requestId}）` : ""}`
            : String(exc);
        setError(message);
        setMessages((current) => [...current, { id: messageId("system"), role: "system", text: `审批失败：${message}` }]);
      }
    },
    [options.token, stream.approval],
  );

  const reset = useCallback(() => {
    const fresh = newSessionId();
    writeSessionId(options.entry, fresh, storage);
    setSessionId(fresh);
    setMessages([]);
    setStream(initialState());
    setError("");
  }, [options.entry, storage]);

  /**
   * 是否正在处理一轮（**只看 `streaming`**）。
   *
   * ⚠️ **不能把 `connecting` 也算进来**（坑 16/17）：那样「连接卡住」会直接表现为
   * 「输入框永久禁用」——用户既得不到回答，也看不到原因，唯一出路是乱点按钮。
   * 现在 `connecting` 时输入框可用，消息**排队**等连接建立（`send` 的分支），
   * 用户的动作永远不会白做。
   */
  const running = stream.phase === "streaming";

  /** 连接是否还卡在握手里（页面用它显示计时与「重连」出口） */
  const connecting = stream.phase === "connecting";

  const reconnect = useCallback(() => {
    setError("");
    // ⚠️ 两件事都做：清掉挂起的一轮（`waitingRef`）**并换一条连接**。
    // 只清状态不换连接 = 坑 16 里那个假修法（「点新会话就好了」）。
    waitingRef.current = null;
    setConnectionAttempts((attempts) => attempts + 1);
    setConnectionKey((key) => key + 1);
  }, []);

  return useMemo(
    () => ({
      messages,
      stream,
      running,
      connecting,
      sessionId,
      error,
      connectionAttempts,
      send,
      approve,
      reset,
      reconnect,
    }),
    [
      messages,
      stream,
      running,
      connecting,
      sessionId,
      error,
      connectionAttempts,
      send,
      approve,
      reset,
      reconnect,
    ],
  );
}

/** 页面用它判断「有没有登录」（未登录时跳 `/login`）。 */
export function useSession(entry: Entry, storage: StorageLike = browserStorage()) {
  const [session, setSession] = useState<ReturnType<typeof readSession>>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    setSession(readSession(entry, storage));
    setReady(true);
  }, [entry, storage]);
  return { session, ready };
}
