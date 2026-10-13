/**
 * 帧时序的单元测试（**离线、零依赖**）：喂一串帧进去，看状态推进对不对。
 *
 * 这是本步最值钱的一组用例，理由（`docs/runbook/16.web-runbook.md` §7）：
 * 「先骨架、再节点、后回答」这条时序是 B2 的落点，而它**必须能离线测**。
 * 起浏览器 + 真后端去测它，等于把一条协议级的断言绑在一堆环境条件上
 * （后端在不在、模型快不快、端口占没占），红的时候你分不清是协议错了还是环境不对。
 *
 * ⚠️ 用例里的帧是**逐字抄**后端 `app/api/chat.py::_stream_turn` 的形状，
 * 抄错的表现是「用例过了但真链路还是坏的」——所以 `live.test.ts` 里另有一条
 * 对着真后端的断言（两种证据都要，§7.4）。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { parseFrame, WS_CLOSE_RATE_LIMITED, WS_CLOSE_UNAUTHORIZED } from "../src/lib/contracts.ts";
import {
  beginTurn,
  closeCodeMessage,
  initialState,
  missingSeqs,
  reduceStream,
  toTurnResult,
} from "../src/lib/stream.ts";

function feed(state: ReturnType<typeof initialState>, frames: unknown[], at = 1_000) {
  let current = state;
  for (const frame of frames) {
    current = reduceStream(current, parseFrame(frame), at);
  }
  return current;
}

/** 一轮「对客查订单」的真实帧序列（形状抄自后端；`citations` 为空 = 数据类问题）。 */
export const DELIVERED_FRAMES = [
  { event: "skeleton", seq: 1, ts: 1.0, data: { phase: "accepted", text: "收到，正在理解你的问题…" } },
  { event: "node", seq: 2, ts: 1.01, data: { node: "guardrail_input", progress: "正在做安全校验…", delta: { blocked: false } } },
  { event: "node", seq: 3, ts: 1.02, data: { node: "router", progress: "正在判断你的意图…", delta: { route: "data", route_confidence: 0.91 } } },
  { event: "skeleton", seq: 4, ts: 1.03, data: { phase: "routing", route: "data", text: "正在查询你的业务数据，结论马上给你…" } },
  { event: "node", seq: 5, ts: 1.04, data: { node: "data", progress: "正在查询数据…", delta: { row_count: 2 } } },
  { event: "node", seq: 6, ts: 1.05, data: { node: "synthesis", progress: "正在组织回答…", delta: { answer: "您最近 2 笔订单：1 笔已发货、1 笔待付款。" } } },
  { event: "node", seq: 7, ts: 1.06, data: { node: "guardrail_output", progress: "正在做输出合规检查…", delta: { citations: [] } } },
  {
    event: "answer",
    seq: 8,
    ts: 1.07,
    data: { text: "您最近 2 笔订单：1 笔已发货、1 笔待付款。", done: true, degraded: false, citations: [], chart: false },
  },
  {
    event: "done",
    seq: 9,
    ts: 1.08,
    data: {
      flow_state: "delivered",
      thread_id: "u2:orders-1",
      session_id: "orders-1",
      route: "data",
      blocked: false,
      approval: null,
      latency_ms: 12.5,
      first_frame_ms: 0.13,
      debug: null,
    },
  },
];

test("一轮正常问答：phase/session/thread/route 全部就位", () => {
  const state = feed(beginTurn(initialState()), DELIVERED_FRAMES);
  assert.equal(state.phase, "done");
  assert.equal(state.sessionId, "orders-1");
  assert.equal(state.threadId, "u2:orders-1");
  assert.equal(state.route, "data");
  assert.equal(state.flowState, "delivered");
  assert.equal(state.answer, "您最近 2 笔订单：1 笔已发货、1 笔待付款。");
  assert.equal(state.serverFirstFrameMs, 0.13);
  assert.equal(state.latencyMs, 12.5);
  assert.equal(state.firstFrameMs, 1_000, "客户端首帧时间戳由注入的时间源决定");
});

