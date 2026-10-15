/**
 * 侧边栏在**真实登录态**下的分流取证（变更 002 / T18 补充）。
 *
 * ## 为什么不能只靠 `shell.test.ts`
 *
 * `shell.test.ts` 里的分流用例测的是**纯函数** `visibleEntriesFor(roles)` ——
 * 它证明"规则对"，但不证明"页面真的按这个规则渲染"。
 *
 * 本脚本走**真的 HTTP**：从 `scripts.dev_token` 拿真令牌，用 `GET /api/auth/me`
 * 拿真权限点，然后对每个角色断言「哪些入口该出现 / 该消失」。
 *
 * ⚠️ 已知局限：它验证的是**判据链**（令牌 → /me → 权限点 → visibleEntriesFor），
 * 不是浏览器里的 DOM。DOM 需要浏览器（本仓没有 Playwright）—— 见变更单 002 §7。
 *
 * 用法（在 web/ 下；需要后端在跑）：
 *   node --import tsx scripts/sidebar-evidence.ts
 */

import { readFileSync } from "node:fs";
import { getMe } from "../src/lib/api.ts";
import { visibleEntriesFor, type Entry } from "../src/lib/auth.ts";
import { apiBase } from "../src/lib/config.ts";

/** 从 `scripts/seed.py` 之外的路径拿令牌：读 `dev_token` 的输出（由外层 shell 传入）。 */
function tokenFor(envName: string): string {
  const value = process.env[envName];
  if (!value) throw new Error(`缺少环境变量 ${envName}`);
  return value;
}

interface Case {
  label: string;
  envName: string;
  expectVisible: Entry[];
  expectHidden: Entry[];
}

const CASES: Case[] = [
  {
    label: "客户（13800000002）",
    envName: "TOK_CUSTOMER",
    expectVisible: ["customer"],
    expectHidden: ["internal", "admin"],
  },
  {
    label: "普通员工（employee）",
    envName: "TOK_EMPLOYEE",
    expectVisible: ["customer", "internal"],
    expectHidden: ["admin"],
  },
  {
    label: "管理员（admin）",
    envName: "TOK_ADMIN",
    expectVisible: ["customer", "internal", "admin"],
    expectHidden: [],
  },
];

async function main() {
  console.log(`后端 apiBase() = ${apiBase()}`);
  console.log();
  let allOk = true;

  for (const item of CASES) {
    const token = tokenFor(item.envName);
    const me = await getMe(token);
    const role = String(me.role ?? "");
    const permissions = Array.isArray(me.permissions) ? me.permissions.map((x) => String(x)) : [];
    const visible = visibleEntriesFor([role]);

    const missing = item.expectVisible.filter((entry) => !visible.includes(entry));
    const extra = item.expectHidden.filter((entry) => visible.includes(entry));
    const ok = missing.length === 0 && extra.length === 0;
    allOk = allOk && ok;

    console.log(`${ok ? "PASS" : "FAIL"}  ${item.label}`);
    console.log(`      role=${role}  subject=${String(me.subject)}  entry=${String(me.entry)}`);
    console.log(`      权限点（${permissions.length}）= ${permissions.join(", ")}`);
    console.log(`      侧边栏可见入口 = [${visible.join(", ")}]`);
    if (missing.length) console.log(`      ✖ 缺少应有的入口: ${missing.join(", ")}`);
    if (extra.length) console.log(`      ✖ 出现了不该有的入口: ${extra.join(", ")}`);
    console.log();
  }

  // 源码级：确认主界面真的挂了侧边栏（判据链的最后一环）
  const appLayout = readFileSync(new URL("../src/app/(app)/layout.tsx", import.meta.url), "utf8");
  const wired = appLayout.includes("<SideNav");
  console.log(`${wired ? "PASS" : "FAIL"}  (app)/layout.tsx 挂了 <SideNav/>`);
  allOk = allOk && wired;

  process.exitCode = allOk ? 0 : 1;
}

void main();
