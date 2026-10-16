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
  clearSessionsForAccount,
  currentAccountId,
  ensureFreshToken,
  readActiveSession,
  readRefreshToken,
  refreshKey,
  SessionExpiredError,
  withFreshToken,
  writeSessionPair,
} from "../src/lib/session.ts";
import { ApiError, type TokenPayload } from "../src/lib/api.ts";

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

// ── 变更 003：登录写多把钥匙（需求 1 的根因修复）────────────────────────────
//
// ⚠️ **语义变更**：变更 002 建立的「三把钥匙互不覆盖」在这里被有意改写。
// 变更 003 之前只写 `payload.entry` 指定的那一把，而每个页面只读自己那把 ——
// 于是**管理员登录后进 `/` 或 `/internal` 会看到「需要先登录」**（需求 1 的现场）。
//
// 现在：同一账号登录会把它能进入的**每一个入口**都写上同一对令牌。
// 代价是**后写覆盖先写**（同一个浏览器里不能同时持两个账号），这是决策 1 的
// 显式取舍，见 `docs/changes/003` §3.1.1。

test("★管理员登录：三个入口的钥匙都拿到同一枚令牌（需求 1）", () => {
  const storage = memoryStorage();
  const payload = payloadFor("admin", "e1", "employee", "admin");
  writeSessionPair(payload, storage);

  for (const entry of ["customer", "internal", "admin"] as const) {
    const session = readSession(entry, storage);
    assert.ok(session, `${entry} 那把钥匙是空的 —— 管理员进那个入口会显示「需要先登录」`);
    assert.equal(session.role, "admin");
    assert.equal(session.token, payload.accessToken, `${entry} 的令牌与登录响应不一致`);
    // 刷新令牌也必须一起写：只写访问令牌会让那个入口在 30 分钟后硬性掉线
    assert.equal(readRefreshToken(entry, storage), payload.refreshToken);
  }
});

test("★客户登录：只写对客那一把（不去碰对内/管理台）", () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("customer", "c2", "customer", "customer"), storage);
  assert.ok(readSession("customer", storage));
  assert.equal(readSession("internal", storage), null, "客户不该拿到对内入口的登录态");
  assert.equal(readSession("admin", storage), null, "客户更不该拿到管理台入口的登录态");
});

test("★客服登录：写对客 + 对内两把（因为他有 approve:refund，能进对内）", () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("customer_service", "e2", "employee", "internal"), storage);
  assert.ok(readSession("customer", storage));
  assert.ok(readSession("internal", storage), "客服必须能进对内，否则看不到自己的退款审批台");
  assert.equal(readSession("admin", storage), null, "客服没有 admin 权限点");
});

test("★后写覆盖先写（变更 003 的显式代价，不是 bug）", () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("customer", "c2", "customer", "customer"), storage);
  assert.equal(readSession("customer", storage)?.role, "customer");

  // 同一个浏览器里再登录管理员：他会写进 customer + internal + admin 三把钥匙，
  // 于是**客户那把被覆盖**。这是「管理员要能用三个入口」的必然代价 ——
  // 想同时持两个身份已不可能（变更 002 的能力在这里有意收回）。
  writeSessionPair(payloadFor("admin", "e1", "employee", "admin"), storage);
  assert.equal(readSession("customer", storage)?.role, "admin");
  assert.equal(readSession("internal", storage)?.role, "admin");
  assert.equal(readSession("admin", storage)?.role, "admin");
});

test("clearSessionPair 把访问令牌与刷新令牌一起清掉", () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("admin", "e1", "employee", "admin"), storage);
  assert.ok(readRefreshToken("admin", storage));
  clearSessionPair("admin", storage);
  assert.equal(readSession("admin", storage), null);
  assert.equal(readRefreshToken("admin", storage), null, "留下刷新令牌会让下次又去试一遍");
});

// ── 变更 003：退出必须按**账号**清（决策 14）────────────────────────────────

test("★退出按账号清：管理员退出把三个入口一起清掉（不留隐形会话）", () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("admin", "e1", "employee", "admin", { accountId: 1 }), storage);
  assert.equal(currentAccountId(storage), 1);

  const cleared = clearSessionsForAccount(1, storage);
  assert.deepEqual([...cleared].sort(), ["admin", "customer", "internal"]);
  for (const entry of ["customer", "internal", "admin"] as const) {
    assert.equal(readSession(entry, storage), null, `${entry} 没清干净：退出后那个入口还能用`);
    assert.equal(readRefreshToken(entry, storage), null);
  }
});

test("★退出不误伤别的账号的钥匙", () => {
  const storage = memoryStorage();
  // 客户 7 在客户入口登录（只有一把钥匙）
  writeSessionPair(payloadFor("customer", "c2", "customer", "customer", { accountId: 7 }), storage);
  // 另一台账号（id=1）的令牌被手工塞进 admin 入口
  const adminToken = accountToken("admin", "e1", "employee", 1800, 1);
  writeSession({ token: adminToken, userId: "e1", role: "admin", entry: "admin" }, storage);

  const cleared = clearSessionsForAccount(1, storage);
  assert.deepEqual(cleared, ["admin"], "只该清掉 id=1 自己的那把钥匙");
  assert.ok(readSession("customer", storage), "客户 7 的登录态被误清了");
});

