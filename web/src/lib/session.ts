/**
 * 登录态的**存储层**（变更 001 / T15）：一对令牌按入口分开存 + 快过期时自动续期。
 *
 * ## 为什么与 `lib/auth.ts` 分开

 * `auth.ts` 管的是「**怎么解读**一个令牌」（不验签的 payload 解码、角色分流），
 * 本模块管的是「**把它放在哪、什么时候换新的**」。
 * 两件事的变化原因完全不同（前者跟令牌格式走，后者跟登录流程走），
 * 放一个文件里会让「改了存储 key」与「改了角色表」看起来像同一类改动。
 *
 * ## 三把钥匙，互不覆盖（沿用第 16 步的设计）

 * `shopsage.token.customer` / `.internal` / `.admin` —— 所以可以「用客户身份开着对客页、
 * 同时用管理员身份开着管理台」，两个入口的行为**由各自令牌的 role 决定**，
 * 而不是由「最后登录的是谁」决定。变更 001 新增 `admin` 入口时沿用这条口径
 * （而不是让管理员复用 internal 的钥匙：那会让「切到管理台」把对内页踢下线）。
 *
 * ## 为什么需要刷新令牌（变更 001 新增）

 * 访问令牌只有 **30 分钟**。变更前的「粘贴令牌」模式下这不是问题（粘一次用完即弃），
 * 但真登录之后它意味着**每 30 分钟被踢回登录页**。
 * `ensureFreshToken()` 在**快过期时**（默认提前 60 秒）用刷新令牌换一对新的，
 * 于是用户无感；而刷新令牌本身 14 天有效、且能被服务端作废（停用/改密/改角色）。
 *
 * ## ⚠️ 一条纪律：**刷新失败必须清干净，不要留半条会话**

 * 刷新失败（401）意味着账号被停用/口令被改/令牌被作废。此时若只清访问令牌、
 * 留下刷新令牌，下一次 `ensureFreshToken` 又会去试一次 —— 用户看到的是
 * 「一直在转圈，然后莫名其妙跳登录页」。所以失败路径一律 `clearSession`。
 */

import {
  browserStorage,
  clearSession,
  decodeJwtPayload,
  defaultEntry,
  type Entry,
  isAccessToken,
  readSession,
  readSessionId,
  type Session,
  type StorageLike,
  tokenKey,
  visibleEntriesFor,
  writeSession,
} from "./auth.ts";
import { postRefresh, type TokenPayload } from "./api.ts";

/** 刷新令牌的存储 key 前缀（与访问令牌分开：它们的失效条件不同）。 */
export const REFRESH_STORAGE_PREFIX = "shopsage.refresh";

/** 提前多久续期（秒）。取 60s：足够覆盖一次请求的往返，又不至于频繁刷新。 */
export const REFRESH_LEEWAY_SECONDS = 60;

export interface StoredPair {
  accessToken: string;
  refreshToken: string;
}

export function refreshKey(entry: Entry): string {
  return `${REFRESH_STORAGE_PREFIX}.${entry}`;
}

export function readRefreshToken(entry: Entry, storage: StorageLike): string | null {
  const value = storage.getItem(refreshKey(entry));
  return value || null;
}

/**
 * 把一登录拿到的一对令牌写进存储。
 *
 * ## ⚠️ 变更 003：写进**该角色可进入的每一个入口**（不再只写一个）
 *
 * 变更 003 之前这里只写 `payload.entry` 指定的那一把钥匙，而每个页面只读自己那把
 * （`/` 读 `.customer`、`/internal` 读 `.internal`）。后果就是需求 1 报的现场：
 * **管理员登录后进 `/` 或 `/internal`，页面显示「需要先登录」** ——
 * 他明明有全部权限，只是那两个页面读的钥匙里没有令牌。
 *
 * 现在按 `visibleEntriesFor([role])` 把**同一对令牌**写进该角色能进的每个入口。
 *
 * ## ⚠️ 必须显式承认的破坏性后果（这是决策 1 的代价，不是实现瑕疵）
 *
 * 变更 002 建立的「三把钥匙互不覆盖」**不再成立**：
 *   * 同一账号登录会写它可进入的**多把**钥匙（不再是「一把钥匙一个身份」）；
 *   * 所以「用客户身份开着对客页、同时用管理员身份开着管理台」这件事，
 *     在**不同账号**之间变成了**后写覆盖先写**（同一个浏览器里只能同时持有一个账号）。
 *
 * 因此退出也必须按**账号**清（见 `clearSessionsForAccount`），否则会留下
 * 「用户以为退了、其实 `/` 与 `/internal` 还能用」的隐形会话。
 *
 * ⚠️ **入口仍以服务端返回的 `entry` 为准**（`payload.entry`）——那是「登录后落在哪一页」；
 * 而这里写几把钥匙是「这个身份能进哪几个界面」，两者是不同的决定。
 */
