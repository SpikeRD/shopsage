/**
 * 「探活失败」时给用户看的那句话 —— **本步最容易被误判的一个症状**。
 *
 * ## 为什么值得单独一个模块 + 单独一组用例
 *
 * 浏览器里的 `fetch` 失败有三种完全不同的原因，但**症状是同一句话**：
 *
 * ```text
 * TypeError: Failed to fetch
 * ```
 *
 * | 真实原因 | 服务端表现 | 页面上看到 |
 * |---|---|---|
 * | 后端没起 / 地址写错 | 没有请求到达 | `Failed to fetch` |
 * | **跨源被浏览器拦掉（缺 CORS 头）** | 请求**到达了**，日志里是 200 | `Failed to fetch`（**没有任何状态码**） |
 * | 令牌无效 / 过期 | 401 + 错误信封 | `连不上后端…` ？不，是能读到的 401 |
 *
 * 第 16 步实测就踩在这上面：前端在 `127.0.0.1:3000`、API 在 `127.0.0.1:8000`
 * （端口不同 = 不同源），而后端当时**没有 CORS 中间件** ——
 * 于是页面报「连不上后端」，用户的第一反应是「令牌不对 / 后端没起」，
 * 而服务端日志里那几条 `GET /health 200 OK` 明明都在。
 *
 * **判据（写进提示语里）**：`fetch` 抛的是 `TypeError`（而不是拿到 4xx/5xx），
 * 说明**响应没被读到**；「令牌无效」会得到 401 —— 那是一个正常的 HTTP 响应。
 * 所以提示语的第一句必须是「先别怀疑令牌」。
 */

/** 后端地址与来源，用于把「谁连谁」写进提示语。 */
export interface ProbeContext {
  apiBase: string;
  webOrigin: string;
}

/** 取当前页面的来源（SSR 时没有 `window`，退化成一句说明）。 */
export function webOrigin(): string {
  if (typeof window !== "undefined" && window.location?.origin) return window.location.origin;
  return "（当前页面来源）";
}

/**
 * 把探活异常翻成一段**能照着修**的话。
 *
 * 三条分支的顺序有讲究：**先排除「响应读不到」**（CORS / 没起 / 地址错），
 * 再谈 HTTP 状态码。
 */
export function probeFailureHint(exc: unknown, context?: Partial<ProbeContext>): string {
  const base = context?.apiBase ?? "";
  const origin = context?.webOrigin ?? webOrigin();
  const detail = exc instanceof Error ? `${exc.name}: ${exc.message}` : String(exc);
  return [
    `探活失败（${detail}）。`,
    `这个症状**先别怀疑令牌**：令牌无效会得到 401 —— 那是一个能读到的 HTTP 响应，`,
    `而不是一句 ${"`"}Failed to fetch${"`"}。`,
    `按下面顺序查这三件事：`,
    `① 后端在不在：另开一个终端跑 ${"`"}uv run uvicorn app.main:app --host 0.0.0.0 --port 8000${"`"}；`,
    `② 地址对不对：当前前端来源 ${origin} → 后端 ${base || "（未配置）"}（跨源时后端必须放通这个来源）；`,
    `③ 是不是**跨源被浏览器拦掉**：打开 DevTools → Console，若有一行 CORS 提示，`,
    `就在后端 .env 里加 ${"`"}API_CORS_ORIGINS=${origin}${"`"} 并重启后端；`,
    `另外看 Network 面板：请求若是 200 却仍报错，那就是 CORS（服务端没问题）。`,
  ].join("");
}

/** 给「打开页面时发现没登录」用的一句话（区别于探活失败）。 */
export function needLoginHint(entry: "customer" | "internal"): string {
  const where = entry === "customer" ? "对客" : "对内";
  return `还没有「${where}」入口的登录态：去「登录 / 切角色」页粘贴一个令牌（见 docs/runbook/16-web-runbook.md §4.1）。`;
}
