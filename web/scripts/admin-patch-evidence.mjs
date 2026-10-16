/**
 * 管理台「停用 / 改角色」的**真浏览器**取证脚本（issue 005）—— 一次性手工脚本，不是测试套件。
 *
 * ## 为什么必须用真浏览器
 *
 * issue 005 的故障是 **CORS 预检**：`PATCH /api/admin/accounts/{id}` 是**非简单方法**，
 * 浏览器一定先发 `OPTIONS` 预检。预检被 `400 Disallowed CORS method` 拒掉之后，
 * **那条真正的 PATCH 根本不会发出** —— 于是：
 *
 * ```text
 * 页面  ：请求没有到达后端（没有状态码）… TypeError: Failed to fetch
 * 控制台：Response to preflight request doesn't pass access control check
 * 后端  ：一条请求都没有（预检在中间件层就被答掉了）
 * ```
 *
 * ⚠️ **本仓现有手段全都够不到它**：
 * * `web/tests/*.test.ts`（离线）不渲染 React、不发请求；
 * * `web/tests/live/*.ts` 是 **Node 直接 fetch** —— Node **不受 CORS 约束**，
 *   所以「真链路全绿」完全不能证明浏览器能用（`tests/api/test_cors.py` 的 docstring 自己写着这条）；
 * * 服务端用例能验「预检带对了头」，但验不了「页面真的点得动」。
 *
 * 所以这里用 CDP 驱动无头 Chrome 真的点「停用」按钮，并**记录预检的状态码**。
 *
 * ## 它断言什么
 *
 * 1. `OPTIONS .../accounts/{id}`（ACRM=PATCH）**必须是 200**（不是 400）；
 * 2. 点「停用」之后**状态真的翻转**（active → disabled），且列表里能看见；
 * 3. 全程没有 `Failed to fetch` / CORS 报错。
 *
 * ⚠️ **本脚本会真的改一个账号的状态**，跑完改回 `active`。
 * 它只该对着**开发/演示库**跑；默认拿列表里第一个可停用的非管理员账号做实验。
 *
 * 用法（在 web/ 下；需要后端 + 前端都在跑）：
 *   node scripts/admin-patch-evidence.mjs
 *
 * 环境变量：
 *   SHOPSAGE_WEB_BASE   前端地址（默认 http://127.0.0.1:3000）
 *   SHOPSAGE_API_BASE   后端地址（默认 http://127.0.0.1:8000）
 *   CHROME_BIN          Chrome/Edge 可执行文件（默认自动探测）
 *   PROBE_IDENTIFIER    指定拿哪个账号做实验（默认自动挑一个非管理员）
 */

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";

const WEB = process.env.SHOPSAGE_WEB_BASE ?? "http://127.0.0.1:3000";
const API = process.env.SHOPSAGE_API_BASE ?? "http://127.0.0.1:8000";
const PORT = Number(process.env.CDP_PORT ?? 9340);

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

/** 读某一行的状态（`rowIndex` 从 0 起）。 */
function readRowStatus(rowIndex) {
  return `(() => {
    const row = document.querySelectorAll("table.data tbody tr")[${rowIndex}];
    if (!row) return null;
    const cells = [...row.querySelectorAll("td")].map((td) => td.innerText.trim());
    return { identifier: cells[0], role: cells[1], subject: cells[2], entry: cells[3], status: cells[4] };
  })()`;
}