export function writeSessionPair(
  payload: TokenPayload,
  storage: StorageLike,
  options: { entry?: Entry } = {},
): Entry {
  const entry = options.entry ?? normalizeEntry(payload.entry) ?? defaultEntry(payload.role);
  // 该角色能进入的全部入口（对客永远是其中之一：它是兜底入口）
  //
  // ⚠️ 还要**并入服务端指定的 `entry`**：它回答的是「登录后落在哪一页」，
  // 而 `visibleEntriesFor` 回答的是「这个角色能进哪几个界面」—— 两者理论上一致，
  // 但**服务端是权威**（它是 `entry_for_role` 的唯一真相）。
  // 不并入的后果很具体：万一服务端说「进 internal」而这个角色不在可见集里，
  // 我们会把它重定向到一个**没有令牌**的页面 —— 用户看到「需要先登录」，
  // 而他刚刚登录成功（正是变更 003 要修的那类现场）。
  // 测试 `writeSessionPair 用**服务端返回的 entry**` 钉住了这条。
  const targets = new Set<Entry>([...visibleEntriesFor([payload.role]), entry]);
  for (const target of targets) {
    const session: Session = {
      token: payload.accessToken,
      userId: payload.subject,
      role: payload.role,
      entry: target,
    };
    writeSession(session, storage);
    storage.setItem(refreshKey(target), payload.refreshToken);
  }
  return entry;
}

/**
 * 退出：清掉**本账号写过的所有入口**钥匙（变更 003 / 决策 14）。
 *
 * ## 为什么不能只清当前路径那个入口
 *
 * 变更 003 起登录会把同一枚令牌写进多把钥匙（见 `writeSessionPair`）。
 * 若退出只清当前入口，用户在 `/admin` 点「退出」之后：
 *   * `/admin` 要重新登录（看起来退成功了）；
 *   * 但 `/` 与 `/internal` 的钥匙里**还留着同一枚有效令牌** —— 会话其实还活着。
 * 那是一类「用户以为已经登出、实际没有」的安全问题，比多清一把钥匙严重得多。
 *
 * ## 怎么判定「是本账号的」
 *
 * 用令牌的 `aid`（账号主键，变更 001 起每个令牌都带）。没有 `aid` 的旧格式令牌
 * 一律清掉 —— 它们在 `deps.principal_with_account` 里本来就会被拒，
 * 留着只会让用户看到一次莫名的 401。
 *
 * `accountId` 为 0（本地解不出 `aid`，例如手粘的令牌）时退化成「清全部入口」：
 * 宁可比必要的多清一点，也不要留下一条隐形会话。
 */
export function clearSessionsForAccount(
  accountId: number,
  storage: StorageLike,
  options: { entries?: readonly Entry[] } = {},
): Entry[] {
  const entries = options.entries ?? (["customer", "internal", "admin"] as const);
  const cleared: Entry[] = [];
  for (const entry of entries) {
    const token = storage.getItem(tokenKey(entry));
    if (!token) continue;
    const claims = decodeJwtPayload(token);
    // 解不出来 / 非账号令牌（`aid` 为 0）→ 一并清掉（保守一侧）
    const owner = claims?.accountId ?? 0;
    if (accountId <= 0 || owner <= 0 || owner === accountId) {
      clearSessionPair(entry, storage);
      cleared.push(entry);
    }
  }
  return cleared;
}

/** 当前登录账号的 id（优先取任一入口令牌的 `aid`；取不到返回 0）。 */
export function currentAccountId(
  storage: StorageLike,
  options: { entries?: readonly Entry[] } = {},
): number {
  const entries = options.entries ?? (["customer", "internal", "admin"] as const);
  for (const entry of entries) {
    const token = storage.getItem(tokenKey(entry));
    if (!token) continue;
    const claims = decodeJwtPayload(token);
    if (claims && claims.accountId > 0) return claims.accountId;
  }
  return 0;
}

