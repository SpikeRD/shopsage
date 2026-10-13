/**
 * 前端要用的**部署参数**（唯一一处读取 `NEXT_PUBLIC_*` 的地方）。
 *
 * 为什么要收在一个模块里：`process.env.NEXT_PUBLIC_X` **在构建时被字面替换**
 * （不是运行期查环境变量）—— 它散落在各个组件里时，你无法一眼看出
 * 「这个前端到底连哪个后端」。第 1 步 `1.config.md` 的口径是「配置取值只有一处」，
 * 前端也照同一条纪律。
 */

/** 后端地址。默认与本仓开发拓扑一致（API 跑在 Windows 开发机的 8000）。 */
const DEFAULT_API_BASE = "http://127.0.0.1:8000";

/**
 * 读后端地址并规范化。
 *
 * ⚠️ 去掉**末尾斜杠**：`http://host:8000/` + `/api/chat` 会拼出 `//api/chat`，
 * 而多数反代会把它当另一个路径（或者直接 404）——与 `app/api/config.py::api_prefix`
 * 里堵的是同一类坑（那里是末尾斜杠导致 `/api//chat`）。
 */
export function apiBase(raw: string | undefined = process.env.NEXT_PUBLIC_API_BASE): string {
  const value = (raw ?? "").trim();
  if (!value) return DEFAULT_API_BASE;
  return value.replace(/\/+$/, "");
}

/**
 * **实际使用**的后端地址：把「指到页面自己」那种配置自动纠正掉（并在页面上说明）。
 *
 * ## 为什么要自动纠正，而不是只报错（实测踩到，见 runbook §10.1 坑 14）
 *
 * `NEXT_PUBLIC_API_BASE` 是**构建期**变量。一旦它被设成前端自己的地址
 * （例如 `http://localhost:3000`，或者用 `NEXT_PUBLIC_API_BASE=$PWD` 之类的写法），
 * 症状是「HTTP 侧两样都像好的、WebSocket 一定失败」：
 *
 * * `WebSocket /api/chat/stream` → **Next 的 dev server 不代理 upgrade**，必然失败；
 * * `/api/chat`（对内页）→ 打到 Next 自己 → 404/500，看起来像「后端坏了」；
 * * `/api/kb`（Next 自己的路由）→ 同源，**永远正常** —— 这一条是「半通」错觉的来源。
 *
 * 这个配置对一个**浏览器**来说没有第二种解释（页面自己的端口上没有后端），
 * 所以「纠正 + 明确告知」比「报错让人自己改」更有用：
 * 前者马上能用，后者要求先理解 `NEXT_PUBLIC_*` 是构建期变量这件事。
 * ⚠️ 关键在**明确告知**：这是「有声音的降级」，不是静默兜底（页面上会出一条提示）。
 */
export function resolvedApiBase(
  raw: string | undefined = process.env.NEXT_PUBLIC_API_BASE,
  context?: PageContext | string,
): string {
  const base = apiBase(raw);
  return isSelfReferentialBase(base, context) ? DEFAULT_API_BASE : base;
}