async function main() {
  const browser = findBrowser();
  if (!browser) {
    console.log("SKIP 找不到 Chrome/Edge：设 CHROME_BIN 指向浏览器可执行文件再跑");
    process.exitCode = 0;
    return;
  }

  const profile = mkdtempSync(path.join(tmpdir(), "shopsage-patch-probe-"));
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

  let restore = null;
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

    /** 记录所有 OPTIONS 预检的状态码，以及所有 PATCH 请求的最终状态。 */
    const preflights = [];
    const patches = [];
    const failures = [];
    const requestInfo = new Map();

    cdp.on((message) => {
      if (message.method === "Network.requestWillBeSent") {
        const { requestId, request } = message.params;
        requestInfo.set(requestId, { url: request.url, method: request.method });
        if (request.method === "OPTIONS" && request.url.includes("/api/admin/accounts")) {
          // 把预检的意图也记下来（ACRM = 它想用的真实方法），
          // 这样「这条 OPTIONS 是不是那条 PATCH 的预检」是可验证的，不靠猜
          const headers = message.params.request.headers ?? {};
          const acrm =
            headers["Access-Control-Request-Method"] ?? headers["access-control-request-method"] ?? "";
          preflights.push({ url: request.url, id: requestId, acrm: String(acrm).toUpperCase() });
        }
      }
      if (message.method === "Network.responseReceived") {
        const info = requestInfo.get(message.params.requestId);
        const status = message.params.response.status;
        if (info?.method === "OPTIONS" && info.url.includes("/api/admin/accounts")) {
          const entry = preflights.find((item) => item.id === message.params.requestId);
          if (entry) entry.status = status;
        }
        if (info?.method === "PATCH") {
          patches.push({ url: info.url, status });
        }
      }
      if (message.method === "Network.loadingFailed") {
        failures.push(message.params.errorText ?? "unknown");
      }
    });

    console.log(`浏览器 = ${browser}`);
    console.log(`前端   = ${WEB}`);
    console.log(`后端   = ${API}\n`);

    // ── 登录 ────────────────────────────────────────────────────────────────
    await cdp.send("Page.navigate", { url: `${WEB}/login` });
    await sleep(2500);
    await cdp.eval(setInput("#login-identifier", "admin"));
    await cdp.eval(setInput("#login-password", "admin-demo-pass"));
    await cdp.eval(clickButton("登录"));
    await sleep(3500);
    const landed = await cdp.eval("location.pathname");
    check("管理员登录后落在 /admin", landed === "/admin", `实际 ${landed}`);
    if (landed !== "/admin") throw new Error("没进管理台，后续断言无意义");

    await sleep(1500);

    // ── 判据 ①：预检本身必须 200（不是 400）────────────────────────────────
    // ⚠️ **不要用页面里的 `fetch(..., {method:"OPTIONS"})` 去"手工发预检"**：
    //    `Access-Control-Request-Method` / `Access-Control-Request-Headers` /
    //    `Origin` 都是**禁止由脚本设置的请求头**（forbidden header names），
    //    浏览器会把它们丢掉 —— 于是那条 OPTIONS 变成一条**普通 OPTIONS**，
    //    CORSMiddleware 不当作预检，请求落到路由上 → 405（本脚本第一版实测踩到）。
    //    真正的预检是浏览器在跨源 `PATCH` 之前**自动**发的，由下面的点击触发，
    //    再用 Network 域把它的状态码读出来。

    // ── 判据 ②：真的点「停用」，状态必须翻转 ────────────────────────────────
    // 找一个非管理员的 active 行（管理员不能停用自己 / 最后一个管理员）
    const prepared = await cdp.eval(`(() => {
      const wanted = ${JSON.stringify(process.env.PROBE_IDENTIFIER ?? "")};
      const rows = [...document.querySelectorAll("table.data tbody tr")];
      for (let i = 0; i < rows.length; i += 1) {
        const cells = [...rows[i].querySelectorAll("td")].map((td) => td.innerText.trim());
        if (wanted && cells[0] !== wanted) continue;
        if (cells[1].includes("admin")) continue;      // 跳过管理员行
        if (cells[4] !== "active") continue;           // 跳过已停用的
        const button = [...rows[i].querySelectorAll("button")].find((b) => b.textContent.trim() === "停用");
        if (!button) continue;
        button.click();
        return { rowIndex: i, identifier: cells[0], before: cells[4] };
      }
      return null;
    })()`);

    check("在列表里找到一个可停用的非管理员账号并点了「停用」", prepared !== null, JSON.stringify(prepared));

    if (prepared) {
      await sleep(3500);

      // 浏览器为这条 PATCH 自动发的那次预检（这才是 issue 005 真正会失败的一步）。
      // ⚠️ 判据要连在「**ACRM=PATCH** 的那条预检」上，不能只看「有没有 OPTIONS」——
      //    页面上还有 GET /api/admin/accounts（列表刷新），而 GET 是简单方法，
      //    它**不需要**预检；拿一条无关的 OPTIONS 来断言等于自欺（本脚本第一版偏差过）。
      const patchPreflight = preflights.find((item) => item.acrm === "PATCH") ?? null;
      const anyPreflight = patchPreflight ?? preflights.find((item) => item.status !== undefined) ?? preflights[0] ?? null;
      check(
        "浏览器为 PATCH 自动发出了预检（OPTIONS + ACRM=PATCH），且它是 200",
        patchPreflight?.status === 200,
        patchPreflight
          ? `OPTIONS ${patchPreflight.url} (ACRM=${patchPreflight.acrm}) -> ${patchPreflight.status}` +
            (patchPreflight.status === 400 ? "  ← issue 005 的原始故障：Disallowed CORS method" : "")
          : `没捕到 ACRM=PATCH 的预检；捕到的 OPTIONS = ${JSON.stringify(anyPreflight)}`,
      );

      const after = await cdp.eval(readRowStatus(prepared.rowIndex));
      check(
        `点「停用」后该行状态由 active 变为 disabled（${prepared.identifier}）`,
        after?.status === "disabled",
        `停用前=${prepared.before} 停用后=${after?.status ?? "(读不到该行)"}`,
      );
      check(
        "浏览器的 PATCH 请求真的发出去了（而不是被预检拦下）",
        patches.length > 0,
        `实际捕到 ${patches.length} 条 PATCH：${JSON.stringify(patches)}`,
      );
      check(
        "PATCH 的响应是 200（不是 CORS 失败 / 无状态码）",
        patches.some((item) => item.status === 200),
        JSON.stringify(patches),
      );

      const notice = await cdp.eval(`(() => {
        const err = document.querySelector(".error-box");
        return err ? err.innerText.replace(/\\s+/g, " ").trim() : "(无错误框)";
      })()`);
      check(
        "页面上没有出现「请求没有到达后端 / Failed to fetch」",
        !/Failed to fetch|请求没有到达后端/.test(notice),
        `通知区：${notice}`,
      );

      restore = { rowIndex: prepared.rowIndex, identifier: prepared.identifier };
    }

    socket.close();
  } finally {
    child.kill();
    // 把实验对象改回启用（管理接口没有"删除"，改回 active 即恢复原状）
    if (restore) {
      try {
        const login = await fetch(`${API}/api/auth/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ identifier: "admin", password: "admin-demo-pass" }),
        });
        const token = (await login.json()).access_token;
        const list = await fetch(`${API}/api/admin/accounts?limit=500`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        const found = (await list.json()).accounts.find((a) => a.identifier === restore.identifier);
        if (found) {
          const back = await fetch(`${API}/api/admin/accounts/${found.account_id}`, {
            method: "PATCH",
            headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
            body: JSON.stringify({ status: "active" }),
          });
          console.log(`\n清理：把 ${restore.identifier} 改回 active（HTTP ${back.status}）`);
        }
      } catch (exc) {
        console.log(`\n清理失败（请手工检查 ${restore.identifier} 的状态）：${exc}`);
      }
    }
  }

  const failed = results.filter((item) => !item.ok);
  console.log(`\n${failed.length === 0 ? "全部通过" : `${failed.length} 条失败`}（共 ${results.length} 条）`);
  process.exitCode = failed.length === 0 ? 0 : 1;
}

void main().catch((exc) => {
  console.error(`取证脚本失败：${exc.stack ?? exc}`);
  process.exitCode = 1;
});
