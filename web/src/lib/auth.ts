/**
 * 前端侧的登录态、角色分流与「展示什么」的规则。
 *
 * ## 一条纪律（`16.web.md` §4 第 4 条）
 *
 * **前端不做业务判断**。这里所有的判断只回答一个问题：
 * 「这个界面要不要显示给这个人看」——**不是**「他能不能拿到这条数据」。
 * 后者在后端（对客走行级过滤、对内走 `app/security/rbac.py` 的权限表），
 * 前端拦不住的请求后端一定会拦。
 *
 * 那为什么还要在前端做一次？因为**两个入口要分流**（`16.web.md` §4 第 1 条）：
 * 客户不该看见「内部数据查询」这个入口。这不是安全边界，是**界面边界** ——
 * 把它当安全边界用（比如「前端不显示内部按钮所以后端可以不查权限」）是错的，
 * 而这正是 runbook §10.1 坑 8 记的那个认知。
 *
 * ## 令牌里的角色**不验签**
 *
 * `decodeJwtPayload()` 只做 base64 解码，**不校验签名**（浏览器侧没有密钥，
 * 也不该有）。所以它只能用来**决定渲染哪个界面**，
 * 绝不能用来决定「这个人有没有权限读这行数据」—— 那需要服务端验签后的结果。
 * 之所以这里敢用它渲染：界面显示错了的后果是「看到自己点不动的按钮」，
 * 而不是「看到别人的订单」。
 */

import { SESSION_STORAGE_PREFIX, TOKEN_STORAGE_PREFIX } from "./config.ts";
import { SESSION_ID_PATTERN } from "./contracts.ts";

/** 本仓后端的四个演示角色（与 `app/security/rbac.py` 的 `ROLE_PERMISSIONS` 同源）。 */
export type Role = "customer" | "customer_service" | "internal_employee" | "manager" | "finance_auditor" | "admin";

/** 登录后要看的界面（`/` 对客、`/internal` 对内、`/admin` 管理台）。 */
export type Entry = "customer" | "internal" | "admin";

/** 权限点（与后端 `app/security/rbac.py` 的常量逐字相同）。 */
export const PERM_READ_OWN = "read:own";
export const PERM_READ_ALL_ORDERS = "read:all_orders";
export const PERM_READ_FINANCE = "read:finance";
export const PERM_WRITE_REFUND = "write:refund";
export const PERM_WRITE_TICKET = "write:ticket";
export const PERM_ADMIN = "admin";

/**
 * 角色 → 权限点。
 *
 * ⚠️ **这是后端表的副本，不是真相**。副本的价值只有一个：
 * 能在界面上一眼看出「这个角色能不能进对内入口」，
 * 而不用为一次渲染去问后端。**任何真正的授权判断都不许用它**。
 */
export const ROLE_PERMISSIONS: Record<string, readonly string[]> = {
  customer: [PERM_READ_OWN, PERM_WRITE_TICKET],
  customer_service: [PERM_READ_OWN, PERM_WRITE_TICKET, PERM_WRITE_REFUND],
  internal_employee: [PERM_READ_OWN, PERM_READ_ALL_ORDERS, PERM_WRITE_TICKET, PERM_WRITE_REFUND],
  manager: [PERM_READ_OWN, PERM_READ_ALL_ORDERS, PERM_WRITE_TICKET, PERM_WRITE_REFUND],
  finance_auditor: [PERM_READ_OWN, PERM_READ_ALL_ORDERS, PERM_READ_FINANCE, PERM_WRITE_TICKET],
  admin: [
    PERM_READ_OWN,
    PERM_READ_ALL_ORDERS,
    PERM_READ_FINANCE,
    PERM_WRITE_REFUND,
    PERM_WRITE_TICKET,
    PERM_ADMIN,
  ],
};

export function hasPermission(role: string, permission: string): boolean {
  return (ROLE_PERMISSIONS[role] ?? []).includes(permission);
}

/** 能否进对内入口（判据是「能不能读全站订单」，与后端 `Actor.internal` 同一判据）。 */
export function canUseInternal(role: string): boolean {
  return hasPermission(role, PERM_READ_ALL_ORDERS);
}

/** 能否进管理台（判据是「有没有 `admin` 权限点」—— 与后端 `accounts.entry_for_role` 同一判据）。 */
export function canUseAdmin(role: string): boolean {
  return hasPermission(role, PERM_ADMIN);
}

