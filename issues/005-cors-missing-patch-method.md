# 005 · 管理台的「停用 / 改角色」在浏览器里失败：CORS 白名单漏了 `PATCH`

* **状态**：已修复（2026-10-16）
* **发现于**：2026-10-16，用户反馈「管理员界面刷新时报 `Failed to fetch`」
* **归属模块**：`app/api/main.py`（CORS 中间件装配，第 16 步）
* **引入于**：`179ea1b 修复后端连接失败的bug` —— CORS 中间件第一次被加上时，
  `allow_methods` 写成了 `["GET", "POST", "OPTIONS"]`，而**变更 001 的管理台用的是 `PATCH`**；
  `PATCH` 是**非简单方法**，会先发预检，预检被 400 拒 → 浏览器根本不会发那条真正的请求

---

## 现象

管理员在 `/admin` 点「停用 / 启用」或改「角色」下拉时，页面报：

```text
请求没有到达后端（没有状态码）：多半是后端没起或跨源被拦。原始错误：TypeError: Failed to fetch
```

浏览器 Console：

```text
Access to fetch at 'http://127.0.0.1:8000/api/admin/accounts/15' from origin 'http://127.0.0.1:3000'
has been blocked by CORS policy: Response to preflight request doesn't pass access control check:
It does not have HTTP ok status.
```

**判据（这句最关键）**：报的是 `Failed to fetch` 且**没有 HTTP 状态码**。
「令牌无效 / 权限不够」会得到 **401/403** —— 那是一个能读到的 HTTP 响应。
所以这个症状**先别怀疑令牌**（与 `16-web-runbook` 坑 11 同一条判据）。

### 实测复现（两条 curl 并排，一眼看出差别）

```text
# ① 管理台真正用的方法：PATCH
OPTIONS /api/admin/accounts/15
  Origin: http://127.0.0.1:3000
  Access-Control-Request-Method: PATCH
-> HTTP/1.1 400 Bad Request
   access-control-allow-methods: GET, POST, OPTIONS
   Disallowed CORS method                       ← 预检被拒，浏览器不会发真正的 PATCH

# ② 对照：POST 是通的
OPTIONS /api/chat
  Origin: http://127.0.0.1:3000
  Access-Control-Request-Method: POST
-> HTTP/1.1 200 OK
   access-control-allow-methods: GET, POST, OPTIONS
   OK
```

⚠️ **只影响浏览器**：`curl` / Node 直接发 `PATCH` 是好的（CORS 只约束浏览器）。
这正是本仓「真链路用例全绿、浏览器打不开」的经典成因 ——
`web/tests/live/*.ts` 是 Node 直接 `fetch`，**永远看不到 CORS 的效果**
（`tests/api/test_cors.py` 的模块 docstring 自己就写着这件事）。

## 影响

* **管理台的两个核心操作在浏览器里完全不可用**：停用/启用、改角色
  （两者都走 `PATCH /api/admin/accounts/{id}`）。
  「开账号」与「重置口令」是 `POST`，**不受影响** ——
  这就是为什么 issue 004 修完「开账号」之后，这两个操作看起来「突然坏了」。
* **症状极具误导性**：页面提示是「后端没起或跨源被拦」，而 `/health` 全绿、
  后端日志里**一条请求都没有**（预检在中间件层就被答掉了）。
  不知道判据的人会去重签令牌、重启后端 —— 全都是无效动作。

## 根因

`app/api/main.py` 的 CORS 装配把方法写成了固定三项：

```python
allow_methods=["GET", "POST", "OPTIONS"],
```

而路由实际用到三种方法（`create_app().openapi()` 实测）：

```text
GET    6 条   /api/auth/me, /api/admin/accounts, ...
PATCH  1 条   /api/admin/accounts/{account_id}      ← 不在 allow_methods 里
POST   7 条   /api/auth/login, /api/chat, ...
```

`GET`/`POST` 是 CORS 的**简单方法**（不触发预检），所以漏掉它们也看不出来；
`PATCH` **一定会触发预检**（非简单方法），于是必然撞上 `allow_methods` 白名单。

### 为什么测试没发现

既有用例（`tests/api/test_cors.py::test_preflight_is_answered_without_authentication`）
**只验了 `POST`**：

```python
response = client.options("/api/chat", headers={..., "Access-Control-Request-Method": "POST"})
assert "POST" in response.headers.get("access-control-allow-methods", "")
```

它证明的是「`allow_methods` 里的方法能过预检」，**证不了反方向**
（「路由用到的方法都在 `allow_methods` 里」）。
与 `issues/003` 的 P0-1（限流三处登记漏一处）是**同一类漏项**：
**正向断言拦不住反向缺失**。

## 修复

1. `app/api/main.py`：`allow_methods` 补上 `PATCH`，并把「为什么要显式登记方法」
   写进注释（它不是可有可无的装饰，漏一个方法就等于那个接口在浏览器里不存在）；
2. **补反向断言**（`tests/api/test_cors.py::test_every_route_method_is_allowed_by_cors`）：
   从 `create_app().openapi()` 取**路由实际用到的所有方法**，
   断言它们都在 CORS 的 `allow_methods` 里 —— 把「加一个新方法忘了登记」变成测试失败。
   同时断言 `OPTIONS` 必须在（预检本身要用）。

> 📌 **刻意不采用 `allow_methods=["*"]`**：那会放通任意方法（含 `DELETE`/`TRACE`），
> 与本项目「表白名单、默认拒绝」的安全纪律相悖（同 `cors_origins` 拒绝 `*` 的理由）。
> 显式列出 + 反向断言，既安全又能挡住漏项。

### 为什么 `max_age=600` 让这个 bug 更隐蔽

预检结果会被浏览器缓存 10 分钟（`access-control-max-age: 600`）。
所以「后端改了 `allow_methods`」之后，**已经缓存过失败预检的页面不会立刻恢复** ——
需要硬刷新（或等 10 分钟）。这不是 bug，但排障时容易误判成「改了没用」。

## 验证

```text
修复前： OPTIONS /api/admin/accounts/15 (ACRM=PATCH) -> 400  Disallowed CORS method
修复后： OPTIONS /api/admin/accounts/15 (ACRM=PATCH) -> 200  OK
        access-control-allow-methods: GET, POST, PATCH, OPTIONS
```

反向验证（把 `PATCH` 从 `allow_methods` 删回去 → 新用例立刻红）：

```text
FAILED tests/api/test_cors.py::test_every_route_method_is_allowed_by_cors
  AssertionError: 这些方法被路由用到、但 CORS 没放通：['PATCH']
  —— 浏览器会先发预检，预检被 400 拒 → 那条请求根本不会发出（症状：Failed to fetch，无状态码）
```

真浏览器验收见 [`docs/实测报告-2026-10-16.md`](../docs/实测报告-2026-10-16.md) §7，
脚本 `web/scripts/admin-patch-evidence.mjs`（无头 Chrome 点「停用」并观察
预检状态码 + 列表里的状态确实翻转）。
