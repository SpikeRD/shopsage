/**
 * 侧边栏权限分流与外壳结构的用例（变更 002 / T11）。
 *
 * ## 两类断言，各管一件事
 *
 * | 类型 | 抓什么 | 抓不到什么 |
 * |---|---|---|
 * | **纯函数**（`visibleEntriesFor` / `entryForPath`） | 分流规则本身 | 「页面有没有用它」 |
 * | **源码级结构**（读 `layout.tsx` / `SideNav.tsx` 文本） | 「接线漏了」——例如登录页居然引用了侧边栏 | 运行期行为 |
 *
 * 源码级那一类的价值与 `hook-wiring.test.ts` 同源：**纯逻辑单测抓不到接线漏项**，
 * 类型检查也抓不到（谁引用了谁不是类型的一部分）。
 *
 * ⚠️ 局限（写清楚，免得误读）：结构断言检查的是**源码文本的形状**。它能挡住
 * 「有人把 SideNav 挂回登录页」「有人把权限过滤删了」，挡不住运行期的渲染错误 ——
 * 真正的渲染覆盖需要浏览器（本仓没有 Playwright，见变更单 002 §6 T13/T18 的手工替代）。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { entryForPath, entryPath, visibleEntriesFor } from "../src/lib/auth.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.resolve(HERE, "../src/app");

/**
 * 去掉注释再断言。
 *
 * ⚠️ **这一步是必须的**（第一版没做，用例直接误报）：本仓的模块注释非常详细，
 * 而且**刻意**会引用被删掉的旧东西（例如 `admin/page.tsx` 的注释里写着
 * 「原先是 `className="table"`」、登录页注释里写着「开发模式已删除」）。
 * 直接对整个文件做 `includes` 断言，会被这些**说明文字**骗过 ——
 * 于是用例连「代码其实已经改对了」都判不过。
 *
 * 只处理块注释与整行行注释：字符串里的 `http://` 不会被误伤。
 */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

const read = (relative: string) => readFileSync(path.join(APP, relative), "utf8");

const APP_LAYOUT = read("(app)/layout.tsx");
const AUTH_LAYOUT = read("(auth)/layout.tsx");
const LOGIN_PAGE = stripComments(read("(auth)/login/page.tsx"));
const SIDE_NAV = stripComments(readFileSync(path.resolve(HERE, "../src/components/SideNav.tsx"), "utf8"));
const GLOBALS_CSS = stripComments(read("globals.css"));
const ROOT_LAYOUT = stripComments(read("layout.tsx"));
const ADMIN_PAGE = stripComments(read("(app)/admin/page.tsx"));
const HEALTH_BANNER = stripComments(readFileSync(path.resolve(HERE, "../src/components/HealthBanner.tsx"), "utf8"));

// ── 分流规则（纯函数）────────────────────────────────────────────────────────

test("★ 客户只看到对客入口（不该看见对内查询与管理台）", () => {
  assert.deepEqual(visibleEntriesFor(["customer"]), ["customer"]);
});

test("★ 能读全站订单的员工看到对客 + 对内，但看不到管理台", () => {
  assert.deepEqual(visibleEntriesFor(["internal_employee"]), ["customer", "internal"]);
  assert.deepEqual(visibleEntriesFor(["manager"]), ["customer", "internal"]);
  assert.deepEqual(visibleEntriesFor(["finance_auditor"]), ["customer", "internal"]);
});

test("管理员三个入口都看到（管理员也带 read:all_orders，顺序不能反）", () => {
  assert.deepEqual(visibleEntriesFor(["admin"]), ["customer", "internal", "admin"]);
});

test("未登录（空角色列表）只看到对客入口 —— 它是兜底入口，也是登录页之外的唯一去处", () => {
  assert.deepEqual(visibleEntriesFor([]), ["customer"]);
});

test("★ 三把钥匙互不覆盖：任一入口的角色够格就显示（与旧 TopNav 的 roles.some 同规则）", () => {
  // 现场：客户身份开着对客页，同时用管理员身份开着管理台。
  // 若只看 customer 那把钥匙，侧边栏会显示「去登录」——那是对既有行为的回归。
  assert.deepEqual(visibleEntriesFor(["customer", "admin"]), ["customer", "internal", "admin"]);
  assert.deepEqual(visibleEntriesFor(["customer", "internal_employee"]), ["customer", "internal"]);
});

test("认不出来的角色按「零权限」处理（默认拒绝，不默认放行）", () => {
  assert.deepEqual(visibleEntriesFor(["some_unknown_role"]), ["customer"]);
});

test("entryForPath 是 entryPath 的反函数（三个入口都要对得上）", () => {
  for (const entry of ["customer", "internal", "admin"] as const) {
    assert.equal(entryForPath(entryPath(entry)), entry);
  }
});

test("entryForPath：容忍末尾斜杠与查询串，认不出来按对客处理", () => {
  assert.equal(entryForPath("/admin/"), "admin");
  assert.equal(entryForPath("/internal?x=1"), "internal");
  assert.equal(entryForPath("/"), "customer");
  assert.equal(entryForPath("/citation"), "customer");
});

// ── 外壳结构（源码级）──────────────────────────────────────────────────────

test("★ 登录页不渲染侧边栏（需求：登录界面和主界面分开）", () => {
  assert.ok(
    !/SideNav/.test(AUTH_LAYOUT),
    "(auth)/layout.tsx 引用了 SideNav：登录页会长出侧边栏，与「登录界面和主界面分开」冲突",
  );
  assert.match(AUTH_LAYOUT, /auth-shell/, "(auth)/layout.tsx 应当用登录专用外壳类");
});