test("帧序号连续：missingSeqs 为空（丢帧自检的口径）", () => {
  const state = feed(beginTurn(initialState()), DELIVERED_FRAMES);
  assert.deepEqual(state.seqs, [1, 2, 3, 4, 5, 6, 7, 8, 9]);
  assert.deepEqual(missingSeqs(state.seqs), []);
});

test("骨架两帧：界面上的提示语取**最后一条**（路由确定后收敛）", () => {
  const state = feed(beginTurn(initialState()), DELIVERED_FRAMES);
  assert.deepEqual(state.skeleton, ["收到，正在理解你的问题…", "正在查询你的业务数据，结论马上给你…"]);
});

test("重复的骨架文案不重复追加（避免界面上刷一串一样的话）", () => {
  const duplicated = [
    { event: "skeleton", seq: 1, ts: 1, data: { phase: "accepted", text: "正在理解你的问题…" } },
    { event: "skeleton", seq: 2, ts: 1, data: { phase: "accepted", text: "正在理解你的问题…" } },
  ];
  const state = feed(beginTurn(initialState()), duplicated);
  assert.deepEqual(state.skeleton, ["正在理解你的问题…"]);
});

test("节点帧去重保序：重复的节点只记一次（图重放时会出现同名节点）", () => {
  const frames = [
    { event: "node", seq: 1, ts: 1, data: { node: "guardrail_input", progress: "正在做安全校验…", delta: {} } },
    { event: "node", seq: 2, ts: 1, data: { node: "guardrail_input", progress: "正在做安全校验…", delta: {} } },
    { event: "node", seq: 3, ts: 1, data: { node: "router", progress: "正在判断你的意图…", delta: {} } },
  ];
  const state = feed(beginTurn(initialState()), frames);
  assert.deepEqual(state.nodes, ["guardrail_input", "router"]);
  assert.equal(state.nodeProgress.router, "正在判断你的意图…");
});

test("出处串从 answer 帧进入状态（溯源展示的入口）", () => {
  const frames = [
    ...DELIVERED_FRAMES.slice(0, 7),
    {
      event: "answer",
      seq: 8,
      ts: 1.07,
      data: { text: "按退货政策，7 天无理由 [1]。", done: true, degraded: false, citations: ["退货政策.md p.3#c12"], chart: false },
    },
    { ...DELIVERED_FRAMES[8], seq: 9 },
  ];
  const state = feed(beginTurn(initialState()), frames);
  assert.deepEqual(state.citations, ["退货政策.md p.3#c12"]);
});

test("answer 帧的 chart 布尔：WS 只告诉「有没有图」，本体在 HTTP 响应里", () => {
  const frames = [
    ...DELIVERED_FRAMES.slice(0, 7),
    { event: "answer", seq: 8, ts: 1.07, data: { text: "见图", done: true, degraded: false, citations: [], chart: true } },
    { ...DELIVERED_FRAMES[8], seq: 9 },
  ];
  const state = feed(beginTurn(initialState()), frames);
  assert.equal(state.hasChart, true);
});

test("挂起（待人工确认）：approval 落进状态，flow_state=approval_required", () => {
  const frames = [
    { event: "skeleton", seq: 1, ts: 1, data: { phase: "accepted", text: "收到…" } },
    { event: "node", seq: 2, ts: 1, data: { node: "gate", progress: "等待人工确认…", delta: { blocked: false } } },
    { event: "answer", seq: 3, ts: 1, data: { text: "这项操作需要人工确认后再执行。", done: true, degraded: false, citations: [], chart: false } },
    {
      event: "done",
      seq: 4,
      ts: 1,
      data: {
        flow_state: "approval_required",
        thread_id: "u2:demo-approve",
        session_id: "demo-approve",
        route: "refund",
        blocked: false,
        approval: { thread_id: "u2:demo-approve", question: "请确认是否继续执行", interrupt_id: "e77f", resume_with: "POST /api/chat/approve" },
        latency_ms: 30,
        first_frame_ms: 0.2,
        debug: null,
      },
    },
  ];
  const state = feed(beginTurn(initialState()), frames);
  assert.equal(state.flowState, "approval_required");
  assert.equal(state.approval?.threadId, "u2:demo-approve");
  assert.equal(state.approval?.resumeWith, "POST /api/chat/approve");
  assert.equal(state.blocked, false);
});

