/**
 * 「探活失败」提示语的用例（离线）。
 *
 * 为什么一句话值得单独测（见 `src/lib/diagnostics.ts` 的模块注释）：
 * 浏览器里 `Failed to fetch` 有**三种**成因，而最容易被误判的是
 * 「跨源被拦掉」—— 那时候服务端日志里全是 200，用户却在怀疑令牌和后端进程。
 * 第 16 步实测就踩在这上面，所以这条提示语必须把顺序钉在代码里（而不是靠人记得）。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { needLoginHint, probeFailureHint } from "../src/lib/diagnostics.ts";

const TYPE_ERROR = new TypeError("Failed to fetch");

test("提示语第一句就是「先别怀疑令牌」（最容易被误判的一条）", () => {
  const hint = probeFailureHint(TYPE_ERROR, { apiBase: "http://127.0.0.1:8000", webOrigin: "http://127.0.0.1:3000" });
  assert.match(hint, /先别怀疑令牌/);
  assert.match(hint, /401/);
  assert.match(hint, /Failed to fetch/);
});

test("提示语把「谁连谁」写进去（排查时不用猜地址）", () => {
  const hint = probeFailureHint(TYPE_ERROR, { apiBase: "http://127.0.0.1:8000", webOrigin: "http://127.0.0.1:3000" });
  assert.match(hint, /http:\/\/127\.0\.0\.1:3000/);
  assert.match(hint, /http:\/\/127\.0\.0\.1:8000/);
});

test("提示语给出三条可执行的动作（起后端 / 查地址 / 查 CORS）", () => {
  const hint = probeFailureHint(TYPE_ERROR, { apiBase: "http://127.0.0.1:8000", webOrigin: "http://127.0.0.1:3000" });
  assert.match(hint, /uvicorn app\.main:app/);
  assert.match(hint, /API_CORS_ORIGINS=http:\/\/127\.0\.0\.1:3000/, "要把能直接粘进 .env 的那一行写出来");
  assert.match(hint, /Network 面板/);
});

test("非 Error 的异常也能翻（不抛、不显示 undefined）", () => {
  const hint = probeFailureHint("boom", { apiBase: "http://x", webOrigin: "http://y" });
  assert.match(hint, /boom/);
  assert.ok(!hint.includes("undefined"));
});

test("没有 window 时（SSR / 测试）退化成占位文案，不抛", () => {
  const hint = probeFailureHint(TYPE_ERROR, { apiBase: "http://x" });
  assert.ok(hint.length > 0);
});

test("未登录提示是另一句话（别把「没登录」说成「连不上」）", () => {
  assert.match(needLoginHint("customer"), /对客/);
  assert.match(needLoginHint("internal"), /对内/);
  assert.ok(!needLoginHint("internal").includes("连不上"));
});
