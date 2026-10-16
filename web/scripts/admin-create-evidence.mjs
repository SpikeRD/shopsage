/**
 * 管理台「开账号」的**真浏览器**取证脚本（issue 004）—— 一次性手工脚本，不是测试套件。
 *
 * ## 为什么必须用真浏览器
 *
 * issue 004 的三个缺陷**全部落在 React 组件这一层**，而本仓现有手段都够不到它：
 *
 * | 手段 | 为什么抓不到 |
 * |---|---|
 * | `tests/*.test.ts`（离线） | 不渲染 React、不发请求 |
 * | `hook-wiring.test.ts`（源码正则） | 只能查「有没有写某个东西」，查不出「漏了 `minLength`」这类**缺失** |
 * | 后端用例 | 自己填的口令**够长**，于是「前端发一个太短的口令」这条路径根本不存在 |
 *
 * 所以这里用 CDP 驱动无头 Chrome **真的点页面**：填表单 → 点「开账号」 →
 * 抓请求/响应、页面提示、表单残留。
 *
 * ## 它断言什么（三段，与 `issues/004` 的修复一一对应）
 *
 * 1. **口令框必须带 `minLength`**（规则在填的时候就看得见）；
 * 2. **短口令不发请求**（前端先在本地拦下，并给出能照做的原因）；
 * 3. **失败保留草稿、成功给出确认**（成功/失败在界面上必须长得不一样）。
 *
 * ⚠️ **本脚本会真的建账号**（成功路径）。它用 `probe-<时间戳>` 当标识，
 * 并在结束时把它删掉；跑之前请确认后端连的是**开发/演示库**。
 *
 * 用法（在 web/ 下；需要后端 + 前端都在跑）：
 *   node scripts/admin-create-evidence.mjs
 *   PROBE_PW=passphrase-12 PROBE_SUBJECT=8 node scripts/admin-create-evidence.mjs
 *
 * 环境变量：
 *   SHOPSAGE_WEB_BASE   前端地址（默认 http://127.0.0.1:3000）
 *   SHOPSAGE_API_BASE   后端地址（默认 http://127.0.0.1:8000）
 *   CHROME_BIN          Chrome/Edge 可执行文件（默认自动探测）
 *   PROBE_PW            初始口令（默认 "pass1234"，用来验「短口令被本地拦下」）
 *   PROBE_SUBJECT       主体 id（默认 8；**必须是当前没有账号的** user_id）
 */

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";

const WEB = process.env.SHOPSAGE_WEB_BASE ?? "http://127.0.0.1:3000";
const API = process.env.SHOPSAGE_API_BASE ?? "http://127.0.0.1:8000";
const PW = process.env.PROBE_PW ?? "pass1234";
const SUBJECT = process.env.PROBE_SUBJECT ?? "8";
const PORT = Number(process.env.CDP_PORT ?? 9336);

const CHROME_CANDIDATES = [
  process.env.CHROME_BIN,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** 找得到浏览器才跑得起来；找不到就**明确跳过**，而不是崩在一句无关的报错上。 */
function findBrowser() {
  for (const candidate of CHROME_CANDIDATES) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

async function waitJson(url, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let last = "";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
      last = `HTTP ${response.status}`;
    } catch (exc) {
      last = String(exc.message ?? exc);
    }
    await sleep(300);
  }
  throw new Error(`等 ${url} 超时：${last}`);
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 0;
    this.pending = new Map();
    this.listeners = [];
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result);
      } else if (message.method) {
        for (const listener of this.listeners) listener(message);
      }
    });
  }

  on(listener) {
    this.listeners.push(listener);
  }

  send(method, params = {}) {
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP 超时：${method}`));
        }
      }, 30000);
    });
  }

  async eval(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(`页面内异常：${result.exceptionDetails.exception?.description ?? "?"}`);
    }
    return result.result.value;
  }
}

/** React 受控输入：必须用**原生 setter** + 冒泡事件，否则 React 收不到这次改动。 */
function setInput(selector, value) {
  return `(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return "找不到 " + ${JSON.stringify(selector)};
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, ${JSON.stringify(value)});
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
    return "ok";
  })()`;
}

function clickButton(text) {
  return `(() => {
    const hit = [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === ${JSON.stringify(text)});
    if (!hit) return "找不到按钮 " + ${JSON.stringify(text)};
    hit.click();
    return "ok";
  })()`;
}

/** 页面当前的提示（成功渲染成 `.card`，失败渲染成 `.error-box`）。 */
const READ_NOTICE = `(() => {
  const err = document.querySelector(".error-box");
  if (err) return "ERROR: " + err.innerText.replace(/\\s+/g, " ").trim();
  const ok = [...document.querySelectorAll(".card")].find((c) => c.innerText.includes("已开账号"));
  return ok ? "OK: " + ok.innerText.replace(/\\s+/g, " ").trim() : "(无提示)";
})()`;

const READ_FORM = `JSON.stringify({
  identifier: document.querySelector('input[aria-label="登录标识"]')?.value ?? null,
  password: document.querySelector('input[aria-label="初始口令"]')?.value ?? null,
  subjectId: document.querySelector('input[aria-label="主体 id"]')?.value ?? null,
})`;

/** 直接调后端删掉探针造的账号（脚本自己收拾现场）。 */
async function deleteAccount(identifier) {
  const login = await fetch(`${API}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identifier: "admin", password: "admin-demo-pass" }),
  });
  if (!login.ok) return `跳过清理（管理员登录失败 HTTP ${login.status}）`;
  const token = (await login.json()).access_token;
  const list = await fetch(`${API}/api/admin/accounts?limit=500`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!list.ok) return `跳过清理（列账号失败 HTTP ${list.status}）`;
  const found = (await list.json()).accounts.find((a) => a.identifier === identifier);
  if (!found) return "无需清理";
  const removed = await fetch(`${API}/api/admin/accounts/${found.account_id}`, {
    method: "PATCH",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ status: "disabled" }),
  });
  return removed.ok ? `已停用 ${identifier}（后端没有删除接口，停用即退出可用态）` : `清理失败 HTTP ${removed.status}`;
}

