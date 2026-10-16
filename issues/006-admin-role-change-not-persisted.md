# 006 · 管理台「改角色」提示成功但**实际没生效**（异步读受控 select 的旧值）

* **状态**：已修复（2026-10-16）
* **发现于**：2026-10-16，用户反馈「管理员修改账户角色实际上没有生效，但前端提示修改成功」
* **归属模块**：`web/src/app/(app)/admin/page.tsx`（管理台，变更 001 + 变更 002）
* **引入于**：变更 001 写这个下拉框时就有的形态；
  issue 005（CORS 漏 `PATCH`）修好之后，这个「请求终于发得出去」的路径才暴露出来

---

## 现象

管理员在 `/admin` 用账号行里的下拉框改角色，页面**明确提示成功**：

```text
成功  已把 probe-role-1 的角色改为 manager（在途令牌已失效）。
```

**但角色的实际权限没变**，刷新也还是原样。实测对照（真浏览器 + 直接问后端）：

```text
页面提示：已把 probe-role-1 的角色改为 manager
后端实际：role = customer        ← 根本没改
```

## 影响

* **静默的权限变更失败**：管理员以为某人已经升/降权（例如把 `customer` 提成 `manager`），
  实际权限**一个都没变**。这类错误在「收回权限」的方向上尤其危险 ——
  管理员以为已经降级了某个账号，对方却仍然保有原权限。
* **完全误导**：提示语是「成功」，界面下拉框也显示成了新值；
  唯一的破绽是**刷新之后会变回去**（因为后端从来没变过）。
  → 不知道的人会以为「前端有缓存」，然后去清浏览器缓存 —— 一条无效的排障路。
* 与 issue 005 叠加时更难查：先是 `PATCH` 因 CORS 被拦（页面报 `Failed to fetch`），
  修好之后变成「提示成功但没生效」—— 症状完全不同，容易以为是修坏了。

## 根因

**受控 `<select>` 的值在 `onChange` 返回后会被 React 回滚，而请求体是异步构造的。**

这个下拉框是**受控**的：

```tsx
<select value={account.role}                 // ← 受控：显示什么由 state 决定
        onChange={(event) =>
          void run(                          // ← 异步：thunk 稍后才执行
            () => patchAccount(..., { role: event.target.value }),   // ← 这里读值
            `已把 ${account.identifier} 的角色改为 ${event.target.value}…`,
          )
        }>
```

用户选 `manager` 之后的**实测时序**（真浏览器取证，脚本见下）：

```text
change 同步 = manager     ← 只有这一瞬间 DOM 的值是对的
microtask   = customer    ← 已经被 React 回滚成旧值
t+0         = customer
t+50ms      = customer
```

为什么会回滚：`onChange` 里的 `run()` 第一句就是 `setNotice(null)`，
它调度了一次重渲染；而这次重渲染时 `account.role` **还是旧值**（列表还没重新拉），
于是 React 按受控语义把 DOM 的 value **写回** `account.role` = `customer`。

于是：

| 求值时机 | 读到什么 | 后果 |
|---|---|---|
| 提示语（**同步**求值，`run()` 的实参） | `manager` | 提示说「已改为 manager」 |
| 请求体（**异步**求值，thunk 真正执行时） | `customer` | PATCH 发的是**原角色** = 什么都没改 |

后端收到 `{"role":"customer"}`（与当前值相同）→ `update_account` 里
`str(row.role) != str(role)` 为假 → **不修改**、不自增 `token_version` →
返回 200 与当前记录。前端拿到 200 → 提示成功。

> 📌 **后端是对的**，而且这个「没变化就不写」的短路本身是**正确**的
> （避免无谓的 `token_version` 自增把用户踢下线）。
> 坏的只有「前端从 DOM 里读了什么值」这一步 —— 所以服务端用例永远测不出这个 bug：
> 它们直接构造 `PATCH {"role":"manager"}`，走的是**正确的**那条路。

### 为什么测试没发现

* 离线用例（`web/tests/*.test.ts`）**不渲染 React**，没有「受控组件回滚」这回事；
* `hook-wiring.test.ts` 原有的源码断言查的是**接线**（有没有调用某个函数），
  查不出「值是在**错误的时机**读的」这种**时序**问题；
* 服务端用例直接发正确的请求体，**从没经过「从 DOM 读值」这一步**。

## 修复

在 `onChange` 的**同步阶段**把值取出来，再交给异步 thunk：

```tsx
onChange={(event) => {
  // ⚠️ 必须在事件的同步阶段就把值取出来：受控 select 会在异步 thunk 执行前被回滚
  const nextRole = event.target.value;
  void run(
    () => withFreshToken(ENTRY, browserStorage(), (fresh) =>
      patchAccount(fresh, account.accountId, { role: nextRole })),
    `已把 ${account.identifier} 的角色改为 ${nextRole}（在途令牌已失效）。`,
  );
}}
```

提示语与请求体从此读的是**同一个值**，两者不可能再不一致。

> 📌 **刻意不改后端**：后端「值没变就不写」的短路是对的（见上）。
> 也不改 `run()` 的接口 —— 它本来就接受一个异步 thunk，问题在调用方传了什么进去。

### 顺带核查了同类写法

`web/src` 里其余 `event.target.value` 都在**同步**回调里立即使用
（`setText(...)` / `setDraft(...)` / `setFilters(...)`），没有这个 bug。
受影响的只有「把事件值捕获进异步 thunk」这一种形态 —— 本文件里就是改角色这一处。

## 验证

见 [`docs/实测报告-2026-10-16.md`](../docs/实测报告-2026-10-16.md) §8。

**真浏览器取证**（`web/scripts/admin-role-evidence.mjs`，把修复撤掉后重跑：

```text
FAIL  PATCH 请求体里的 role 等于用户选的值（manager）
      实际请求体 = {"role":"customer"}
      ← issue 006 的原始故障：发的是**旧值** customer（等于什么都没改）
FAIL  后端里 probe-role-1 的角色**真的**变成了 manager     实际 role=customer
FAIL  列表里显示的角色也变成了 manager                     role=customer
```

装回修复后 **6 条全过**：

```text
PASS  PATCH 请求体里的 role 等于用户选的值（manager）        实际 {"role":"manager"}
PASS  后端里 probe-role-1 的角色**真的**变成了 manager       实际 role=manager entry=internal
PASS  列表里显示的角色也变成了 manager                       界面与后端一致
PASS  页面提示语里说的角色与真正生效的角色一致
```

**源码级防回归**（`web/tests/hook-wiring.test.ts` 新增 2 条）：
把修复退回旧写法即红（实测）：

```text
✖ ★管理台改角色：必须同步取出事件值（受控 select 会在异步 thunk 执行前被回滚）
✖ ★管理台：所有异步 thunk 里都不许直接读 `event.target.value`
```

判据是**结构**而不是行为：`void run(...)` 的 thunk 文本里不许出现 `event.target.value`。
这挡得住「有人为了省一个变量把它改回旧写法」，且不依赖浏览器。
