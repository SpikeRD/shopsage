/**
 * 「等 LLM 回复时退出登录 → 再也登不进去」的**真浏览器**取证脚本（issue 007）。
 *
 * 场景（用户原话）：
 *   在等待 llm 回复的过程中退出登录会导致无法再次登录，一直显示登录中。
 *
 * 本脚本用 CDP 驱动真 Chrome 复刻这条路径：
 *   ① 登录客户 → 落在 `/`；
 *   ② 发一句要走 RAG 的问题（本地 reranker 冷启动，回帧窗口够长）；
 *   ③ 在**还没收到 answer/done 帧**的窗口里点侧边栏「退出登录」；
 *   ④ 回到 `/login` 再登一次，看按钮是不是永远停在「登录中…」。
 *
 * 用法（在 web/ 下；需要后端 + 前端都在跑）：
 *   node scripts/logout-during-llm-evidence.mjs
 */

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";

const WEB = process.env.SHOPSAGE_WEB_BASE ?? "http://127.0.0.1:3000";
const API = process.env.SHOPSAGE_API_BASE ?? "http://127.0.0.1:8000";
const PORT = Number(process.env.CDP_PORT ?? 9355);
const IDENTIFIER = process.env.PROBE_ACCOUNT ?? "13800000002";
const PASSWORD = process.env.PROBE_PASSWORD ?? "customer-demo-pass";
const QUESTION = process.env.PROBE_QUESTION ?? "退货政策是怎么规定的？";

