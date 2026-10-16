/**
 * 管理台「改角色」的**真浏览器**取证脚本（issue 006）—— 一次性手工脚本，不是测试套件。
 *
 * ## 为什么必须用真浏览器
 *
 * issue 006 的故障形态是「**前端提示成功、后端没变化**」：
 *
 * ```text
 * 页面提示：已把 probe-role-1 的角色改为 manager（在途令牌已失效）。   ← 说改了
 * 后端实际：role = customer                                        ← 没改
 * ```
 *
 * 根因是 **React 受控 `<select>` + 异步读 `event.target.value`**：
 *
 * ```text
 * change 同步 = manager     ← 只有这一瞬间是对的
 * microtask   = customer    ← React 用旧的 account.role 把 DOM 回滚了
 * t+50ms      = customer
 * ```
 *
 * 而 `onChange` 里的 thunk 是**异步执行**的：`run()` 先 `setNotice(null)` 调度重渲染，
 * 等它真正构造请求体时 `event.target.value` 已经是旧值 → 请求体发的是原角色
 * （等于什么都没改）→ 后端照旧 200 → 前端提示却说改了。
 *
 * ⚠️ **本仓别的手段都够不到它**：
 * * 离线用例不渲染 React、不发请求；
 * * 服务端用例直接构造 `PATCH {"role":"manager"}`，**后端是好的**（实测）——
 *   坏的只有「前端从 DOM 读了什么值」这一步；
 * * `hook-wiring.test.ts` 的源码正则曾经查不出「值被异步读了」这类**时序**问题
 *   （所以本轮除了脚本还补了一条源码级断言，见 `web/tests/hook-wiring.test.ts`）。
 *
 * ## 它断言什么
 *
 * 1. 改角色之后，**请求体里的 `role` 等于用户选的那个值**（不是列表里的旧值）；
 * 2. 后端读回来的 role **确实变了**；
 * 3. 列表那一行的显示值、以及页面提示语，三者**互相一致**。
 *
 * ⚠️ **本脚本会真的改一个账号的角色**，跑完改回原角色。
 * 它只该对着**开发/演示库**跑。
 *
 * 用法（在 web/ 下；需要后端 + 前端都在跑）：
 *   node scripts/admin-role-evidence.mjs
 *
 * 环境变量：
 *   SHOPSAGE_WEB_BASE   前端地址（默认 http://127.0.0.1:3000）
 *   SHOPSAGE_API_BASE   后端地址（默认 http://127.0.0.1:8000）
 *   CHROME_BIN          Chrome/Edge 可执行文件（默认自动探测）
 *   PROBE_IDENTIFIER    用哪个账号做实验（默认 "probe-role-1"，不存在就建一个）
 */

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";

const WEB = process.env.SHOPSAGE_WEB_BASE ?? "http://127.0.0.1:3000";
const API = process.env.SHOPSAGE_API_BASE ?? "http://127.0.0.1:8000";
const PORT = Number(process.env.CDP_PORT ?? 9352);
const IDENTIFIER = process.env.PROBE_IDENTIFIER ?? "probe-role-1";
const SUBJECT_ID = Number(process.env.PROBE_SUBJECT ?? 8);
const PROBE_PASSWORD = "probe-role-passphrase";

