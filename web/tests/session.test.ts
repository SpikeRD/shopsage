/**
 * 会话存储层与刷新续期的单元测试（变更 001 / T15，离线）。
 *
 * ## 这个文件测的是三件容易出错、又不容易在手工点页面时发现的事
 *
 * 1. **三把钥匙互不覆盖**：登录管理台不能把对客页的登录态顶掉；
 * 2. **入口以服务端返回的 `entry` 为准**：前端不复制「哪个角色进哪个界面」的规则；
 * 3. **刷新失败必须清干净**：留下半条会话的症状是「一直转圈然后莫名跳登录页」。
 *
 * ⚠️ 全部离线：`fetch` 由用例注入（`fetchImpl`），不碰真后端。
 * 「真链路能不能跑通」由 `web/tests/run-live.mjs` 与 `tests/api/test_auth_accounts.py` 覆盖。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  canUseAdmin,
  defaultEntry,
  decodeJwtPayload,
  entryLabel,
  entryPath,
  isAccountToken,
  memoryStorage,
  readSession,
  writeSession,
} from "../src/lib/auth.ts";
import {
  clearSessionPair,
  ensureFreshToken,
  readActiveSession,
  readRefreshToken,
  refreshKey,
  writeSessionPair,
} from "../src/lib/session.ts";
import type { TokenPayload } from "../src/lib/api.ts";

/** 造一个**形状真实**的 JWT（不验签，前端只解码 payload）。 */
function fakeJwt(payload: Record<string, unknown>): string {
  const encode = (value: unknown) =>
    Buffer.from(JSON.stringify(value))
      .toString("base64")
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  return `${encode({ alg: "HS256", typ: "JWT" })}.${encode(payload)}.sig`;
}

/** 一个「变更 001 之后」的访问令牌（带 `aid`/`stype`，所以 `isAccountToken` 为真）。 */
function accountToken(
  role: string,
  subject: string,
  subjectType: string,
  ttlSeconds = 1800,
  accountId = 7,
): string {
  return fakeJwt({
    sub: subject,
    stype: subjectType,
    sid: Number(subject.replace(/^[ce]/, "")),
    aid: accountId,
    ver: 0,
    role,
    typ: "access",
    iat: Math.floor(Date.now() / 1000),
    exp: Math.floor(Date.now() / 1000) + ttlSeconds,
  });
}

function payloadFor(
  role: string,
  subject: string,
  subjectType: string,
  entry: string,
  options: { ttlSeconds?: number; accountId?: number } = {},
): TokenPayload {
  const accountId = options.accountId ?? 7;
  return {
    accessToken: accountToken(role, subject, subjectType, options.ttlSeconds ?? 1800, accountId),
    refreshToken: fakeJwt({ sub: subject, typ: "refresh", exp: Math.floor(Date.now() / 1000) + 999999 }),
    tokenType: "bearer",
    expiresIn: options.ttlSeconds ?? 1800,
    refreshExpiresIn: 999999,
    accountId,
    subject,
    subjectType,
    role,
    entry,
    permissions: [],
  };
}

// ── 入口分流（三档）──────────────────────────────────────────────────────────

test("canUseAdmin 只认 admin 权限点，不看角色名", () => {
  assert.equal(canUseAdmin("admin"), true);
  assert.equal(canUseAdmin("internal_employee"), false, "能读全站订单不等于能开账号");
  assert.equal(canUseAdmin("manager"), false);
  assert.equal(canUseAdmin("customer"), false);
  assert.equal(canUseAdmin("nobody"), false, "未登记角色按默认拒绝");
});

test("defaultEntry 的优先级与后端 entry_for_role 一致（管理员优先于对内）", () => {
  // ⚠️ 顺序不能反：admin 也带 read:all_orders，先判对内就永远进不了管理台
  assert.equal(defaultEntry("admin"), "admin");
  assert.equal(defaultEntry("internal_employee"), "internal");
  assert.equal(defaultEntry("finance_auditor"), "internal");
  assert.equal(defaultEntry("customer"), "customer");
  assert.equal(defaultEntry("nobody"), "customer");
});

