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
import { initialState, openChatStream, type WebSocketLike } from "../src/lib/stream.ts";

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

/** 看门狗默认在测试里关掉：假 socket 永远不会自己 `open`（见 runbook §10.1 坑 16）。 */
const NO_WATCHDOG = { handshakeTimeoutMs: 0, frameTimeoutMs: 0 } as const;

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
    ...NO_WATCHDOG,
  });
  return { handle, socket: () => socket as unknown as FakeSocket, states };
}

/** 造一个能同时拿到多条连接的装置（模拟 React 反复建连）。 */
function multiConnect(token = "tok-123") {
  const sockets: FakeSocket[] = [];
  const create = (url: string, protocols: string[]) => {
    const socket = new FakeSocket(url, protocols);
    sockets.push(socket);
    return socket;
  };
  const open = () =>
    openChatStream({
      token,
      url: "ws://example.test/api/chat/stream",
      socketFactory: create,
      onState: () => {},
      onClose: () => {},
      now: () => 1_000,
      ...NO_WATCHDOG,
    });
  return { sockets, open };
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

/**
 * ★ 这条是**实测故障的防回归**（runbook §10.1 坑 20）：握手成功后 phase 必须离开 `connecting`。
 *
 * 现场症状：对客页徽章永远「正在连接… Ns」秒数一直涨、发消息没反应，而后端完全健康
 * （`WS /api/chat/stream [accepted]`、`open` 都正常）。死锁是这样形成的：
 *
 * ```text
 * ① useChatStream.send() 判 phase === "connecting" → 把消息排队，**不发送**；
 * ② 那条排队只在 open 事件里冲一次 —— 而 open 早就在用户点发送之前发生了，队列当时是空的；
 * ③ 后端不会主动发帧 → 收不到帧 → 而 phase 唯一能离开 connecting 的路径就是「收到帧」。
 * ```
 *
 * 于是「没发 → 没帧 → 状态不动 → 更不发」自我锁死，页面一条消息都出不去。
 * 修法是：open 时把 phase 推到 `idle`（可发状态）。本用例钉住那个推进。
 */
test("★握手成功后 phase 必须离开 connecting（否则 send 会永远排队 —— 坑 20）", () => {
  const { handle, socket, states } = connect();
  assert.equal(handle.state().phase, "connecting", "建连后初始状态就是握手忙");
  assert.equal(socket().readyState, 0);

  socket().readyState = 1; // 握手成功（浏览器里 readyState 变成 OPEN）
  socket().emit("open", {});

  assert.notEqual(
    handle.state().phase,
    "connecting",
    "open 之后 phase 还停在 connecting：send 会把消息一直排进 pendingRef，" +
      "而冲队列的时机（open）已经过去 → 页面永远发不出任何消息（实测故障）",
  );
  assert.equal(handle.state().phase, "idle", "握手成功后应当是「可发」状态");
  assert.deepEqual(states.at(-1), "idle:0", "这个推进必须**推给调用方**（页面据此显示已连接）");
});

test("★握手成功后，onOpen 与 await ready() 照旧（推进 phase 不许顶掉它们）", async () => {
  let opened = 0;
  let ref: FakeSocket | null = null;
  const handle = openChatStream({
    token: "tok-123",
    url: "ws://example.test/api/chat/stream",
    socketFactory: (url, protocols) => {
      ref = new FakeSocket(url, protocols);
      return ref;
    },
    onState: () => {},
    onOpen: () => {
      opened += 1;
    },
    now: () => 5_000,
    ...NO_WATCHDOG,
  });
  const socket = ref as unknown as FakeSocket;
  socket.readyState = 1;
  socket.emit("open", {});
  await handle.ready();
  assert.equal(opened, 1, "onOpen 必须被调用（排队中的消息靠它发出）");
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
  const sockets: FakeSocket[] = [];
  const handle = openChatStream({
    token: "t",
    url: "ws://example.test/x",
    socketFactory: (url, protocols) => {
      const socket = new FakeSocket(url, protocols);
      sockets.push(socket);
      return socket;
    },
    onState: () => {},
    onClose: (code) => codes.push(code),
    ...NO_WATCHDOG,
  });
  sockets[0].emit("close", { code: 4401 });
  assert.deepEqual(codes, [4401]);
  assert.equal(handle.state().phase, "failed");
  assert.match(handle.state().error?.message ?? "", /重新登录/);
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

test("socket error：话术里带**试过的 URL**、自查命令与要查的配置项", () => {
  const { handle, socket } = connect();
  socket().emit("error", {});
  assert.equal(handle.state().phase, "failed");
  const message = handle.state().error?.message ?? "";
  // 第一版只有一句「先确认后端在跑」，而实测那次故障里后端完全正常（坑 14）——
  // 所以这条用例钉住「必须带上 URL 与两个可执行动作」
  assert.match(message, /ws:\/\/example\.test\/api\/chat\/stream/, "要打印实际试过的 URL");
  assert.match(message, /check_web_cors/, "给出那条一次问完后端/前端/跨源的自查命令");
  assert.match(message, /NEXT_PUBLIC_API_BASE/, "指出要检查的配置项");
});

// ── 看门狗（坑 16）：没有它，「一直连接中」会永久卡住输入框 ────────────────────

test("★握手超时：既不 open 也不 error 时必须自己收线并报错（否则输入框永久禁用）", async () => {
  // 现场症状：「界面一直显示正在连接，没办法输入问题，点了新会话后就正常了」。
  // 「新会话」只是把状态清空，**没有换 socket** —— 所以卡住的一定是 phase=connecting。
  // 网络层吞掉 upgrade 时浏览器可能长时间既不 open 也不 error，必须自己兜。
  const sockets: FakeSocket[] = [];
  const states: string[] = [];
  const handle = openChatStream({
    token: "tok",
    url: "ws://example.test/api/chat/stream",
    socketFactory: (url, protocols) => {
      const socket = new FakeSocket(url, protocols);
      sockets.push(socket);
      return socket;
    },
    onState: (state) => states.push(state.phase),
    handshakeTimeoutMs: 20,
    frameTimeoutMs: 0,
  });
  assert.equal(handle.state().phase, "connecting");
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(handle.state().phase, "failed", "握手超时后必须变成 failed，界面才可能重新可用");
  assert.equal(handle.state().error?.code, "connect_timeout");
  assert.match(handle.state().error?.message ?? "", /check_web_cors/, "话术里要给出下一步动作");
  assert.equal(sockets[0].closed?.code, 1000, "超时时要主动收线，别把连接挂在后台");
  assert.deepEqual(states, ["connecting", "failed"]);
});

test("握手成功之后，看门狗**不许**再误报超时（否则正常连接会被自己掐断）", async () => {
  const sockets: FakeSocket[] = [];
  const handle = openChatStream({
    token: "tok",
    url: "ws://example.test/api/chat/stream",
    socketFactory: (url, protocols) => {
      const socket = new FakeSocket(url, protocols);
      sockets.push(socket);
      return socket;
    },
    onState: () => {},
    handshakeTimeoutMs: 20,
    frameTimeoutMs: 0,
  });
  sockets[0].emit("open", {});
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.notEqual(handle.state().phase, "failed");
  assert.equal(handle.state().error, null);
});

test("★等回帧超时：握手成功但这一轮没有任何帧时，也必须收线（服务端被重启/静默切断）", async () => {
  const sockets: FakeSocket[] = [];
  const handle = openChatStream({
    token: "tok",
    url: "ws://example.test/api/chat/stream",
    socketFactory: (url, protocols) => {
      const socket = new FakeSocket(url, protocols);
      sockets.push(socket);
      return socket;
    },
    onState: () => {},
    handshakeTimeoutMs: 0,
    frameTimeoutMs: 20,
  });
  sockets[0].emit("open", {});
  handle.ask("有人吗", "s1");
  assert.equal(handle.state().phase, "streaming");
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(handle.state().phase, "failed");
  assert.equal(handle.state().error?.code, "frame_timeout");
});

test("收到帧会重置等回帧的看门狗（慢回答不该被判成死连接）", async () => {
  const sockets: FakeSocket[] = [];
  const handle = openChatStream({
    token: "tok",
    url: "ws://example.test/api/chat/stream",
    socketFactory: (url, protocols) => {
      const socket = new FakeSocket(url, protocols);
      sockets.push(socket);
      return socket;
    },
    onState: () => {},
    handshakeTimeoutMs: 0,
    frameTimeoutMs: 40,
  });
  sockets[0].emit("open", {});
  handle.ask("慢问题", "s1");
  // 每 20ms 来一帧（< 40ms 的上限）：总时长超过上限，但不该被判超时
  for (let index = 1; index <= 4; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 20));
    sockets[0].emitFrame({
      event: "node",
      seq: index,
      ts: 1,
      data: { node: `n${index}`, progress: "", delta: {} },
    });
  }
  assert.equal(handle.state().phase, "streaming", "只要还在来帧，就不该被看门狗掐断");
  assert.equal(handle.state().error, null);
});

// ── 被抛弃的连接不许影响当前连接（坑 15：StrictMode 双挂载 / 热更新）──────────

/**
 * 建一条连接并把状态收进 `box`。
 *
 * ⚠️ 首个参数是 `box` 而不是 `sockets`：**每次调用都必须新建一个 box** ——
 * 第一版写成 `const { sockets, open } = multiConnect()` 然后 `open()` 两次，
 * 结果**只有最后一次调用**的环境被记进 `onState`，于是「第一条连接的事件影响第二条」
 * 这件事根本测不到（用例会以 `sockets[1] is undefined` 的形式露馅，见 runbook §10.1 坑 15）。
 */
function openInto(box: { state: ReturnType<typeof initialState>; sockets: FakeSocket[] }) {
  return openChatStream({
    token: "tok",
    url: "ws://example.test/api/chat/stream",
    socketFactory: (url, protocols) => {
      const socket = new FakeSocket(url, protocols);
      box.sockets.push(socket);
      return socket;
    },
    onState: (next) => {
      box.state = next;
    },
    onClose: () => {},
    now: () => 1_000,
    ...NO_WATCHDOG,
  });
}

test("★被关闭的旧连接再抛 error：**不许**污染当前状态（StrictMode 双挂载的现实场景）", () => {
  // 现场证据（后端日志）：同一个客户端端口出现两条 `/api/chat/stream` accepted，
  // 之后一条被强行关掉。React 开发模式的 StrictMode 会把 effect 跑两遍：
  // 第一条连接被 cleanup.close() 掉，而它的 `error` 可能**在新连接已经工作之后**才到。
  // 若照单全收，用户会看到「聊得好好的，忽然报 WebSocket 连接失败」——正是实测那次。
  const box: { state: ReturnType<typeof initialState>; sockets: FakeSocket[] } = {
    state: initialState(),
    sockets: [],
  };

  // ① 第一条连接（StrictMode 的第一次挂载）：还没 open 就被 cleanup 关掉
  const first = openInto(box);
  first.close();
  box.sockets[0].emit("close", { code: 1000 });

  // ② 第二条连接（第二次挂载）：正常收发
  const second = openInto(box);
  second.ask("我的订单到哪了", "s1");
  box.sockets[1].emitFrame({
    event: "done",
    seq: 1,
    ts: 1,
    data: { flow_state: "delivered", thread_id: "u2:s1", session_id: "s1", blocked: false, approval: null },
  });
  assert.equal(box.state.phase, "done");
  assert.equal(box.sockets.length, 2, "应当有两条连接（第一条被抛弃、第二条在用）");

  // ③ 迟到的 error/close：来自**已经被抛弃的**第一条连接
  box.sockets[0].emit("error", {});
  box.sockets[0].emit("close", { code: 1006 });

  assert.equal(box.state.phase, "done", "迟到的 error 把 phase 打回 failed 了（用户会看到「连接失败」）");
  assert.equal(box.state.error, null, "迟到的 error 污染了错误提示");
});

test("被抛弃的连接抛 error 时，当前连接仍然是干净的", () => {
  const box: { state: ReturnType<typeof initialState>; sockets: FakeSocket[] } = {
    state: initialState(),
    sockets: [],
  };
  const first = openInto(box);
  const second = openInto(box);
  void first;
  box.sockets[0].emit("error", {});
  assert.equal(second.state().error, null, "旧连接的 error 不该出现在当前状态里");
  assert.notEqual(second.state().phase, "failed");
});

test("ask 之后重连：新连接能正常发消息（被抛弃的连接不影响 ask）", () => {
  const box: { state: ReturnType<typeof initialState>; sockets: FakeSocket[] } = {
    state: initialState(),
    sockets: [],
  };
  const first = openInto(box);
  first.close();
  box.sockets[0].emit("close", { code: 1000 });
  const second = openInto(box);
  second.ask("还在吗", "s1");
  assert.deepEqual(JSON.parse(box.sockets[1].sent[0]), { message: "还在吗", session_id: "s1" });
});

test("★主动 close() 之后的 1006 不算「连接失败」（实测事件序列就是 error → close(1006)）", () => {
  // 实测（Node 的 WebSocket 也一样）：**在 CONNECTING 阶段 close()** 会得到
  // `error → close(1006)` —— 因为浏览器还没握手就收到关闭请求。
  // React 开发模式的双挂载正好命中它（cleanup 在同一个 tick 里 close）。
  // 于是「用户主动放弃的连接」会被渲染成一句「WebSocket 连接失败」——
  // 这正是实测里「聊天能用但时不时弹报错」的成因（runbook §10.1 坑 15）。
  const { handle, socket } = connect();
  handle.close();
  socket().emit("close", { code: 1006 });
  assert.notEqual(handle.state().phase, "failed", "主动关闭不该被当成失败");
  assert.equal(handle.state().error, null, "主动关闭不该产生错误提示");
  assert.equal(handle.state().closeCode, 1006, "关闭码仍然如实记下来（信息，不是报错）");
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
