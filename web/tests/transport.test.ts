/**
 * 连接层与错误解析的单元测试（离线，**注入假 socket / 假 fetch**）。
 *
 * 为什么这两件事必须离线测：
 *
 * * **子协议**：`Sec-WebSocket-Protocol` 里少一个 `shopsage.v1`，服务端会回一个空子协议，
 *   浏览器随即报「握手失败」—— 而这条信息在真环境里只有一行控制台报错。
 *   离线测它，才能把「我们到底发了什么头」钉死（§5.2）；
 * * **错误信封**：后端的 4xx/5xx 只有一个形状，前端只有一套解析。
 *   用假 `fetch` 造出 401/429/500/HTML 四种响应，比真去把后端弄坏一遍便宜得多。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError, postApprove, postChat } from "../src/lib/api.ts";
import { WS_SUBPROTOCOL, WS_TOKEN_PREFIX } from "../src/lib/contracts.ts";
import { openChatStream, type WebSocketLike } from "../src/lib/stream.ts";

/** 假 WebSocket：记录「发出去的子协议」与「客户端发了什么消息」。 */
class FakeSocket implements WebSocketLike {
  readonly sent: string[] = [];
  readonly listeners = new Map<string, ((event: unknown) => void)[]>();
  readonly url: string;
  readonly protocols: string[];
  readyState = 0;
  closed: { code?: number; reason?: string } | null = null;