test("entryPath / entryLabel 是三档的一处映射", () => {
  assert.equal(entryPath("admin"), "/admin");
  assert.equal(entryPath("internal"), "/internal");
  assert.equal(entryPath("customer"), "/");
  assert.equal(entryLabel("admin"), "管理台");
});

// ── 令牌形态识别 ─────────────────────────────────────────────────────────────

test("isAccountToken 认得出变更 001 之前签的旧格式令牌", () => {
  const legacy = fakeJwt({ sub: "2", role: "customer", typ: "access", exp: 9999999999 });
  assert.equal(isAccountToken(legacy), false, "旧令牌没有 aid/stype，后端一定拒");
  assert.equal(isAccountToken(accountToken("customer", "c2", "customer")), true);
});

// ── 一对令牌的存储 ───────────────────────────────────────────────────────────

test("writeSessionPair 用**服务端返回的 entry**，而不是前端算的", () => {
  const storage = memoryStorage();
  // 角色是 customer（前端会算成 customer 入口），但服务端说进 internal —— 以服务端为准
  const entry = writeSessionPair(payloadFor("customer", "c2", "customer", "internal"), storage);
  assert.equal(entry, "internal");
  // ⚠️ `?.` 之后再取 `.length`：`readSession` 可能返回 null，
  // 而 TS 的 `?.` 只保护到它自己那一层（写 `?.token.length > 0` 会报「可能 undefined」）。
  assert.ok((readSession("internal", storage)?.token.length ?? 0) > 0);
});

test("服务端没给 entry（或给了不认识的词）时退回前端兜底", () => {
  const storage = memoryStorage();
  assert.equal(writeSessionPair(payloadFor("admin", "e1", "employee", ""), storage), "admin");
  const storage2 = memoryStorage();
  assert.equal(writeSessionPair(payloadFor("admin", "e1", "employee", "nonsense"), storage2), "admin");
});

test("三把钥匙互不覆盖：登录管理台不影响对客页的登录态", () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("customer", "c2", "customer", "customer"), storage);
  writeSessionPair(payloadFor("admin", "e1", "employee", "admin"), storage);
  assert.equal(readSession("customer", storage)?.role, "customer");
  assert.equal(readSession("admin", storage)?.role, "admin");
  assert.notEqual(refreshKey("customer"), refreshKey("admin"));
});

test("clearSessionPair 把访问令牌与刷新令牌一起清掉", () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("admin", "e1", "employee", "admin"), storage);
  assert.ok(readRefreshToken("admin", storage));
  clearSessionPair("admin", storage);
  assert.equal(readSession("admin", storage), null);
  assert.equal(readRefreshToken("admin", storage), null, "留下刷新令牌会让下次又去试一遍");
});

test("readActiveSession 把主体标识串原样带出来（不是数字 user_id）", () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("customer", "c2", "customer", "customer"), storage);
  const active = readActiveSession("customer", storage);
  assert.equal(active?.subject, "c2");
  assert.equal(active?.role, "customer");
});

// ── 刷新续期 ─────────────────────────────────────────────────────────────────

test("令牌还够用时**不**发刷新请求（省一次往返）", async () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("customer", "c2", "customer", "customer", { ttlSeconds: 1800 }), storage);
  let called = 0;
  const token = await ensureFreshToken("customer", storage, {
    fetchImpl: (async () => {
      called += 1;
      throw new Error("不该被调用");
    }) as unknown as typeof fetch,
  });
  assert.equal(called, 0);
  assert.ok(token);
});