async function main() {
  const browser = findBrowser();
  if (!browser) {
    console.log("SKIP 找不到 Chrome/Edge：设 CHROME_BIN 指向浏览器可执行文件再跑");
    process.exitCode = 0;
    return;
  }

  const profile = mkdtempSync(path.join(tmpdir(), "shopsage-admin-probe-"));
  const child = spawn(browser, [
    "--headless=new",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu",
    "about:blank",
  ], { stdio: "ignore" });

  const results = [];
  const check = (label, ok, detail) => {
    results.push({ label, ok });
    console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? `\n      ${detail}` : ""}`);
  };

  let created = null;
  try {
    await waitJson(`http://127.0.0.1:${PORT}/json/version`, 30000);
    // 新版 Chrome 的 /json/new 只接受 PUT（GET 会 405）
    const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve);
      socket.addEventListener("error", reject);
    });
    const cdp = new Cdp(socket);
    await cdp.send("Runtime.enable");
    await cdp.send("Page.enable");
    await cdp.send("Network.enable");

    /** 抓管理台的开账号请求（用来证明「短口令**没有**发出去」）。 */
    const requests = [];
    const pending = new Map();
    cdp.on((message) => {
      if (message.method === "Network.requestWillBeSent") {
        pending.set(message.params.requestId, {
          url: message.params.request.url,
          method: message.params.request.method,
          postData: message.params.request.postData,
        });
      }
      if (message.method === "Network.responseReceived") {
        const info = pending.get(message.params.requestId);
        if (info?.url.includes("/api/admin/accounts") && info.method === "POST") {
          requests.push({ ...info, status: message.params.response.status });
        }
      }
    });

    console.log(`浏览器  = ${browser}`);
    console.log(`前端    = ${WEB}`);
    console.log(`后端    = ${API}`);
    console.log(`口令    = ${JSON.stringify(PW)}（${PW.length} 位）  主体 id = ${SUBJECT}\n`);

    // ── 登录 ────────────────────────────────────────────────────────────────
    await cdp.send("Page.navigate", { url: `${WEB}/login` });
    await sleep(2500);
    await cdp.eval(setInput("#login-identifier", "admin"));
    await cdp.eval(setInput("#login-password", "admin-demo-pass"));
    await cdp.eval(clickButton("登录"));
    await sleep(3500);
    const landed = await cdp.eval("location.pathname");
    check("管理员登录后落在 /admin", landed === "/admin", `实际 ${landed}`);

    // ── 判据 ①：口令框必须声明 minLength ────────────────────────────────────
    const minLength = await cdp.eval(
      `document.querySelector('input[aria-label="初始口令"]')?.minLength ?? -1`,
    );
    check(
      "口令框带 minLength=12（规则在填的时候就看得见）",
      Number(minLength) === 12,
      `实际 minLength=${minLength}（修复前是 -1：策略只存在于后端）`,
    );

    // ── 判据 ②：短口令被本地拦下、不发请求 ──────────────────────────────────
    const stamp = Date.now().toString(36).slice(-6);
    const shortId = `probe-short-${stamp}`;
    await cdp.eval(setInput('input[aria-label="登录标识"]', shortId));
    await cdp.eval(setInput('input[aria-label="初始口令"]', PW));
    await cdp.eval(setInput('input[aria-label="主体 id"]', SUBJECT));
    await cdp.eval(clickButton("开账号"));
    await sleep(3000);

    const notice = await cdp.eval(READ_NOTICE);
    const form = JSON.parse(await cdp.eval(READ_FORM));
    const sent = requests.length;

    if (PW.length < 12) {
      check("口令太短：请求**没有**发出去（不再白跑一次后端）", sent === 0, `实际发了 ${sent} 次`);
      check(
        "口令太短：提示语能照做（写明要求几位、当前几位）",
        /至少\s*12\s*位/.test(notice) && /当前\s*\d+\s*位/.test(notice),
        notice,
      );
      check(
        "口令太短：草稿**保留**（不用全部重填）",
        form.identifier === shortId && form.password === PW,
        JSON.stringify(form),
      );
    } else {
      check("合法口令：后端返回 201", requests.at(-1)?.status === 201, `实际 ${requests.at(-1)?.status}`);
      const listed = await cdp.eval(`document.body.innerText.includes(${JSON.stringify(shortId)})`);
      check("合法口令：新账号出现在列表里", listed === true);
      check("合法口令：给出**成功确认**（而不是一片空白）", notice.startsWith("OK:"), notice);
      created = shortId;
    }

    socket.close();
  } finally {
    child.kill();
    if (created) console.log(`\n清理：${await deleteAccount(created)}`);
  }

  const failed = results.filter((item) => !item.ok);
  console.log(`\n${failed.length === 0 ? "全部通过" : `${failed.length} 条失败`}（共 ${results.length} 条）`);
  process.exitCode = failed.length === 0 ? 0 : 1;
}

void main().catch((exc) => {
  console.error(`取证脚本失败：${exc.stack ?? exc}`);
  process.exitCode = 1;
});
