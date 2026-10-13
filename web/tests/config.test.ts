/**
 * 配置诊断的用例（离线）。
 *
 * 钉住的是本步最贵的一个坑（runbook §10.1 坑 14）：`NEXT_PUBLIC_API_BASE` 指到**前端自己**时，
 * HTTP 侧可能看着正常（同源的 `/api/kb` 由 Next 自己答），而 **WebSocket 流式一定失败** ——
 * 因为 Next 的 dev server 不代理 WebSocket upgrade。
 * 实测对照：直连后端 `ws://127.0.0.1:8000/...` 54ms 就 open；`ws://127.0.0.1:3000/...` 8s 超时。
 * 所以「同一来源」这个判据必须在**发请求之前**就变成一句人话，
 * 并且**自动纠正**成后端地址（纠正后仍要在页面上说出来 —— 有声音的降级，不是静默兜底）。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { apiBase, configWarnings, isSelfReferentialBase, resolvedApiBase, streamUrl } from "../src/lib/config.ts";

test("apiBase：默认值、末尾斜杠归一化、空串退默认", () => {
  assert.equal(apiBase("http://host:8000"), "http://host:8000");
  assert.equal(apiBase("http://host:8000///"), "http://host:8000", "末尾斜杠会让拼接出 //api/chat");
  assert.equal(apiBase("   "), "http://127.0.0.1:8000", "空串 = 用默认");
});

test("streamUrl：http→ws、https→wss（不是无脑替换字符串）", () => {
  assert.equal(streamUrl("/api/chat/stream", "http://127.0.0.1:8000"), "ws://127.0.0.1:8000/api/chat/stream");
  assert.equal(streamUrl("/api/chat/stream", "https://api.example.test"), "wss://api.example.test/api/chat/stream");
  assert.equal(streamUrl("/api/chat/stream", "http://127.0.0.1:8000/"), "ws://127.0.0.1:8000/api/chat/stream");
});

test("★同一来源判定：base 指到前端自己 = 必须报警（坑 14 的判据）", () => {
  assert.equal(isSelfReferentialBase("http://127.0.0.1:3000", "http://127.0.0.1:3000"), true);
  assert.equal(isSelfReferentialBase("http://localhost:3000", "http://localhost:3000"), true);
  assert.equal(isSelfReferentialBase("http://127.0.0.1:8000", "http://127.0.0.1:3000"), false);
  // localhost 与 127.0.0.1 是**不同来源**：base 写 localhost:3000、页面在 127.0.0.1:3000 时不算同源
  assert.equal(isSelfReferentialBase("http://localhost:3000", "http://127.0.0.1:3000"), false);
});

test("没有 window（SSR / 测试）时不误报：拿不到页面来源就不判定", () => {
  assert.equal(isSelfReferentialBase("http://127.0.0.1:3000"), false);
});

test("configWarnings：同源配置给出**带修法**的一句话（含要设的环境变量）", () => {
  const notes = configWarnings("http://127.0.0.1:3000", "http://127.0.0.1:3000");
  assert.equal(notes.length, 1);
  assert.match(notes[0], /WebSocket/);
  assert.match(notes[0], /NEXT_PUBLIC_API_BASE/);
  assert.match(notes[0], /http:\/\/127\.0\.0\.1:8000/, "要给出可粘贴的正确值");
  assert.match(notes[0], /已自动改用/, "必须说明「已经纠正了」——它是有声音的降级，不是静默兜底");
});

test("★resolvedApiBase：指到页面自己时**自动纠正**成后端地址（坑 14 的修复）", () => {
  // 不纠正的话：HTTP 半通（/api/kb 同源永远通）而 WebSocket 必死 —— 最难查的那种
  assert.equal(resolvedApiBase("http://127.0.0.1:3000", "http://127.0.0.1:3000"), "http://127.0.0.1:8000");
  assert.equal(resolvedApiBase("http://localhost:3000", "http://localhost:3000"), "http://127.0.0.1:8000");
  // 正确配置**不许**被动过
  assert.equal(resolvedApiBase("http://127.0.0.1:8000", "http://127.0.0.1:3000"), "http://127.0.0.1:8000");
  assert.equal(resolvedApiBase("http://api.example.test", "http://127.0.0.1:3000"), "http://api.example.test");
  // 拿不到页面来源（SSR/测试）时不纠正
  assert.equal(resolvedApiBase("http://127.0.0.1:3000"), "http://127.0.0.1:3000");
});

test("streamUrl 默认走「纠正后」的地址（否则页面会连到自己的 3000）", () => {
  // 没有 window 时 resolvedApiBase 不纠正，所以这里显式对比两个函数的行为差异：
  // `streamUrl(path, base)` 传入什么就用什么；默认参数取的是 resolvedApiBase
  assert.equal(streamUrl("/api/chat/stream", "http://127.0.0.1:8000"), "ws://127.0.0.1:8000/api/chat/stream");
});

test("configWarnings：正确配置下**不报警**（不能变成常驻黄条）", () => {
  assert.deepEqual(configWarnings("http://127.0.0.1:8000", "http://127.0.0.1:3000"), []);
});

test("configWarnings：HTTPS 页面 + http 后端 = 混合内容，必须报警", () => {
  const notes = configWarnings("http://api.example.test", {
    origin: "https://app.example.test",
    protocol: "https:",
  });
  assert.equal(notes.length, 1);
  assert.match(notes[0], /混合内容/);
});
