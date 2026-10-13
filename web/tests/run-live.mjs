/**
 * 真链路用例的**一键启动器**（`node tests/run-live.mjs`）。
 *
 * 它把「跑一次真链路」需要的四件事串起来，因为少任何一件这组用例都会以
 * 「skip 一大片 / 连不上」收场，而那种红跟代码毫无关系（最费时间的假红）：
 *
 * ```
 *   ① 现签三个令牌（客户 / 内部 / 已过期）   ← uv run python -m scripts.dev_token --json
 *   ② 起前端（next dev，等端口就绪）          ← 默认只在前端没起时起
 *   ③ 把 ①② 的结果塞进环境变量
 *   ④ node --test --test-isolation=none tests/live/*.test.ts
 * ```
 *
 * ⚠️ **前提是后端已经在跑**（`uv run uvicorn app.main:app --port 8000`）。
 * 这里刻意不去起后端：它依赖 Postgres/Redis（在 Linux VM 上），
 * 由人来起、看得见日志，比脚本在后台悄悄拉起来好排查得多（§10.2 第 1 条）。
 *
 * 用法：
 *
 * ```bash
 * node tests/run-live.mjs                    # 全流程（会自动起前端）
 * SHOPSAGE_SKIP_WEB=1 node tests/run-live.mjs # 前端已经起了：只签令牌 + 跑用例
 * SHOPSAGE_KEEP_WEB=1 node tests/run-live.mjs # 跑完不关前端（留着手工看页面）
 * ```
 */

import { spawn } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(HERE, "..");
const REPO_ROOT = path.resolve(WEB_DIR, "..");

const API_BASE = process.env.SHOPSAGE_API_BASE ?? "http://127.0.0.1:8000";
const WEB_BASE = process.env.SHOPSAGE_WEB_BASE ?? "http://127.0.0.1:3000";
const WEB_PORT = Number(new URL(WEB_BASE).port || 3000);

function log(message) {
  process.stdout.write(`\n▶ ${message}\n`);
}

/** 跑一条命令并收集输出。⚠️ 沙箱下子进程的 stdout 管道可能不可用，所以要先 try。 */
function capture(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd ?? REPO_ROOT,
      shell: process.platform === "win32",
      env: { ...process.env, ...(options.env ?? {}) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`${command} ${args.join(" ")} 退出码 ${code}\n${stderr || stdout}`));
    });
  });
}

/** 等一个 HTTP 端点可用（最多等 `timeoutMs`）。 */
async function waitForHttp(url, timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { redirect: "manual" });
      if (response.status < 500) return true;
      lastError = `HTTP ${response.status}`;
    } catch (exc) {
      lastError = exc instanceof Error ? exc.message : String(exc);
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error(`等 ${url} 就绪超时：${lastError}`);
}

async function mintToken(args) {
  const { stdout } = await capture("uv", ["run", "python", "-m", "scripts.dev_token", "--json", ...args], {
    env: { UV_CACHE_DIR: path.join(REPO_ROOT, ".uv-cache"), PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
  });
  const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "";
  return JSON.parse(line).token;
}

async function main() {
  log(`① 检查后端（${API_BASE}/health）——它必须已经在跑`);
  await waitForHttp(`${API_BASE}/health`, 15_000);

  log("② 现签三个令牌（客户 / 内部员工 / 已过期）");
  const [customer, internal, expired] = await Promise.all([
    mintToken(["--role", "customer", "--user-id", "2"]),
    mintToken(["--role", "internal_employee", "--user-id", "7"]),
    mintToken(["--role", "customer", "--user-id", "2", "--ttl", "-5"]),
  ]);
  log(`   客户令牌 …${customer.slice(-12)} / 内部令牌 …${internal.slice(-12)} / 过期令牌 …${expired.slice(-12)}`);

  let web = null;
  const skipWeb = process.env.SHOPSAGE_SKIP_WEB === "1";
  if (!skipWeb) {
    log(`③ 起前端（next dev，端口 ${WEB_PORT}）`);
    web = spawn("npx", ["next", "dev", "--port", String(WEB_PORT)], {
      cwd: WEB_DIR,
      shell: process.platform === "win32",
      // ⚠️ stdio: 'inherit' 是**必须**的：沙箱下管道不可用时，
      // 一个 `stdio: 'pipe'` 的子进程会以 EPERM 直接失败（见 runbook §10.1 坑 1）
      stdio: "inherit",
      env: { ...process.env, NEXT_PUBLIC_API_BASE: API_BASE },
    });
    await waitForHttp(WEB_BASE, 120_000);
    log("   前端已就绪");
  } else {
    log("③ 跳过起前端（SHOPSAGE_SKIP_WEB=1），只探一下它是否可用");
    await waitForHttp(WEB_BASE, 10_000);
  }

  log("④ 跑真链路用例");
  const test = spawn(
    process.execPath,
    ["--test", "--test-isolation=none", "tests/live/live.test.ts"],
    {
      cwd: WEB_DIR,
      stdio: "inherit",
      env: {
        ...process.env,
        SHOPSAGE_API_BASE: API_BASE,
        SHOPSAGE_TOKEN_CUSTOMER: customer,
        SHOPSAGE_TOKEN_INTERNAL: internal,
        SHOPSAGE_TOKEN_EXPIRED: expired,
        SHOPSAGE_WEB_BASE: WEB_BASE,
        NEXT_PUBLIC_API_BASE: API_BASE,
      },
    },
  );
  const code = await new Promise((resolve) => test.on("close", resolve));

  if (web && process.env.SHOPSAGE_KEEP_WEB !== "1") {
    log("⑤ 关掉前端");
    web.kill();
  } else if (web) {
    log(`⑤ 前端留着：${WEB_BASE}（q 或 Ctrl+C 结束本脚本时它会一起退出）`);
  }
  process.exitCode = code === 0 ? 0 : 1;
}

main().catch((exc) => {
  process.stderr.write(`\n✖ 启动器失败：${exc instanceof Error ? exc.message : String(exc)}\n`);
  process.exitCode = 1;
});