const CHROME_CANDIDATES = [
  process.env.CHROME_BIN,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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

function setInput(selector, value) {
  return `(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return "找不到 " + ${JSON.stringify(selector)};
    const proto = HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, ${JSON.stringify(value)});
    el.dispatchEvent(new Event("input", { bubbles: true }));
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

/** 页面上那个登录按钮现在写着什么（「登录中…」就是卡住的那一态）。 */
const LOGIN_BUTTON_TEXT = `(() => {
  const btn = [...document.querySelectorAll("button")].find((b) => /登录/.test(b.textContent));
  return btn ? { text: btn.textContent.trim(), disabled: btn.disabled } : null;
})()`;

async function main() {
  const browser = findBrowser();
  if (!browser) {
    console.log("SKIP 找不到 Chrome/Edge：设 CHROME_BIN 指向浏览器可执行文件再跑");
    return;
  }

  const results = [];
  const check = (label, ok, detail) => {
    results.push({ label, ok });
    console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? `\n      ${detail}` : ""}`);
  };

  const profile = mkdtempSync(path.join(tmpdir(), "shopsage-logout-evidence-"));
  const child = spawn(browser, [
    "--headless=new",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu",
    "about:blank",
  ], { stdio: "ignore" });

  try {
    await waitJson(`http://127.0.0.1:${PORT}/json/version`, 30000);
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
    await cdp.send("Log.enable");
    await cdp.send("Console.enable");

    const consoleLines = [];
    const requests = [];
    cdp.on((message) => {
      if (message.method === "Runtime.consoleAPICalled") {
        const text = (message.params.args ?? []).map((a) => a.value ?? a.description ?? "").join(" ");
        consoleLines.push(`[${message.params.type}] ${text}`);
      }
      if (message.method === "Log.entryAdded") {
        consoleLines.push(`[log:${message.params.entry.level}] ${message.params.entry.text}`);
      }
      if (message.method === "Network.requestWillBeSent") {
        requests.push({ url: message.params.request.url, method: message.params.request.method });
      }
    });

    console.log(`浏览器 = ${browser}`);
    console.log(`前端   = ${WEB}`);
    console.log(`后端   = ${API}`);
    console.log(`账号   = ${IDENTIFIER}`);
    console.log(`问题   = ${QUESTION}\n`);

    // ── ① 登录 ──────────────────────────────────────────────────────────────
    await cdp.send("Page.navigate", { url: `${WEB}/login` });
    await sleep(3000);
    await cdp.eval(setInput("#login-identifier", IDENTIFIER));
    await cdp.eval(setInput("#login-password", PASSWORD));
    await cdp.eval(clickButton("登录"));
    await sleep(4000);
    const landed = await cdp.eval("location.pathname");
    check("客户登录后落在 /", landed === "/", `实际 ${landed}`);
    if (landed !== "/") throw new Error("没进对客页，后续断言无意义");

    // ── ② 发问题，等流式回帧 ─────────────────────────────────────────────────
    await cdp.eval(`(() => {
      const el = document.querySelector("textarea, input.input");
      if (!el) return "找不到输入框";
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, ${JSON.stringify(QUESTION)});
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return "ok";
    })()`);
    await sleep(300);
    const sent = await cdp.eval(clickButton("发送"));
    console.log(`发送问题：${sent}`);

    // ── 等「正在等 LLM」的中间态 ─────────────────────────────────────────────
    //
    // ⚠️ **不能等「正在检索知识库…」那类节点文案**：`_stream_turn` 是「先跑完图、
    // 再把节点帧一次性补发」（见 `app/api/chat.py` 的说明），所以那些进度文案出现的
    // 时刻，图**已经跑完了** —— 那时再退出登录，断开的是「回合已结束」的连接，
    // 根本踩不到故障窗口。真正的「等 LLM」窗口只有首帧骨架到 answer 帧之间那一段。
    //
    // 所以判据是**「骨架已到、回答还没到」**：页面已经显示「正在理解你的问题…」
    // 这样的骨架话术（首帧），而气泡里还没有回答。
    let sawStreaming = false;
    for (let i = 0; i < 40; i += 1) {
      const phase = await cdp.eval(`(() => {
        const text = document.body.innerText;
        return {
          skeleton: /正在(理解|准备|查询|检索|汇总|整理|组织)/.test(text),
          answered: /来源/.test(text),
          approval: Boolean(document.querySelector(".approval")),
        };
      })()`);
      if (phase.skeleton && !phase.answered) { sawStreaming = true; break; }
      if (phase.approval) { sawStreaming = true; break; }
      await sleep(100);
    }
    check("点发送后进入「等 LLM 回复」的窗口（骨架已到、回答未到）", sawStreaming, "（页面显示骨架话术）");

    // ── ③ 在等 LLM 期间点「退出登录」────────────────────────────────────────
    // 这一刻图还在服务端跑（RAG + 本地 reranker 冷启动要好几秒），
    // `location.assign` 会卸载页面 → 浏览器关掉那条 WS。
    const clicked = await cdp.eval(clickButton("退出登录"));
    console.log(`退出登录：${clicked}`);
    await sleep(3000);
    const afterLogout = await cdp.eval("location.pathname");
    check("点退出登录后回到 /login", afterLogout === "/login", `实际 ${afterLogout}`);

    const storage = await cdp.eval(`(() => {
      const out = {};
      for (let i = 0; i < localStorage.length; i += 1) {
        const k = localStorage.key(i);
        out[k] = k.startsWith("shopsage.token") || k.startsWith("shopsage.refresh") ? "<有值>" : localStorage.getItem(k);
      }
      return out;
    })()`);
    console.log(`  localStorage 现状：${JSON.stringify(storage)}`);

    // ── ④ 再登录一次 —— 这一步就是故障现场 ──────────────────────────────────
    console.log("\n再登录一次（故障现场）…");
    const loginRequestsBefore = requests.filter((r) => r.url.includes("/api/auth/login")).length;
    await cdp.eval(setInput("#login-identifier", IDENTIFIER));
    await cdp.eval(setInput("#login-password", PASSWORD));
    await cdp.eval(clickButton("登录"));

    let buttonState = null;
    let stuck = true;
    for (let i = 0; i < 40; i += 1) {
      await sleep(500);
      buttonState = await cdp.eval(LOGIN_BUTTON_TEXT);
      const here = await cdp.eval("location.pathname");
      if (here !== "/login") { stuck = false; break; }
      const err = await cdp.eval(`(() => {
        const box = document.querySelector(".error-box");
        return box ? box.innerText.replace(/\\s+/g, " ").trim() : "";
      })()`);
      if (err) { stuck = false; buttonState = { ...(buttonState ?? {}), error: err }; break; }
    }

    const loginRequestsAfter = requests.filter((r) => r.url.includes("/api/auth/login")).length;
    check(
      "退出登录之后还能再登进去（不卡在「登录中…」）",
      !stuck,
      `按钮 = ${JSON.stringify(buttonState)}；/api/auth/login 请求数 ${loginRequestsBefore} → ${loginRequestsAfter}`,
    );

    const finalPath = await cdp.eval("location.pathname");
    console.log(`\n最终路径 = ${finalPath}`);
    if (stuck) {
      console.log("\n页面可见文本（截断 600 字）：");
      console.log((await cdp.eval("document.body.innerText")).slice(0, 600));
      if (consoleLines.length > 0) {
        console.log("\n控制台输出（后 20 条）：");
        for (const line of consoleLines.slice(-20)) console.log(`  ${line}`);
      }
    }

    socket.close();
  } finally {
    child.kill();
  }

  const failed = results.filter((item) => !item.ok);
  console.log(`\n${failed.length === 0 ? "全部通过" : `${failed.length} 条失败`}（共 ${results.length} 条）`);
  process.exitCode = failed.length === 0 ? 0 : 1;
}

void main().catch((exc) => {
  console.error(`取证脚本失败：${exc.stack ?? exc}`);
  process.exitCode = 1;
});