/**
 * 默认入口。⚠️ **优先级与后端 `accounts.entry_for_role` 必须一致**：
 * 管理员 → 管理台；能读全站订单 → 对内；否则 → 对客。
 *
 * 顺序不能反：管理员也带 `read:all_orders`，先判对内就永远进不了管理台。
 * 但**真正的入口由服务端在登录响应的 `entry` 字段里给**（`lib/session.ts` 用它），
 * 这个函数只在「服务端没给 entry」（例如手粘令牌）时兜底。
 */
export function defaultEntry(role: string): Entry {
  if (canUseAdmin(role)) return "admin";
  return canUseInternal(role) ? "internal" : "customer";
}

/** 入口 → 页面路径（**唯一一处**映射，导航与跳转都用它）。 */
export function entryPath(entry: Entry): string {
  if (entry === "admin") return "/admin";
  if (entry === "internal") return "/internal";
  return "/";
}

/** 入口的中文名（标语与导航共用，避免各处写三遍字符串）。 */
export function entryLabel(entry: Entry): string {
  if (entry === "admin") return "管理台";
  if (entry === "internal") return "对内查询";
  return "对客问答";
}

// ── 令牌解码（**不验签**，见模块注释）────────────────────────────────────────

export interface TokenClaims {
  /**
   * 主体标识串（变更 001 起是 `c2` 客户 / `e1` 员工，**不再是裸 user_id**）。
   *
   * ⚠️ 前端**不要**把它当数字用：它带类别前缀，而「前缀 + 数字」正是
   * `thread_id` 的形状。要显示「我是谁」直接用原串。
   */
  sub: string;
  role: string;
  /** 过期时间（秒级 UNIX 时间戳）；缺失 = 0，视作「已过期」 */
  exp: number;
  /** 令牌类型：`access` / `refresh`（见 `decodeJwtPayload` 的注释：声明的键名是 `typ`） */
  tokenType: string;
  /** 主体类别（`customer` / `employee`）；旧令牌缺失 = 空串 */
  subjectType: string;
  /** 账号主键；旧令牌缺失 = 0 */
  accountId: number;
}

/**
 * 把 `JWT` 的 payload 段解出来；形状不对时返回 null（**不抛**）。
 *
 * ## ⚠️ 类型声明的键名是 `typ`，不是 `type`（实测踩到，见 runbook §10.1 坑 13）
 *
 * 后端 `app/security/auth.py::_create_token` 写的是 `{"typ": token_type}`，
 * 而 `parse_token` 也读 `payload.get("typ")`。前端第一版读的是 **`type`** —— 于是：
 *
 * ```text
 * 所有**真的**访问令牌都被前端判成「不是访问令牌」→ 登录页一律拒绝：
 *   这是刷新令牌（type=refresh），后端会当 401 处理 —— 用 --kind access 重新签发一个。
 * ```
 *
 * 也就是说：**报错信息本身是错的**（令牌是 access，声明键名前缀对不上），
 * 而后端完全能用这个令牌 —— 这类「前后端对同一个契约读不同键名」的 bug
 * 最容易被误判成「令牌签错了」，因为报错看起来非常具体。
 *
 * 处置：**两个键名都认**（`typ` 优先），并且把认不出来的情况与「确实是刷新令牌」
 * 分开报（见 `tokenTypeOf` / 登录页的提示语）—— 「未知」与「refresh」的修法完全不同。
 */
export function decodeJwtPayload(token: string): TokenClaims | null {
  const parts = String(token ?? "").split(".");
  if (parts.length !== 3) return null;
  try {
    // 浏览器与 Node 都有 atob（base64url 要先换回 base64 并补齐 padding）
    const normalized = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
    const json = typeof atob === "function" ? atob(padded) : Buffer.from(padded, "base64").toString("binary");
    const payload = JSON.parse(json) as Record<string, unknown>;
    const role = typeof payload.role === "string" ? payload.role : "customer";
    const sub = payload.sub === undefined || payload.sub === null ? "" : String(payload.sub);
    const exp = typeof payload.exp === "number" ? payload.exp : 0;
    // `typ` 是本仓后端的声明键名；`type` 留给「别处签发、按 OAuth 惯例命名」的令牌
    const typeValue = payload.typ ?? payload.type;
    const tokenType = typeof typeValue === "string" ? typeValue : "";
    // 变更 001 新增的两个声明（旧令牌没有 → 空串/0，`isAccountToken` 据此判「旧格式」）
    const subjectType = typeof payload.stype === "string" ? payload.stype : "";
    const accountId = typeof payload.aid === "number" ? payload.aid : 0;
    if (!sub) return null;
    return { sub, role, exp, tokenType, subjectType, accountId };
  } catch {
    return null;
  }
}

