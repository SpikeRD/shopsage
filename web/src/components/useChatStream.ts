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
  sessionId: string;
  error: string;
  send: (text: string) => void;
  approve: (approved: boolean, reason?: string) => Promise<void>;
  reset: () => void;
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

  useEffect(() => {
    const existing = readSessionId(options.entry, storage) ?? newSessionId();
    writeSessionId(options.entry, existing, storage);
    setSessionId(existing);
  }, [options.entry, storage]);

  useEffect(() => {
    const handle = openChatStream({
      token: options.token,
      url: options.url,
      socketFactory: options.socketFactory,
      onState: (next) => {
        setStream(next);
        if (next.error && next.phase === "failed") setError(next.error.message);
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
    // ⚠️ 依赖里只放「不变的东西」：token/url 在登录后就是常量，
    // 把它们放进来会在每次重渲染时重建连接（症状是「每发一句就掉线重连」）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.token, options.url]);

  const send = useCallback(
    (text: string) => {
      const handle = handleRef.current;
      if (!handle) {
        setError("连接还没建立（WebSocket 正在握手），请稍等一下再发。");
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

  const running = stream.phase === "connecting" || stream.phase === "streaming";

  return useMemo(
    () => ({ messages, stream, running, sessionId, error, send, approve, reset }),
    [messages, stream, running, sessionId, error, send, approve, reset],
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
