/**
 * 角色分流与登录态的单元测试（离线）。
 *
 * 测的是**界面边界**，不是安全边界（见 `src/lib/auth.ts` 的模块注释）：
 * 「客户不该看到对内入口的按钮」是这一层的事；「客户拿不到别人的订单」
 * 是后端行级过滤的事 —— 后端那条已由 `tests/api/test_authorization.py` 覆盖。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  canUseInternal,
  clearSession,
  decodeJwtPayload,
  defaultEntry,
  DEMO_PRESETS,
  hasPermission,
  isAccessToken,
  isExpired,
  memoryStorage,
  newSessionId,
  PERM_READ_ALL_ORDERS,
  PERM_READ_FINANCE,
  readSession,
  readSessionId,
  ROLE_PERMISSIONS,
  tokenKey,
  writeSession,
  writeSessionId,
} from "../src/lib/auth.ts";
import { SESSION_ID_PATTERN } from "../src/lib/contracts.ts";

/** 造一个**形状真实**的 JWT（不验签，前端只解码 payload）。 */
function fakeJwt(payload: Record<string, unknown>): string {
  const encode = (value: unknown) =>
    Buffer.from(JSON.stringify(value)).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${encode({ alg: "HS256", typ: "JWT" })}.${encode(payload)}.signature-not-verified`;
}

const future = () => Math.floor(Date.now() / 1000) + 1800;
const past = () => Math.floor(Date.now() / 1000) - 10;

test("权限表：客户没有 read:all_orders，员工有", () => {
  assert.equal(canUseInternal("customer"), false);
  assert.equal(canUseInternal("internal_employee"), true);
  assert.equal(canUseInternal("finance_auditor"), true);
  assert.equal(canUseInternal("admin"), true);
  assert.equal(canUseInternal("未知角色"), false, "表里没有的角色 = 没有任何权限（默认拒绝）");
});

test("权限表：只有财务审计与管理员有 read:finance", () => {
  assert.equal(hasPermission("finance_auditor", PERM_READ_FINANCE), true);
  assert.equal(hasPermission("internal_employee", PERM_READ_FINANCE), false);
  assert.equal(hasPermission("customer", PERM_READ_FINANCE), false);
  assert.equal(hasPermission("admin", PERM_READ_FINANCE), true);
});

test("默认入口：内部角色进对内页，客户进对客页", () => {
  assert.equal(defaultEntry("internal_employee"), "internal");
  assert.equal(defaultEntry("customer"), "customer");
});

test("解码令牌：能取出 sub/role/exp/type", () => {
  const token = fakeJwt({ sub: "7", role: "internal_employee", exp: future(), type: "access" });
  const claims = decodeJwtPayload(token);
  assert.equal(claims?.sub, "7");
  assert.equal(claims?.role, "internal_employee");
  assert.equal(claims?.tokenType, "access");
});

test("解码令牌：三段不对/坏 base64 一律返回 null（**不抛**）", () => {
  assert.equal(decodeJwtPayload("not-a-jwt"), null);
  assert.equal(decodeJwtPayload("a.b"), null);
  assert.equal(decodeJwtPayload("a.!!!.c"), null);
});

test("过期判定", () => {
  assert.equal(isExpired(fakeJwt({ sub: "1", exp: past() })), true);
  assert.equal(isExpired(fakeJwt({ sub: "1", exp: future() })), false);
  assert.equal(isExpired("坏令牌"), true, "解不出来 = 当成过期（别把坏令牌放进去）");
});

test("令牌类型：刷新令牌不算访问令牌（它在后端一定 401）", () => {
  assert.equal(isAccessToken(fakeJwt({ sub: "1", exp: future(), type: "access" })), true);
  assert.equal(isAccessToken(fakeJwt({ sub: "1", exp: future(), type: "refresh" })), false);
  assert.equal(isAccessToken(fakeJwt({ sub: "1", exp: future() })), false);
});

test("readSession：有效访问令牌 → 会话；过期 → null 且顺手清掉", () => {
  const storage = memoryStorage();
  const good = fakeJwt({ sub: "2", role: "customer", exp: future(), type: "access" });
  writeSession({ token: good, userId: "2", role: "customer", entry: "customer" }, storage);
  assert.equal(readSession("customer", storage)?.role, "customer");

  const expired = fakeJwt({ sub: "2", role: "customer", exp: past(), type: "access" });
  storage.setItem(tokenKey("customer"), expired);
  assert.equal(readSession("customer", storage), null);
  assert.equal(storage.getItem(tokenKey("customer")), null, "过期令牌必须被清掉（否则每次都要重登还查不出原因）");
});

test("readSession：刷新令牌与坏令牌都视为未登录", () => {
  const storage = memoryStorage();
  storage.setItem(tokenKey("customer"), fakeJwt({ sub: "2", exp: future(), type: "refresh" }));
  assert.equal(readSession("customer", storage), null);
  storage.setItem(tokenKey("customer"), "garbage");
  assert.equal(readSession("customer", storage), null);
});

test("两个入口的令牌互不覆盖（同一个浏览器可以同时开对客与对内）", () => {
  const storage = memoryStorage();
  writeSession({ token: fakeJwt({ sub: "2", role: "customer", exp: future(), type: "access" }), userId: "2", role: "customer", entry: "customer" }, storage);
  writeSession({ token: fakeJwt({ sub: "7", role: "internal_employee", exp: future(), type: "access" }), userId: "7", role: "internal_employee", entry: "internal" }, storage);
  assert.equal(readSession("customer", storage)?.userId, "2");
  assert.equal(readSession("internal", storage)?.userId, "7");
  clearSession("customer", storage);
  assert.equal(readSession("customer", storage), null);
  assert.equal(readSession("internal", storage)?.userId, "7", "清对客不该动对内");
});

test("会话 id：生成的形状满足后端的字符集约束", () => {
  for (let i = 0; i < 50; i += 1) {
    const id = newSessionId();
    assert.match(id, SESSION_ID_PATTERN, `生成的会话 id 非法：${id}`);
    assert.ok(!id.includes(":"), "绝不允许 ':'（那会拼出看起来属于别人的 thread_id）");
  }
});

test("会话 id：只写入合法的值（非法的丢掉，别写进存储）", () => {
  const storage = memoryStorage();
  writeSessionId("customer", "orders-1", storage);
  assert.equal(readSessionId("customer", storage), "orders-1");
  writeSessionId("customer", "1:foo", storage);
  assert.equal(readSessionId("customer", storage), "orders-1", "非法值应当被忽略，而不是覆盖掉合法的");
});

test("权限表的形状：每个角色都是数组，且客户里没有 read:all_orders", () => {
  for (const [role, permissions] of Object.entries(ROLE_PERMISSIONS)) {
    assert.ok(Array.isArray(permissions), `${role} 的权限点必须是数组`);
  }
  assert.ok(!ROLE_PERMISSIONS.customer.includes(PERM_READ_ALL_ORDERS));
});

test("演示身份卡片：每个 role 都在权限表里（漏配会让卡片显示成「仅对客」）", () => {
  assert.ok(DEMO_PRESETS.length >= 3, "至少要有客户 / 员工 / 财务三个演示身份");
  for (const preset of DEMO_PRESETS) {
    assert.ok(
      Object.hasOwn(ROLE_PERMISSIONS, preset.role),
      `演示身份 ${preset.label} 的角色 ${preset.role} 不在前端权限表里`,
    );
    // 「对内」卡片必须是真的能进对内的角色，否则点了会被自己的门禁挡回去
    if (preset.entry === "internal") {
      assert.equal(canUseInternal(preset.role), true, `${preset.label} 标了对内，但 canUseInternal 是 false`);
    }
  }
});