/**
 * 这是不是**变更 001 之后**签发的令牌（带 `aid`/`stype`/`ver`）。
 *
 * 为什么需要这个判断：旧令牌（`sub` 是裸数字、没有这些声明）在后端
 * `deps.principal_with_account` 里**一定被拒**（那是「升级后旧令牌失效」的落点）。
 * 前端若能提前识别，就能给出「这是旧格式，请重新登录」而不是笼统的 401 ——
 * 两者的修法完全不同。
 */
export function isAccountToken(token: string): boolean {
  const claims = decodeJwtPayload(token);
  return Boolean(claims && claims.accountId > 0 && claims.subjectType);
}

/** 令牌类型的三态：`access` / `refresh` / **`unknown`**（没有任何类型声明）。 */
export type TokenKind = "access" | "refresh" | "unknown";

/**
 * 归一化令牌类型。
 *
 * 为什么要有 `unknown` 这一态：**「声明缺失」与「声明 refresh」的修法不同** ——
 * 前者是「这个令牌不是本系统签的（或走了别处的签发工具）」，后者是「签的时候选了刷新令牌」。
 * 把它们混成一句话（第一版就是混着的）会把人引到错误的修法上（§10.1 坑 13）。
 */
export function tokenKind(token: string): TokenKind {
  const value = (decodeJwtPayload(token)?.tokenType ?? "").trim().toLowerCase();
  if (value === "access") return "access";
  if (value === "refresh") return "refresh";
  return "unknown";
}

/** 令牌是否已过期（或根本解不出来）。`skewSeconds` 给一点余量，避免「正好卡在边界」。 */
export function isExpired(token: string, nowSeconds: number = Date.now() / 1000, skewSeconds = 0): boolean {
  const claims = decodeJwtPayload(token);
  if (!claims) return true;
  return claims.exp <= nowSeconds + skewSeconds;
}

/**
 * 令牌类型是不是访问令牌（**刷新令牌不能当访问令牌用**，后端会 401）。
 *
 * ⚠️ 这里只放行 `access`：声明缺失（`unknown`）的令牌一律**不接受** ——
 * 后端 `parse_token` 对缺失类型的令牌也是拒绝（`收到 (缺失)`），前端放行只会让
 * 用户「粘进去看着成功、一发消息就 401」。两端的判据必须一致。
 */
export function isAccessToken(token: string): boolean {
  return tokenKind(token) === "access";
}

// ── 存储（localStorage）──────────────────────────────────────────────────────

/**
 * 存储后端的最小接口。
 *
 * 为什么要注入而不是直接写 `window.localStorage`：
 * ① 本模块要能在**Node 测试**里跑（没有 window）；② 用假实现能测「过期令牌被清掉」。
 */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** 内存实现：供测试与 SSR（服务端渲染时没有 localStorage）使用。 */
export function memoryStorage(): StorageLike {
  const data = new Map<string, string>();
  return {
    getItem: (key) => (data.has(key) ? (data.get(key) as string) : null),
    setItem: (key, value) => {
      data.set(key, value);
    },
    removeItem: (key) => {
      data.delete(key);
    },
  };
}

/** 浏览器里取 localStorage；SSR 或隐私模式下取不到时退化成内存（**不抛**）。 */
export function browserStorage(): StorageLike {
  try {
    if (typeof window !== "undefined" && window.localStorage) {
      // 隐私模式下 localStorage 存在但 setItem 会抛 —— 所以这里试写一次
      const probe = "__shopsage_probe__";
      window.localStorage.setItem(probe, "1");
      window.localStorage.removeItem(probe);
      return window.localStorage;
    }
  } catch {
    // 落到内存实现：这一轮能用，刷新后要重新登录（可接受的降级）
  }
  return memoryStorage();
}

export interface Session {
  token: string;
  userId: string;
  role: string;
  entry: Entry;
}