test("★ 主界面有侧边栏（需求：页面入口放在左侧边栏）", () => {
  assert.match(APP_LAYOUT, /<SideNav\s*\/>/, "(app)/layout.tsx 没有挂 SideNav：入口就没有落点了");
  assert.match(APP_LAYOUT, /app-shell/, "(app)/layout.tsx 应当用两栏外壳类");
});

test("★ 探活横幅挂在**两个** layout 上（需求：每次打开或刷新就执行一次探活）", () => {
  assert.match(APP_LAYOUT, /<HealthBanner\s*\/>/, "(app)/layout.tsx 没挂探活横幅：主界面断连时不会有全局提示");
  assert.match(AUTH_LAYOUT, /<HealthBanner\s*\/>/, "(auth)/layout.tsx 没挂探活横幅：登录页打开时不会探活");
});

test("★ 侧边栏的入口过滤用的是 lib/auth 的纯函数（不是自己写一套 if）", () => {
  assert.match(SIDE_NAV, /visibleEntriesFor/, "侧边栏没有用 visibleEntriesFor：分流规则会分叉成两份");
});

test("★ 个人信息优先来自 GET /api/auth/me，且回退时标注了「本地回退值」", () => {
  assert.match(SIDE_NAV, /getMe\(/, "侧边栏没有调 /api/auth/me：权限状态就不是服务端权威值");
  assert.match(
    SIDE_NAV,
    /本地回退值/,
    "回退路径没有在界面上标注：一个「本地说是 admin、后端已停用」的账号会看到满权限界面然后每个请求都 401",
  );
});

test("★ 侧边栏读**多个**入口的登录态（三把钥匙互不覆盖，只读一个会回归）", () => {
  // 判据是「源码里对三个入口都调了 readActiveSession」——用一个循环写的话，
  // 至少要有 entries 列表与 readActiveSession 两样东西。
  assert.match(SIDE_NAV, /readActiveSession/);
  assert.match(SIDE_NAV, /"admin"/, "没有读 admin 那把钥匙：管理员登录后侧边栏会显示「去登录」");
});

test("★ 探活不在模块顶层调用（SSR/构建期不能 fetch，否则 next build 直接失败）", () => {
  assert.match(HEALTH_BANNER, /useEffect\(/, "HealthBanner 没有用 useEffect：探活会跑到服务端渲染里");
  assert.match(HEALTH_BANNER, /probeHealth\(/, "HealthBanner 没有用 lib/health 的 probeHealth");
});

// ── 主题与表格（变更 002 的另两条需求）─────────────────────────────────────

test("★ 浅色是唯一主题：globals.css 不再有 data-theme 分支", () => {
  assert.ok(
    !/data-theme/.test(GLOBALS_CSS),
    "globals.css 里还有 data-theme：主题切换是死代码（没有任何界面在写它），且默认深色不满足「浅色护眼」",
  );
});

test("★ 根布局不再有主题脚本（它读的那个 key 没人写）", () => {
  assert.ok(!/shopsage-theme/.test(ROOT_LAYOUT), "layout.tsx 里还有主题脚本：那是读一个没人写的 key");
  assert.ok(!/TopNav/.test(ROOT_LAYOUT), "layout.tsx 还在引用 TopNav（该组件已删除）");
});

test("★ 浅色变量确实落地了（--bg 是浅色，不是原来的 #0b1020）", () => {
  assert.match(GLOBALS_CSS, /--bg:\s*#f6f7f4/, "globals.css 的 --bg 不是浅色米白");
  assert.ok(!/#0b1020/.test(GLOBALS_CSS), "globals.css 里还留着原来的深色底 #0b1020");
});

test("★ 管理台的表格样式存在（.table 这个类名从来没有对应规则 —— 这就是那个 bug）", () => {
  // 变更前 `admin/page.tsx` 写的是 className="table"，而 CSS 里只有 table.data，
  // 于是那张表完全没有样式（长文本自由换行 = 「格式混在一起」）。
  assert.match(GLOBALS_CSS, /table\.data\s*\{/, "globals.css 里没有 table.data 规则");
  assert.match(GLOBALS_CSS, /\.cell-ellipsis\s*\{/, "没有长文本省略号规则（长文本仍会把表格挤乱）");
  assert.match(GLOBALS_CSS, /\.cell-actions\s*\{/, "没有操作列不换行规则");
});

test("★ 管理台用的是 table.data 而不是无样式的 table", () => {
  assert.match(ADMIN_PAGE, /className="data"/, "管理台没有用 table.data");
  assert.ok(
    !/className="table"/.test(ADMIN_PAGE),
    '管理台还在用 className="table"：这个类名在 globals.css 里没有对应规则，表格会没有样式',
  );
  assert.match(ADMIN_PAGE, /cell-ellipsis/, "长文本列没有用省略号");
  assert.match(ADMIN_PAGE, /title=\{account\.identifier\}/, "省略号列没有 title：截断了却不给全文，等于把信息藏起来");
});

test("★ 登录页只剩登录（不再有开发模式 / 演示身份 / 登录态卡片）", () => {
  for (const gone of ["开发模式", "演示身份", "DEMO_PRESETS", "showDevMode", "当前登录态"]) {
    assert.ok(!LOGIN_PAGE.includes(gone), `登录页里还有「${gone}」：需求是「只管登录账户」`);
  }
  // 但它必须**保留**登录本身与防枚举口径
  assert.match(LOGIN_PAGE, /postLogin\(/, "登录页没有调登录接口");
  assert.match(LOGIN_PAGE, /writeSessionPair\(/, "登录页没有把服务端返回的 entry 写进存储");
});