/** 实验用的两端角色：从 FROM 改到 TO，再改回去。 */
const FROM_ROLE = process.env.PROBE_FROM ?? "customer";
const TO_ROLE = process.env.PROBE_TO ?? "manager";

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
function setSelect(selector, value) {
  return `(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return "找不到 " + ${JSON.stringify(selector)};
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, ${JSON.stringify(value)});
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

/** 从后端读这个账号的**权威**角色值。 */
async function backendRole(token) {
  const payload = await fetch(`${API}/api/admin/accounts?limit=500`, {
    headers: { Authorization: `Bearer ${token}` },
  }).then((response) => response.json());
  return payload.accounts.find((item) => item.identifier === IDENTIFIER) ?? null;
}

async function main() {
  const browser = findBrowser();
  if (!browser) {
    console.log("SKIP 找不到 Chrome/Edge：设 CHROME_BIN 指向浏览器可执行文件再跑");
    process.exitCode = 0;
    return;
  }

  // ── 准备：确保探针账号存在，并归位到 FROM_ROLE ─────────────────────────────
  const login = await fetch(`${API}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identifier: "admin", password: "admin-demo-pass" }),
  }).then((response) => response.json());
  const token = login.access_token;
  const authJson = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };

  let probe = (await backendRole(token)) ?? null;
  if (!probe) {
    await fetch(`${API}/api/admin/accounts`, {
      method: "POST",
      headers: authJson,
      body: JSON.stringify({
        identifier: IDENTIFIER,
        password: PROBE_PASSWORD,
        role: FROM_ROLE,
        subject_type: "customer",
        subject_id: SUBJECT_ID,
        status: "active",
      }),
    });
    probe = await backendRole(token);
  }
  await fetch(`${API}/api/admin/accounts/${probe.account_id}`, {
    method: "PATCH",
    headers: authJson,
    body: JSON.stringify({ role: FROM_ROLE }),
  });
  console.log(`探针账号 ${IDENTIFIER}（account_id=${probe.account_id}）已归位为 ${FROM_ROLE}\n`);

  const profile = mkdtempSync(path.join(tmpdir(), "shopsage-role-evidence-"));
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

    /** 抓 PATCH 的请求体 —— 「请求体里到底发了哪个角色」是本脚本的核心判据。 */
    const patches = [];
    const requestInfo = new Map();
    cdp.on((message) => {
      if (message.method === "Network.requestWillBeSent") {
        const { requestId, request } = message.params;
        requestInfo.set(requestId, { url: request.url, method: request.method, postData: request.postData });
        if (request.method === "PATCH") {
          patches.push({ url: request.url, postData: request.postData });
        }
      }
      if (message.method === "Network.responseReceived") {
        const info = requestInfo.get(message.params.requestId);
        if (info?.method === "PATCH") {
          const entry = patches.filter((item) => item.url === info.url && item.status === undefined).pop();
          if (entry) entry.status = message.params.response.status;
        }
      }
    });

    console.log(`浏览器 = ${browser}`);
    console.log(`前端   = ${WEB}`);
    console.log(`后端   = ${API}`);
    console.log(`改角色 = ${FROM_ROLE} -> ${TO_ROLE}\n`);

    // ── 登录 ────────────────────────────────────────────────────────────────
    await cdp.send("Page.navigate", { url: `${WEB}/login` });
    await sleep(3000);
    await cdp.eval(setSelect("#login-identifier", "admin"));
    await cdp.eval(setSelect("#login-password", "admin-demo-pass"));
    await cdp.eval(clickButton("登录"));
    await sleep(4000);
    const landed = await cdp.eval("location.pathname");
    check("管理员登录后落在 /admin", landed === "/admin", `实际 ${landed}`);
    if (landed !== "/admin") throw new Error("没进管理台，后续断言无意义");
    await sleep(1500);

    const findRow = `(() => {
      const rows = [...document.querySelectorAll("table.data tbody tr")];
      for (let i = 0; i < rows.length; i += 1) {
        const cells = [...rows[i].querySelectorAll("td")].map((td) => td.innerText.trim());
        if (cells[0] === ${JSON.stringify(IDENTIFIER)}) {
          const select = rows[i].querySelector("select");
          return { rowIndex: i, role: cells[1], selectValue: select ? select.value : null };
        }
      }
      return null;
    })()`;

    const before = await cdp.eval(findRow);
    check(`列表里找到 ${IDENTIFIER}，且当前角色是 ${FROM_ROLE}`, before?.role === FROM_ROLE, JSON.stringify(before));

    // ── 改角色（模拟用户在下拉框里选）────────────────────────────────────────
    const selector = `select[aria-label="改 ${IDENTIFIER} 的角色"]`;
    await cdp.eval(setSelect(selector, TO_ROLE));
    await sleep(4000);

    const lastPatch = patches.at(-1) ?? null;
    let sentRole = null;
    try {
      sentRole = lastPatch?.postData ? JSON.parse(lastPatch.postData).role : null;
    } catch {
      sentRole = null;
    }

    check(
      `PATCH 请求体里的 role 等于用户选的值（${TO_ROLE}）`,
      sentRole === TO_ROLE,
      `实际请求体 = ${lastPatch?.postData ?? "(没有捕到 PATCH)"}` +
        (sentRole === FROM_ROLE ? `\n      ← issue 006 的原始故障：发的是**旧值** ${FROM_ROLE}（等于什么都没改）` : ""),
    );

    const authoritative = await backendRole(token);
    check(
      `后端里 ${IDENTIFIER} 的角色**真的**变成了 ${TO_ROLE}`,
      authoritative?.role === TO_ROLE,
      `实际 role=${authoritative?.role} entry=${authoritative?.entry}`,
    );

    const after = await cdp.eval(findRow);
    check(
      `列表里显示的角色也变成了 ${TO_ROLE}（界面与后端一致）`,
      after?.role === TO_ROLE && after?.selectValue === TO_ROLE,
      JSON.stringify(after),
    );

    const notice = await cdp.eval(`(() => {
      const err = document.querySelector(".error-box");
      if (err) return "ERROR: " + err.innerText.replace(/\\s+/g, " ").trim();
      const ok = [...document.querySelectorAll(".card")].find((c) => c.innerText.includes("已把"));
      return ok ? ok.innerText.replace(/\\s+/g, " ").trim().slice(0, 160) : "(无提示)";
    })()`);
    check(
      "页面提示语里说的角色与真正生效的角色一致",
      notice.includes(TO_ROLE) && !notice.startsWith("ERROR"),
      notice,
    );

    socket.close();
  } finally {
    child.kill();
    // 改回原角色，恢复现场
    try {
      const current = await backendRole(token);
      if (current) {
        const back = await fetch(`${API}/api/admin/accounts/${current.account_id}`, {
          method: "PATCH",
          headers: authJson,
          body: JSON.stringify({ role: FROM_ROLE }),
        });
        console.log(`\n清理：把 ${IDENTIFIER} 的角色改回 ${FROM_ROLE}（HTTP ${back.status}）`);
      }
    } catch (exc) {
      console.log(`\n清理失败（请手工检查 ${IDENTIFIER} 的角色）：${exc}`);
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