export function tokenKey(entry: Entry): string {
  return `${TOKEN_STORAGE_PREFIX}.${entry}`;
}

export function sessionKey(entry: Entry): string {
  return `${SESSION_STORAGE_PREFIX}.${entry}`;
}

/**
 * 读取某一入口的登录态。
 *
 * 三条口径：
 * 1. **过期即视为未登录**（并顺手清掉），而不是「先放进去、等后端 401」——
 *    后者的表现是「点一下弹一次登录框」，比直接跳登录页难用得多；
 * 2. 令牌解不出来（被截断/手工改坏）同样视为未登录；
 * 3. 刷新令牌一律拒绝（`isAccessToken`）—— 它在后端一定 401。
 */
export function readSession(entry: Entry, storage: StorageLike): Session | null {
  const token = storage.getItem(tokenKey(entry));
  if (!token) return null;
  const claims = decodeJwtPayload(token);
  if (!claims || !isAccessToken(token) || isExpired(token)) {
    // 顺手清掉：留着它只会让「为什么每次都要重新登录」变成一次排障
    storage.removeItem(tokenKey(entry));
    return null;
  }
  return { token, userId: claims.sub, role: claims.role, entry };
}

export function writeSession(session: Session, storage: StorageLike): void {
  storage.setItem(tokenKey(session.entry), session.token);
}

export function clearSession(entry: Entry, storage: StorageLike): void {
  storage.removeItem(tokenKey(entry));
  storage.removeItem(sessionKey(entry));
}

/**
 * 会话 id：**同一入口复用同一个值**（多轮对话才接得上）。
 *
 * 后端的 `thread_id` 是从「身份 + session_id」派生的（`app/api/deps.py`），
 * 所以前端只需要记住自己的 `session_id`；`thread_id` 一律用响应里回传的那个
 * （`16.web.md` §4 第 4 条：前端不自己拼内部标识）。
 */
export function readSessionId(entry: Entry, storage: StorageLike): string | null {
  const value = storage.getItem(sessionKey(entry));
  return value && SESSION_ID_PATTERN.test(value) ? value : null;
}

export function writeSessionId(entry: Entry, sessionId: string, storage: StorageLike): void {
  if (SESSION_ID_PATTERN.test(sessionId)) storage.setItem(sessionKey(entry), sessionId);
}

/** 生成一个客户端会话 id（`s` + 时间戳 + 随机段，字符集受后端约束，所以只用小写字母数字）。 */
export function newSessionId(now: () => number = Date.now): string {
  const stamp = now().toString(36);
  const random = Math.random().toString(36).slice(2, 8);
  return `s${stamp}${random}`.slice(0, 64);
}

/** 演示身份的卡片数据（登录页展示用）。 */
export interface DemoPreset {
  entry: Entry;
  role: string;
  /** 登录标识（用户名或手机号）—— 变更 001 起登录要它，而不是 user_id */
  account: string;
  label: string;
  hint: string;
}

/**
 * 演示身份（与 `scripts/seed.py::DEMO_ACCOUNTS` **同源**）。
 *
 * ⚠️ 放在 `lib/` 而不是页面文件里，有两个理由：
 * ① **页面文件不许有额外导出** —— `next build` 会对 `app/**\/page.tsx` 做入口检查，
 *    除白名单（`default` / `metadata` / `dynamic` …）之外的导出都会让构建失败
 *    （实测：`Property 'PRESETS' is incompatible with index signature`）；
 * ② 这张表要能被测试引用（`web/tests/auth.test.ts` 会核对每个 role 都在权限表里）。
 *
 * ⚠️ 变更 001 把 `userId` 换成了 `account`：登录用的是**登录标识**，
 * 而主体 id 由服务端从账号行里取（前端不该猜「哪个 user_id 对应哪个账号」）。
 */
export const DEMO_PRESETS: DemoPreset[] = [
  {
    entry: "customer",
    role: "customer",
    account: "13800000002",
    label: "客户",
    hint: "只能查自己的订单（行级过滤）",
  },
  {
    entry: "internal",
    role: "internal_employee",
    account: "employee",
    label: "普通员工",
    hint: "可读全站订单；财务级数据不可见（RBAC）",
  },
  {
    entry: "admin",
    role: "admin",
    account: "admin",
    label: "管理员",
    hint: "全量权限 + 管理台（开账号 / 停用 / 改角色 / 重置口令）",
  },
];