test("★账号 id 解不出来（手粘令牌）时保守地清全部", () => {
  const storage = memoryStorage();
  // `aid` 缺失的令牌（形状合法但 accountId=0）——后端本来就会拒它
  const legacy = fakeJwt({
    sub: "c2",
    role: "customer",
    typ: "access",
    exp: Math.floor(Date.now() / 1000) + 1800,
  });
  writeSession({ token: legacy, userId: "c2", role: "customer", entry: "customer" }, storage);
  const cleared = clearSessionsForAccount(0, storage);
  assert.deepEqual(cleared, ["customer"], "解不出归属时宁可多清一点，也不要留隐形会话");
  assert.equal(readSession("customer", storage), null);
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

// ── withFreshToken：401 自动续期并重试（issue 003 的 P1-2）────────────────────

test("★401 时自动刷新令牌并重试一次（而不是把用户踢回登录页）", async () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("customer", "c2", "customer", "customer", { ttlSeconds: 1800 }), storage);
  const refreshed = payloadFor("customer", "c2", "customer", "customer", {
    ttlSeconds: 1800,
    accountId: 4242,
  });

  const seen: string[] = [];
  const result = await withFreshToken(
    "customer",
    storage,
    async (token) => {
      seen.push(token);
      // 第一次用旧令牌 → 401；第二次（刷新后的）→ 成功
      if (seen.length === 1) {
        throw new ApiError({ status: 401, code: "unauthorized", message: "令牌过期" });
      }
      return "ok";
    },
    {
      fetchImpl: (async () =>
        new Response(JSON.stringify(respondWith(refreshed)), { status: 200 })) as unknown as typeof fetch,
    },
  );

  assert.equal(result, "ok");
  assert.equal(seen.length, 2, "401 之后必须重试一次");
  assert.notEqual(seen[0], seen[1], "重试用的必须是刷新后的令牌");
  assert.equal(decodeAccountId(seen[1]), 4242);
});

test("★只重试一次：第二次仍 401 就把错误抛出去（不进入死循环）", async () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("customer", "c2", "customer", "customer", { ttlSeconds: 1800 }), storage);
  const refreshed = payloadFor("customer", "c2", "customer", "customer", {
    ttlSeconds: 1800,
    accountId: 4242,
  });
  let attempts = 0;
  await assert.rejects(
    () =>
      withFreshToken(
        "customer",
        storage,
        async () => {
          attempts += 1;
          throw new ApiError({ status: 401, code: "unauthorized", message: "还是不行" });
        },
        {
          fetchImpl: (async () =>
            new Response(JSON.stringify(respondWith(refreshed)), { status: 200 })) as unknown as typeof fetch,
        },
      ),
    /还是不行/,
  );
  assert.equal(attempts, 2, "最多两次：一次原请求 + 一次重试");
});

test("非 401 的错误**不**触发刷新（避免无谓的令牌轮换）", async () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("customer", "c2", "customer", "customer", { ttlSeconds: 1800 }), storage);
  let refreshCalled = 0;
  await assert.rejects(
    () =>
      withFreshToken(
        "customer",
        storage,
        async () => {
          throw new ApiError({ status: 500, code: "internal_error", message: "服务端炸了" });
        },
        {
          fetchImpl: (async () => {
            refreshCalled += 1;
            return new Response("{}", { status: 200 });
          }) as unknown as typeof fetch,
        },
      ),
    /服务端炸了/,
  );
  assert.equal(refreshCalled, 0, "500 不该触发刷新");
});

test("★并发刷新只发一次请求（刷新令牌互相作废是真实故障）", async () => {
  const storage = memoryStorage();
  writeSessionPair(payloadFor("customer", "c2", "customer", "customer", { ttlSeconds: 5 }), storage);
  const refreshed = payloadFor("customer", "c2", "customer", "customer", {
    ttlSeconds: 1800,
    accountId: 4242,
  });
  let refreshCalls = 0;
  const fetchImpl = (async () => {
    refreshCalls += 1;
    return new Response(JSON.stringify(respondWith(refreshed)), { status: 200 });
  }) as unknown as typeof fetch;

  // 三个并发调用（模拟页面同时发起多个请求）
  const results = await Promise.all([
    ensureFreshToken("customer", storage, { fetchImpl }),
    ensureFreshToken("customer", storage, { fetchImpl }),
    ensureFreshToken("customer", storage, { fetchImpl }),
  ]);

  assert.equal(refreshCalls, 1, "并发刷新必须共享一次请求（否则会互相作废刷新令牌）");
  assert.ok(results.every((token) => token !== null));
  assert.equal(decodeAccountId(results[0] as string), 4242);
});

test("没有登录态时 withFreshToken 抛 SessionExpiredError", async () => {
  const storage = memoryStorage();
  await assert.rejects(
    () => withFreshToken("customer", storage, async () => "never"),
    (exc: unknown) => exc instanceof SessionExpiredError,
  );
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