/** 把服务端给的 `entry` 字符串收敛成 `Entry`；不认识就返回 null（由调用方兜底）。 */
export function normalizeEntry(value: string | null | undefined): Entry | null {
  const text = String(value ?? "").trim();
  if (text === "customer" || text === "internal" || text === "admin") return text;
  return null;
}

/** 清掉某个入口的**一整套**登录态（访问 + 刷新）。 */
export function clearSessionPair(entry: Entry, storage: StorageLike): void {
  clearSession(entry, storage);
  storage.removeItem(refreshKey(entry));
}

export interface ActiveSession {
  token: string;
  role: string;
  /** 主体标识串（`c2` / `e1`）——**不是**数字 user_id（变更 001） */
  subject: string;
  entry: Entry;
  sessionId: string | null;
}

/** 读当前入口的登录态（含会话 id）。过期/坏令牌一律视为未登录（`readSession` 已处理）。 */
export function readActiveSession(entry: Entry, storage: StorageLike): ActiveSession | null {
  const session = readSession(entry, storage);
  if (!session) return null;
  return {
    token: session.token,
    role: session.role,
    subject: session.userId,
    entry,
    sessionId: readSessionId(entry, storage),
  };
}

/** 进行中的刷新（按入口去重）——**这是正确性要求，不只是省一次往返**。
 *
 * 为什么必须去重：刷新令牌在服务端是「换一对新的」，**多个请求同时刷新会互相作废**
 * （后一个用旧刷新令牌去换，可能撞上 `token_version` 已变而拿到 401）。
 * 症状是「页面一打开就莫名跳登录页」—— 而它只在并发请求时出现，最难复现。
 * 所以同一入口的并发刷新共享同一个 Promise。
 */
const inFlightRefresh = new Map<Entry, Promise<string | null>>();

/**
 * 确保拿到一条**还能用一会儿**的访问令牌；快过期时用刷新令牌换新的。
 *
 * 返回值是最终可用的令牌；完全没法用（没有会话 / 刷新失败）时返回 `null`
 * 并**把该入口清干净**（见模块 docstring 的纪律）。
 *
 * ⚠️ 同一入口的并发调用**共享一次刷新**（见 `inFlightRefresh`）。
 */
export async function ensureFreshToken(
  entry: Entry,
  storage: StorageLike,
  options: { fetchImpl?: typeof fetch; now?: () => number; leewaySeconds?: number } = {},
): Promise<string | null> {
  const session = readSession(entry, storage);
  if (!session) return null;

  const nowSeconds = (options.now ?? (() => Date.now()))() / 1000;
  const leeway = options.leewaySeconds ?? REFRESH_LEEWAY_SECONDS;
  const claims = decodeJwtPayload(session.token);
  const exp = claims?.exp ?? 0;
  if (exp > nowSeconds + leeway) return session.token; // 还够用，不再付一次往返

  return refreshTokenNow(entry, storage, options);
}

/**
 * **强制**刷新（跳过「还够用」的短路）。401 之后的补救走这里。
 *
 * 与 `ensureFreshToken` 的分工：那个是「提前续期」（预防），这个是
 * 「已经被拒了，换一条再试一次」（补救）。两条路径都可能被并发触发，所以共用同一份去重。
 */
export function forceRefreshToken(
  entry: Entry,
  storage: StorageLike,
  options: { fetchImpl?: typeof fetch } = {},
): Promise<string | null> {
  return refreshTokenNow(entry, storage, options);
}

function refreshTokenNow(
  entry: Entry,
  storage: StorageLike,
  options: { fetchImpl?: typeof fetch },
): Promise<string | null> {
  const existing = inFlightRefresh.get(entry);
  if (existing) return existing;

  const task = (async (): Promise<string | null> => {
    const session = readSession(entry, storage);
    if (!session) return null;

    const refreshToken = readRefreshToken(entry, storage);
    if (!refreshToken) {
      // 没有刷新令牌（例如手粘的演示令牌）：**不当作错误**，把现有令牌交出去。
      // 它可能还能用几分钟；用不了时后端会 401，那时再跳登录页。
      return session.token;
    }

    let payload: TokenPayload;
    try {
      payload = await postRefresh(refreshToken, { fetchImpl: options.fetchImpl });
    } catch {
      // 刷新失败 = 账号被停用/口令被改/令牌被作废。**清干净**，不要留半条会话。
      clearSessionPair(entry, storage);
      return null;
    }
    const resolved = writeSessionPair(payload, storage, { entry });
    return readSession(resolved, storage)?.token ?? null;
  })();

  inFlightRefresh.set(entry, task);
  // ⚠️ 必须在**两个分支**都清掉：失败时不清会让这个入口永远拿到那个 rejected/null 的结果
  //    （之后所有刷新都直接返回旧结果，看起来像「刷新突然不生效了」）。
  void task.finally(() => {
    if (inFlightRefresh.get(entry) === task) inFlightRefresh.delete(entry);
  });
  return task;
}