test("令牌快过期时用刷新令牌换一对新的（并把新的存下来）", async () => {
  const storage = memoryStorage();
  // 只剩 10 秒（小于 60 秒的提前量）→ 必须刷新
  writeSessionPair(payloadFor("customer", "c2", "customer", "customer", { ttlSeconds: 10 }), storage);
  const before = readSession("customer", storage)?.token;

  // ⚠️ **不能靠「新旧令牌字符串不同」来断言**：JWT 在同一秒内是确定性的
  // （同样的 payload + 同样的 iat/exp ⇒ 同样的串），所以「刷新过」与「没刷新」
  // 可能给出**一模一样**的字符。本仓在 live 用例里已经记过这件事。
  // 判据必须是**可区分的事实**：这里让「刷新响应」带一个不同的 account_id，
  // 于是「存储被换成了新响应」这件事可断言。
  const refreshed = {
    ...payloadFor("customer", "c2", "customer", "customer", { ttlSeconds: 1800, accountId: 4242 }),
  };
  let called = 0;
  const token = await ensureFreshToken("customer", storage, {
    fetchImpl: (async () => {
      called += 1;
      return new Response(JSON.stringify(respondWith(refreshed)), { status: 200 });
    }) as unknown as typeof fetch,
  });

  assert.equal(called, 1, "快过期时必须真的发一次刷新请求");
  assert.ok(token);
  assert.equal(token, readSession("customer", storage)?.token, "返回的应当是**存下来的那一条**");
  assert.equal(decodeAccountId(token as string), 4242, "存储里已经是刷新回来的新令牌");
  // 旧的没有刷新令牌、新的有 —— 两者确实不同（用可区分的事实验，不靠字符串比较）
  assert.ok(readRefreshToken("customer", storage));
  assert.notEqual(before, token, "刷新响应换了 account_id，串必然不同");
});


test("tokenKey/refreshKey 每个入口一套（不共用）", () => {
  assert.notEqual(refreshKey("customer"), refreshKey("internal"));
  assert.notEqual(refreshKey("internal"), refreshKey("admin"));
});

test("刷新失败必须**清干净**并返回 null（不留半条会话）", async () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("admin", "e1", "employee", "admin", { ttlSeconds: 5 }), storage);
  const token = await ensureFreshToken("admin", storage, {
    fetchImpl: (async () =>
      new Response(
        JSON.stringify({ error: { code: "invalid_refresh_token", message: "账号已停用" }, request_id: "r1" }),
        { status: 401 },
      )) as unknown as typeof fetch,
  });
  assert.equal(token, null);
  assert.equal(readSession("admin", storage), null, "访问令牌也要清掉");
  assert.equal(readRefreshToken("admin", storage), null, "刷新令牌也要清掉（否则会一直重试）");
});

test("没有刷新令牌时**不当作错误**（手粘的演示令牌就是这种情况）", async () => {
  const storage = memoryStorage();
  // 直接写入访问令牌（没有配对写刷新令牌）
  const token = accountToken("customer", "c2", "customer", 5);
  writeSession({ token, userId: "c2", role: "customer", entry: "customer" }, storage);
  let called = 0;
  const result = await ensureFreshToken("customer", storage, {
    fetchImpl: (async () => {
      called += 1;
      throw new Error("不该被调用");
    }) as unknown as typeof fetch,
  });
  assert.equal(called, 0);
  assert.equal(result, token, "把现有令牌交出去：它可能还能用几分钟，用不了时后端会 401");
});

/** 把 `TokenPayload` 转成后端 `TokenResponse` 的形状（刷新响应体）。 */
function respondWith(payload: TokenPayload): Record<string, unknown> {
  return {
    access_token: payload.accessToken,
    refresh_token: payload.refreshToken,
    token_type: payload.tokenType,
    expires_in: payload.expiresIn,
    refresh_expires_in: payload.refreshExpiresIn,
    account_id: payload.accountId,
    subject: payload.subject,
    subject_type: payload.subjectType,
    role: payload.role,
    entry: payload.entry,
    permissions: payload.permissions,
  };
}

/** 从令牌里读 `aid`（用来断言「存储里换成了刷新回来的那一条」）。 */
function decodeAccountId(token: string): number {
  return decodeJwtPayload(token)?.accountId ?? 0;
}
