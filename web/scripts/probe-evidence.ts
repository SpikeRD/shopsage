/**
 * 探活的**真实网络**取证脚本（变更 002 / T14 + T15）—— 一次性手工脚本，不是测试套件。
 *
 * 为什么单独写它、而不是只靠 `tests/health.test.ts`：那组用例是**离线**的
 * （注入假的 getHealth）。本脚本走**真的 fetch**：
 *
 * ```
 * ① NEXT_PUBLIC_API_BASE 指向一个空闲端口 → 期望 unreachable（后端没起）
 * ② NEXT_PUBLIC_API_BASE 指向真后端       → 期望 ok（后端在跑）
 * ```
 *
 * ⚠️ 已知局限：它验证的是**判据**（`probeHealth` 的结论），不是浏览器里的**渲染**。
 * 横幅的 DOM 需要浏览器才看得见（本仓没有 Playwright）—— 见变更单 002 §7 的说明。
 *
 * 用法（在 web/ 下）：
 *   node --import tsx scripts/probe-evidence.ts
 */

import { getHealth } from "../src/lib/api.ts";
import { apiBase } from "../src/lib/config.ts";
import { probeHealth } from "../src/lib/health.ts";

async function main() {
  console.log(`探活取证：当前 apiBase() = ${apiBase()}`);
  const result = await probeHealth(getHealth, { apiBase: apiBase() });
  console.log(`state    = ${result.judgement.state}`);
  console.log(`reported = ${JSON.stringify(result.judgement.reported)}`);
  console.log(`summary  = ${result.judgement.summary}`);
  if (result.payload) {
    console.log(`payload.status = ${result.payload.status}`);
    for (const item of result.payload.components) {
      console.log(`  component ${item.name} = ${item.status}`);
    }
  } else {
    console.log("payload = null（没读到 body）");
    console.log("detail  =");
    console.log(result.judgement.detail);
  }
  // 退出码：ok → 0；否则 1（方便在脚本里判定「这一次是正常还是异常」）
  process.exitCode = result.judgement.state === "ok" ? 0 : 1;
}

void main();