/** 测试用：清掉进行中的刷新记录（避免用例之间互相影响）。 */
export function resetInFlightRefresh(): void {
  inFlightRefresh.clear();
}

/**
 * ★**带令牌执行一个请求，401 时自动刷新并重试一次**（issue 003 的 P1-2 修复点）。
 *
 * ## 为什么需要它（这是「刷新令牌实现了却没用」的接线）
 *
 * 修复前 `ensureFreshToken` / `postRefresh` 写得完整、测试也覆盖了，但**全项目没有一处调用**：
 * 各调用点都是 `readSession()` 拿到 token 就直接发请求。于是访问令牌 30 分钟一到期，
 * 用户就被硬性踢回登录页 —— 而刷新令牌明明还躺在 localStorage 里（14 天有效）。
 *
 * ## 两条路径都覆盖
 *
 * | 时机 | 走哪条 |
 * |---|---|
 * | 请求前发现令牌快过期 | `ensureFreshToken`（**预防**：提前 60s 换新） |
 * | 请求已发出但被 401 拒 | `forceRefreshToken`（**补救**：换一条再试一次） |
 *
 * 只做预防是不够的：页面可能一直开着不刷新，令牌在两次请求之间过期；
 * 只做补救也是不够的：那样每个请求都要先撞一次 401 才换令牌（多一个往返）。
 *
 * ## 三条纪律
 *
 * 1. **只重试一次**：第二次仍 401 说明刷新令牌也无效了，再试就是死循环；
 * 2. **拿不到新令牌就抛原来的 401**：让调用方按既有逻辑跳登录页，而不是抛一个奇怪的错误；
 * 3. **重试前比对令牌**：若刷新返回的还是同一条（例如没有刷新令牌），不必再打一次。
 */
export async function withFreshToken<T>(
  entry: Entry,
  storage: StorageLike,
  run: (token: string) => Promise<T>,
  options: { fetchImpl?: typeof fetch } = {},
): Promise<T> {
  const token = await ensureFreshToken(entry, storage, options);
  if (!token) {
    throw new SessionExpiredError("没有可用的登录态：请重新登录");
  }
  try {
    return await run(token);
  } catch (exc) {
    if (!isUnauthorizedError(exc)) throw exc;
    const refreshed = await forceRefreshToken(entry, storage, options);
    if (!refreshed || refreshed === token) throw exc;
    return await run(refreshed);
  }
}

/** 登录态完全不可用（没有会话 / 刷新也失败）时抛它。 */
export class SessionExpiredError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SessionExpiredError";
  }
}

/**
 * 这个异常是不是「该重新登录」的 401。
 *
 * ⚠️ 用**结构化判据**（`status === 401`）而不是 `instanceof ApiError`：
 * 本模块不该硬依赖 `api.ts` 的类（那会让「换一个 HTTP 客户端」变成一次大改），
 * 而且测试里的假异常对象也能被正确识别。
 */
function isUnauthorizedError(exc: unknown): boolean {
  if (typeof exc !== "object" || exc === null) return false;
  const status = (exc as { status?: unknown }).status;
  if (status === 401) return true;
  const code = (exc as { code?: unknown }).code;
  return code === "unauthorized";
}

/** 浏览器环境下的便捷封装（SSR 时退化成内存实现，见 `auth.browserStorage`）。 */
export function currentStorage(): StorageLike {
  return browserStorage();
}

/**
 * 判断一条令牌**是不是**刷新令牌（用来给用户一句准确的提示）。
 *
 * 与 `auth.tokenKind` 的区别：这里只需要「是/不是」，而 `tokenKind` 有三态
 * （`access` / `refresh` / `unknown`）—— 三态是给**粘贴令牌**的登录页用的，
 * 因为「声明缺失」与「声明 refresh」的修法不同。
 */
export function looksLikeRefreshToken(token: string): boolean {
  return !isAccessToken(token) && decodeJwtPayload(token)?.tokenType === "refresh";
}

export { tokenKey };
