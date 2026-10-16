/**
 * 订单页与退款台共用的**展示辅助**（变更 003）。
 *
 * ## 为什么它们不在页面文件里
 *
 * ⚠️ `next build` 对 `app/**\/page.tsx` 做**入口检查**：除白名单
 * （`default` / `metadata` / `dynamic` …）之外的任何导出都会让构建失败
 * （变更 002 实测：`Property 'PRESETS' is incompatible with index signature`）。
 * 所以「可复用的小函数」一律放 `src/lib/` —— 这也是 `DEMO_PRESETS` 当初被搬到
 * `lib/auth.ts` 的同一条理由。
 *
 * ## 为什么这些看起来「小到不值得抽」的东西值得单独一处
 *
 * 它们在**两个页面**（`/orders` 客户侧、`/internal/refunds` 员工侧）都要用，
 * 而两处各写一份的症状是「同一笔退款在两个页面上显示成不同颜色 / 不同措辞」——
 * 那类不一致不会报错，只会让人以为系统在骗他。
 */

import { ApiError } from "./api.ts";
import { apiBase } from "./config.ts";
import { probeFailureHint } from "./diagnostics.ts";
import { SessionExpiredError } from "./session.ts";

/** 备注/审批意见的长度上限（与后端 `String(255)` 列一致）。 */
export const MAX_NOTE_CHARS = 255;

/**
 * 退款状态 → 徽章样式（**唯一一处**映射：订单页与审批台共用同一种配色语义）。
 *
 * `globals.css` 里 `badge.ok` / `badge.warn` / `badge.danger` 是三档语义：
 * 「已完成」/「等待中」/「被拒绝」。`requested` 落在「等待中」——
 * 它是**唯一**需要人去处理的状态，用 warn 是为了让它在一屏里跳出来。
 */
export function refundBadgeClass(status: string): string {
  if (status === "approved" || status === "refunded") return "ok";
  if (status === "rejected") return "danger";
  return "warn";
}

/**
 * 小计 = 数量 × 单价（两位小数）。
 *
 * ⚠️ 非法/空白输入返回 `—`（**不是 `0.00`**）。
 * 这条被用例抓出来过：`Number("")` 是 `0` 且是有限数，于是空单价会显示成 `0.00` ——
 * 那是在**声明一件错的事实**（「这件商品不要钱」），而真相是「这个值没拿到」。
 * `—` 才是诚实的显示。同一个理由适用于 `NaN` / 非数字文本。
 */
export function lineTotal(quantity: number, unitPrice: string): string {
  const text = String(unitPrice ?? "").trim();
  if (!text) return "—";
  const unit = Number(text);
  if (!Number.isFinite(unit) || !Number.isFinite(quantity)) return "—";
  return (unit * quantity).toFixed(2);
}

/**
 * ISO8601 → 本地可读时间。
 *
 * ⚠️ 解析不出来时**原样返回**（而不是显示 `Invalid Date`）：后端给的是 ISO 字符串，
 * 但万一将来变成别的格式，显示原文至少还能让人看出「这里有个值」——
 * 而 `Invalid Date` 会让人以为数据坏了。
 */
export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

/**
 * 造一个退款申请用的**幂等键**（客户手动申请用）。
 *
 * ## ⚠️ 调用时机是这个函数的全部要点
 *
 * 它必须在**打开表单时调用一次**，并把结果存在表单状态里，
 * 而不是在「点提交」时调用 —— 后者意味着「网络抖动后用户又点了一次」会落**两张单**，
 * 而两张 `requested` 各占一份可退额度（后端把 `requested` 计入
 * `CONSUMING_REFUND_STATUSES`），客户会发现可退额度莫名少了一半。
 *
 * 字符集与后端 `write_path.validate_idempotency_key` 一致：
 * `[A-Za-z0-9._:-]`、≤64 字符。所以前缀用 `-` 而不是 `:`（两者都合法，
 * 但 `-` 在 URL/日志里不会被误当成主体分隔符）。
 */
export function newRefundKey(): string {
  const stamp = Date.now().toString(36);
  const random = Math.random().toString(36).slice(2, 10);
  return `web-refund-${stamp}-${random}`;
}

/**
 * 把接口错误翻成一句**能照做**的话。
 *
 * ⚠️ 四档必须分开（它们的修法完全不同）：
 *   * **401** → 重新登录；
 *   * **404** → 后端按越权处置了（「不是你的」与「不存在」故意不可区分）；
 *   * **400** → 业务拒绝（额度不足 / 状态不可退），后端的 message 已说清原因，原样展示；
 *   * **无状态码** → fetch 自己抛的 `TypeError`（后端没起 / 跨源被拦），
 *     走 `probeFailureHint` 的分诊话术 —— 把一句 `TypeError: Failed to fetch`
 *     甩给用户等于什么都没说。
 */
export function describeApiFailure(exc: unknown): string {
  if (exc instanceof ApiError) {
    if (exc.isUnauthorized) {
      return `登录态已失效（账号可能被停用或口令被改）：请重新登录。request_id=${exc.requestId}`;
    }
    if (exc.status === 404) {
      return (
        "被后端按 404 拒绝了：要么这条记录不属于你，要么当前身份没有这个权限" +
        `（后端的越权口径与「不存在」刻意不可区分）。request_id=${exc.requestId}`
      );
    }
    if (exc.status === 400) {
      // 业务拒绝：后端的 message 已经把原因说清了，原样展示（不要改写成「操作失败」）
      return `${exc.message}（code=${exc.code}${exc.requestId ? ` · request_id=${exc.requestId}` : ""}）`;
    }
    if (exc.status === 409) {
      // 变更 003：审批的状态机冲突 —— **别人已经处理过了**，用户该做的是刷新
      return `${exc.message}（code=${exc.code} · 请刷新列表看最新状态${exc.requestId ? ` · request_id=${exc.requestId}` : ""}）`;
    }
    if (exc.status === 422) {
      return `请求被后端拒绝：${exc.message}（HTTP 422${exc.requestId ? ` · request_id=${exc.requestId}` : ""}）`;
    }
    return `${exc.message}（HTTP ${exc.status}${exc.requestId ? ` · request_id=${exc.requestId}` : ""}）`;
  }
  if (exc instanceof SessionExpiredError) return `${exc.message}（请重新登录）`;
  // 没有状态码 = 请求的结果没被读到（服务没起 / 跨源被拦 / 网络）
  return probeFailureHint(exc, { apiBase: apiBase() });
}
