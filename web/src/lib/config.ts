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

/** HTTP 接口的完整地址（`/api/chat` → `http://host:8000/api/chat`）。 */
export function apiUrl(path: string): string {
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${apiBase()}${suffix}`;
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
export function streamUrl(path = "/api/chat/stream", base: string = apiBase()): string {
  const http = `${base.replace(/\/+$/, "")}${path.startsWith("/") ? path : `/${path}`}`;
  if (http.startsWith("https://")) return `wss://${http.slice("https://".length)}`;
  if (http.startsWith("http://")) return `ws://${http.slice("http://".length)}`;
  return http;
}

/** 会话在 localStorage 里的键前缀（同一角色多标签页共用一条会话）。 */
export const SESSION_STORAGE_PREFIX = "shopsage.session";

/** 令牌在 localStorage 里的键前缀。 */
export const TOKEN_STORAGE_PREFIX = "shopsage.token";