  // ⚠️ 不用「构造函数参数属性」（`constructor(readonly url: string)`）：
  // Node 的类型擦除（strip-only）不支持那种语法，会抛
  // `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX` —— 这也是本项目测试跑在
  // `node --test --test-isolation=none` 而不是 tsx 上的代价（见 runbook §7.3）
  constructor(url: string, protocols: string[]) {
    this.url = url;
    this.protocols = protocols;
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(code?: number, reason?: string): void {
    this.closed = { code, reason };
  }

  addEventListener(type: string, listener: (event: unknown) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  emit(type: string, event: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  emitFrame(frame: unknown): void {
    this.emit("message", { data: JSON.stringify(frame) });
  }
}

function connect(token = "tok-123") {
  let socket: FakeSocket | null = null;
  const states: string[] = [];
  const handle = openChatStream({
    token,
    url: "ws://example.test/api/chat/stream",
    socketFactory: (url, protocols) => {
      socket = new FakeSocket(url, protocols);
      return socket;
    },
    onState: (state) => states.push(`${state.phase}:${state.seqs.length}`),
    now: () => 5_000,
  });
  return { handle, socket: () => socket as unknown as FakeSocket, states };
}

test("握手子协议：`shopsage.v1` 与 `bearer.<token>` **两个都要**（少一个就连不上）", () => {
  const { socket } = connect("abc.def.ghi");
  const sent = socket().protocols;
  assert.deepEqual(sent, [WS_SUBPROTOCOL, `${WS_TOKEN_PREFIX}abc.def.ghi`]);
  assert.equal(sent[0], "shopsage.v1");
  assert.ok(sent[1].startsWith("bearer."), "令牌只能走子协议：浏览器不能给 WS 自定义请求头");
});

test("URL 由注入点决定，默认走 NEXT_PUBLIC_API_BASE 推导", () => {
  const { socket } = connect();
  assert.equal(socket().url, "ws://example.test/api/chat/stream");
});

test("ask：带上 session_id 时原样发出；不带时不写这个键", () => {
  const { handle, socket } = connect();
  handle.ask("我的订单到哪了", "orders-1");
  assert.deepEqual(JSON.parse(socket().sent[0]), { message: "我的订单到哪了", session_id: "orders-1" });

  handle.ask("再问一句");
  assert.deepEqual(JSON.parse(socket().sent[1]), { message: "再问一句" });
});

test("ask 之后 phase=streaming，上一轮的产物被清掉", () => {
  const { handle, socket } = connect();
  handle.ask("第一轮", "s1");
  socket().emitFrame({ event: "answer", seq: 1, ts: 1, data: { text: "答", done: true, citations: [], chart: false } });
  assert.equal(handle.state().answer, "答");
  handle.ask("第二轮", "s1");
  assert.equal(handle.state().answer, "", "新的一轮不带着上一轮的回答");
  assert.equal(handle.state().phase, "streaming");
});

test("close(1000) 直接关连接（组件卸载时调用）", () => {
  const { handle, socket } = connect();
  handle.close();
  assert.equal(socket().closed?.code, 1000);
});

test("握手后立刻收到骨架帧：客户端首帧时间戳来自注入的时间源", () => {
  const { handle, socket } = connect();
  handle.ask("hi", "s1");
  socket().emitFrame({ event: "skeleton", seq: 1, ts: 1, data: { phase: "accepted", text: "收到…" } });
  assert.equal(handle.state().firstFrameMs, 5_000);
  assert.equal(handle.state().skeleton[0], "收到…");
});

test("无法解析的帧：本地造一条 bad_frame 并发成 failed（**不让异常冒出去**）", () => {
  const { handle, socket } = connect();
  socket().emit("message", { data: "<html>代理的错误页</html>" });
  assert.equal(handle.state().phase, "failed");
  assert.equal(handle.state().error?.code, "bad_frame");
  assert.match(handle.state().error?.message ?? "", /不是合法 JSON/);
});

test("4401 关闭：话术指向「重新登录」，且 phase=failed", () => {
  const codes: number[] = [];
  let last: ReturnType<typeof openChatStream> | null = null;
  let socket: FakeSocket | null = null;
  last = openChatStream({
    token: "t",
    url: "ws://example.test/x",
    socketFactory: (url, protocols) => {
      socket = new FakeSocket(url, protocols);
      return socket;
    },
    onState: () => {},
    onClose: (code) => codes.push(code),
  });
  assert.ok(last);
  (socket as unknown as FakeSocket).emit("close", { code: 4401 });
  assert.deepEqual(codes, [4401]);
  assert.equal(last.state().phase, "failed");
  assert.match(last.state().error?.message ?? "", /重新登录/);
});

test("已经有 done 帧之后再关闭：不算失败（正常收尾）", () => {
  const { handle, socket } = connect();
  socket().emitFrame({
    event: "done",
    seq: 1,
    ts: 1,
    data: { flow_state: "delivered", thread_id: "u2:s1", session_id: "s1", blocked: false, approval: null },
  });
  socket().emit("close", { code: 1000 });
  assert.equal(handle.state().phase, "done");
  assert.equal(handle.state().closeCode, 1000);
});

test("socket error：给出一句**能照着修**的话（先查后端、再查地址）", () => {
  const { handle, socket } = connect();
  socket().emit("error", {});
  assert.equal(handle.state().phase, "failed");
  assert.match(handle.state().error?.message ?? "", /uvicorn|NEXT_PUBLIC_API_BASE/);
});

// ── HTTP 侧的构造 ──────────────────────────────────────────────────────────

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

test("postChat：不带 sessionId 时请求体里没有 session_id（后端 extra=forbid）", async () => {
  let seen: { url: string; init: RequestInit } | null = null;
  const fetchImpl = (async (url: string | URL | Request, init?: RequestInit) => {
    seen = { url: String(url), init: init ?? {} };
    return jsonResponse({
      answer: "好",
      flow_state: "delivered",
      thread_id: "u2:s1",
      session_id: "s1",
    });
  }) as unknown as typeof fetch;

  const response = await postChat("tok", { message: "hi", fetchImpl });
  assert.equal(response.answer, "好");
  assert.equal(response.flow_state, "delivered");
  assert.equal((seen!.init.headers as Record<string, string>).Authorization, "Bearer tok");
  assert.deepEqual(JSON.parse(String(seen!.init.body)), { message: "hi" });
});

test("postChat：带 sessionId 时原样发出", async () => {
  let body: unknown = null;
  const fetchImpl = (async (_url: string | URL | Request, init?: RequestInit) => {
    body = JSON.parse(String(init?.body));
    return jsonResponse({ answer: "", flow_state: "delivered", thread_id: "u2:s1", session_id: "s1" });
  }) as unknown as typeof fetch;
  await postChat("tok", { message: "hi", sessionId: "orders-1", fetchImpl });
  assert.deepEqual(body, { message: "hi", session_id: "orders-1" });
});

test("401：翻成 ApiError，把 code / request_id / Retry-After 都留住", async () => {
  const fetchImpl = (async () =>
    jsonResponse(
      { error: { code: "unauthorized", message: "缺少 Authorization 头", detail: {} }, request_id: "req-9" },
      401,
      { "X-Request-Id": "req-9", "Retry-After": "7" },
    )) as unknown as typeof fetch;
  await assert.rejects(
    () => postChat("bad", { message: "hi", fetchImpl }),
    (exc: unknown) => {
      assert.ok(exc instanceof ApiError);
      assert.equal(exc.code, "unauthorized");
      assert.equal(exc.status, 401);
      assert.equal(exc.requestId, "req-9");
      assert.equal(exc.retryAfter, 7);
      assert.equal(exc.isUnauthorized, true, "401 是唯一该跳登录页的状态");
      return true;
    },
  );
});

test("非 JSON 响应（反代的 HTML 错误页）：也要有话说，不能只说「解析失败」", async () => {
  const fetchImpl = (async () =>
    new Response("<html>502 Bad Gateway</html>", { status: 502 })) as unknown as typeof fetch;
  await assert.rejects(
    () => postChat("tok", { message: "hi", fetchImpl }),
    (exc: unknown) => {
      assert.ok(exc instanceof ApiError);
      assert.equal(exc.status, 502);
      assert.match(exc.message, /非 JSON 响应/);
      return true;
    },
  );
});

test("契约不符（后端改了字段）：报 contract_mismatch，而不是静默给一个空对象", async () => {
  const fetchImpl = (async () => jsonResponse({ hello: "world" })) as unknown as typeof fetch;
  await assert.rejects(
    () => postChat("tok", { message: "hi", fetchImpl }),
    (exc: unknown) => {
      assert.ok(exc instanceof ApiError);
      assert.equal(exc.code, "contract_mismatch");
      assert.match(exc.message, /flow_state|不是对象/);
      return true;
    },
  );
});

test("postApprove：thread_id 原样回传，approved=false 也要发出去", async () => {
  let body: unknown = null;
  const fetchImpl = (async (_url: string | URL | Request, init?: RequestInit) => {
    body = JSON.parse(String(init?.body));
    return jsonResponse({ answer: "按未执行处理", flow_state: "blocked", thread_id: "u2:s1", session_id: "s1" });
  }) as unknown as typeof fetch;
  const response = await postApprove("tok", { threadId: "u2:s1", approved: false, reason: "风险", fetchImpl });
  assert.deepEqual(body, { thread_id: "u2:s1", approved: false, reason: "风险" });
  assert.equal(response.flow_state, "blocked");
});