test("被拦下：flow_state=blocked + answer 是安全话术（前端照实展示）", () => {
  const frames = [
    { event: "skeleton", seq: 1, ts: 1, data: { phase: "accepted", text: "收到…" } },
    { event: "answer", seq: 2, ts: 1, data: { text: "抱歉，这个问题我不能回答。", done: true, degraded: false, citations: [], chart: false } },
    {
      event: "done",
      seq: 3,
      ts: 1,
      data: { flow_state: "blocked", thread_id: "u2:blocked", session_id: "blocked", route: "", blocked: true, approval: null, latency_ms: 5, first_frame_ms: 0.1, debug: null },
    },
  ];
  const state = feed(beginTurn(initialState()), frames);
  assert.equal(state.flowState, "blocked");
  assert.equal(state.blocked, true);
  assert.equal(state.answer, "抱歉，这个问题我不能回答。");
});

test("error 帧**不结束这一轮**：后端对「这条消息不合法」只回一条错误帧，连接继续用", () => {
  const frames = [
    { event: "skeleton", seq: 1, ts: 1, data: { phase: "accepted", text: "收到…" } },
    { event: "error", seq: 2, ts: 1, data: { code: "empty_message", message: "message 不能为空" } },
  ];
  const state = feed(beginTurn(initialState()), frames);
  assert.equal(state.error?.code, "empty_message");
  assert.equal(state.phase, "streaming", "仍然是 streaming：用户改一句话还能继续发");
});

test("未知 event / 非法 JSON：parseFrame 抛错而不是静默吞掉", () => {
  assert.throws(() => parseFrame('{"event":"chatty","seq":1,"ts":1,"data":{}}'), /未知的帧类型/);
  assert.throws(() => parseFrame("这不是 JSON"), /不是合法 JSON/);
  assert.throws(() => parseFrame({ event: "answer", seq: 0, ts: 1, data: {} }), /seq 必须是/);
});

test("beginTurn 保留会话与 thread_id，清掉上一轮的产物", () => {
  const done = feed(beginTurn(initialState()), DELIVERED_FRAMES);
  const next = beginTurn(done);
  assert.equal(next.phase, "streaming");
  assert.equal(next.sessionId, "orders-1", "会话要留着（多轮对话接得上）");
  assert.equal(next.threadId, "u2:orders-1");
  assert.equal(next.answer, "", "上一轮的回答必须清掉");
  assert.deepEqual(next.seqs, []);
  assert.deepEqual(next.nodes, []);
  assert.equal(next.flowState, null);
});

test("toTurnResult 把状态翻成一轮的产物", () => {
  const state = feed(beginTurn(initialState()), DELIVERED_FRAMES);
  const turn = toTurnResult(state);
  assert.equal(turn.flowState, "delivered");
  assert.equal(turn.firstFrameMs, 0.13);
  assert.equal(turn.threadId, "u2:orders-1");
});

test("关闭码话术：每一条都指向一个动作（4401 重新登录 / 4429 退避）", () => {
  assert.match(closeCodeMessage(WS_CLOSE_UNAUTHORIZED), /重新登录/);
  assert.match(closeCodeMessage(WS_CLOSE_RATE_LIMITED), /频繁/);
  assert.match(closeCodeMessage(1006), /1006/);
});

test("missingSeqs 能发现断档（自检用）", () => {
  assert.deepEqual(missingSeqs([1, 2, 4]), [3]);
  assert.deepEqual(missingSeqs([1, 3, 5]), [2, 4]);
  assert.deepEqual(missingSeqs([]), []);
});