/**
 * 这一条配置**没法工作**吗？——`NEXT_PUBLIC_API_BASE` 指到了页面自己（同源）。

/** HTTP 接口的完整地址（`/api/chat` → `http://host:8000/api/chat`）。 */
export function apiUrl(path: string): string {
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${resolvedApiBase()}${suffix}`;
}

/**
 * WebSocket 流式地址。
 *
 * `http:` → `ws:`、`https:` → `wss:`（**不是**无脑替换字符串）：
 * 页面在 HTTPS 下连 `ws://` 会被浏览器以混合内容拦掉，
 * 而症状（连接直接失败、控制台一行 mixed content）很容易被误判成「后端没起」。
 *
 * `base` 可注入：真链路用例（`tests/live`）用它把地址指到环境变量里的后端，
 * 这样测试与页面走的是**同一段推导逻辑**，不会出现「测试里对、页面上错」。
 */
export function streamUrl(path = "/api/chat/stream", base: string = resolvedApiBase()): string {
  const http = `${base.replace(/\/+$/, "")}${path.startsWith("/") ? path : `/${path}`}`;
  if (http.startsWith("https://")) return `wss://${http.slice("https://".length)}`;
  if (http.startsWith("http://")) return `ws://${http.slice("http://".length)}`;
  return http;
}

/** 会话在 localStorage 里的键前缀（同一角色多标签页共用一条会话）。 */
export const SESSION_STORAGE_PREFIX = "shopsage.session";

/** 令牌在 localStorage 里的键前缀。 */
export const TOKEN_STORAGE_PREFIX = "shopsage.token";

/** 页面所在位置（可注入，便于离线测试；缺省从 `window` 读）。 */
export interface PageContext {
  origin?: string;
  protocol?: string;
}

function pageContext(context?: PageContext): Required<PageContext> {
  if (context?.origin && context?.protocol) return { origin: context.origin, protocol: context.protocol };
  if (typeof window !== "undefined" && window.location) {
    return {
      origin: context?.origin ?? window.location.origin,
      protocol: context?.protocol ?? window.location.protocol,
    };
  }
  return { origin: context?.origin ?? "", protocol: context?.protocol ?? "" };
}

/**
 * 这一条配置**没法工作**吗？——`NEXT_PUBLIC_API_BASE` 指到了页面自己（同源）。
 *
 * ## 为什么要有这个判定（实测踩到，见 runbook §10.1 坑 14）
 *
 * 现象很有欺骗性：**对客页能问答（HTTP 通）但流式一定失败**。当时三条对照证据：
 *
 * ```text
 * ① ws://127.0.0.1:8000/api/chat/stream → open，子协议=shopsage.v1（54ms）   ← 后端正常
 * ② ws://localhost:8000/…               → open（27ms）
 * ③ ws://127.0.0.1:3000/…（前端自己）    → error 事件（8s 超时）              ← 就是页面上的症状
 * ```
 *
 * 成因：HTTP 侧有两个出口 —— **`/api/kb` 是 Next 自己的路由**（同源，永远通），
 * `/api/chat` 走 `NEXT_PUBLIC_API_BASE`；而 **WebSocket 只能走 `NEXT_PUBLIC_API_BASE`**。
 * 于是「base 指到前端自己」时，HTTP 按钮看着正常（点的是同源 `/api/kb`），
 * 而 WS 一定是死的 —— **Next 的 dev server 不代理 WebSocket upgrade**（`next.config.ts` 里也没有 rewrite）。
 *
 * 这条判定的价值：把一个「看起来像后端坏了」的症状，在**发请求之前**就说清楚，
 * 并且给出唯一正确的修法（重启前端并把 base 指到后端）。
 */
export function isSelfReferentialBase(
  base: string = apiBase(),
  context?: PageContext | string,
): boolean {
  const page =
    typeof context === "string" ? { origin: context, protocol: "" } : pageContext(context);
  if (!page.origin) return false;
  try {
    return new URL(base).origin === new URL(page.origin).origin;
  } catch {
    return false;
  }
}

/**
 * 配置诊断：返回**人能照着改**的一句话数组（空数组 = 没发现问题）。
 *
 * 与 `diagnostics.ts::probeFailureHint` 的分工：那个是「**失败之后**怎么查」，
 * 这个是「**发请求之前**先看一眼」。
 */
export function configWarnings(
  base: string = apiBase(),
  context?: PageContext | string,
): string[] {
  const page =
    typeof context === "string" ? { origin: context, protocol: "" } : pageContext(context);
  const out: string[] = [];
  if (isSelfReferentialBase(base, page)) {
    out.push(
      `NEXT_PUBLIC_API_BASE 指到了页面自己（${base}）——**已自动改用 ${DEFAULT_API_BASE}**：` +
        "页面自己的端口上没有后端（Next 的 dev server 既不代理 WebSocket upgrade，" +
        "也不转发 /api/chat），不纠正的话症状会是「HTTP 看着正常（同源的 /api/kb 由 Next 自己答）、" +
        "WebSocket 一定失败」。" +
        `想用别的后端就在**启动前端时**设它（构建期变量，改了要重启）：` +
        `$env:NEXT_PUBLIC_API_BASE="http://127.0.0.1:8000"; npm run dev`,
    );
  }
  if (page.protocol === "https:" && base.startsWith("http://")) {
    out.push(
      "页面在 HTTPS 下，而后端是 http:// —— 浏览器会以混合内容拦掉它（连接会直接失败）。" +
        "生产形态应当同源部署（反代）或给后端配 TLS。",
    );
  }
  return out;
}
