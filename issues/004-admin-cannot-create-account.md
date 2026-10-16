# 004 · 管理员在管理台开账号**总是失败**（口令策略只在后端、前端一无所知）

* **状态**：已修复（2026-10-16）
* **发现于**：2026-10-16，用户反馈「管理员无法创建账户」→ 用真浏览器（无头 Chrome + CDP）**模拟前端操作**复现
* **归属模块**：`web/src/app/(app)/admin/page.tsx`（第 17/18 步：变更 001 的管理台 + 变更 002 的前端改版）
* **引入于**：[`issues/003`](003-security-and-correctness-fixes.md) 的 **P1-3**（口令强度只在后端落地，前端没跟上）

---

## 现象

管理员在 `/admin` 页填「登录标识 + 初始口令 + 角色 + 主体」后点「开账号」，
**请求确实发出去了，但后端一律 422**，页面上只有一句读完不知道该改什么的：

```text
ERROR: 请求参数不合规（HTTP 422，request_id=0340062fd0dd401d）
```

复现步骤（无头 Chrome 真点页面，见「证据」）：

```text
① 打开 http://127.0.0.1:3000/login，用 admin / admin-demo-pass 登录 → 落在 /admin（正常）
② 在「开账号」里填：标识=probe-affypn  口令=pass1234  角色=customer  主体=customer#6
③ 点「开账号」
   → POST /api/admin/accounts  HTTP 422
   → 请求体 {"identifier":"probe-affypn","password":"pass1234", ...}   ← 前端**原样发出**了
   → 页面提示「请求参数不合规（HTTP 422…）」
④ 新账号**没有**出现在列表里；表单被清空（标识/口令/主体 id 全没了）→ 管理员重填一遍，然后再失败
```

后端同一条请求的确切原因（`web/.probe/backend-422.py` 实测）：

```text
口令 1 位  (1)  -> HTTP 422  detail.fields=[{loc:[body,password], msg:"String should have at least 12 characters"}]
口令 8 位  (8)  -> HTTP 422  同上
口令 11 位 (11) -> HTTP 422  同上
口令 12 位 (13) -> HTTP 201  ✅
```

**判据**：口令 **≥ 12 位**时开账号是好的（实测 201）；
口令 **< 12 位**时必然 422，而**页面上不会告诉你「口令太短」**。
所以「管理员无法创建账户」的准确表述是：**只要口令不够 12 位就永远建不出来，且界面不说为什么。**

> ⚠️ 这不是「12 位」这个策略本身有问题 —— 它是有意为之的安全修复（`issues/003` 的 P1-3，
> 与 NIST SP 800-63B 口径一致）。**坏的是策略只落在后端，前端既不拦也不说。**

## 影响

* **挡住开工**：这是「管理员开账号」这条需求的**唯一**入口，口令随便填一个（`pass1234`、
  `Abc12345`、姓名+生日…）就 100% 失败。演示时表现为「管理台坏了」。
* **误导判断**：422 的 `detail.fields` 里**带着**能自救的信息
  （`loc=[body,password]` + `msg="String should have at least 12 characters"`），
  但前端 `describe()` 只渲染 `exc.message`（固定的「请求参数不合规」），
  把字段级原因整个丢掉。管理员看到的是「参数不合规」——他刚填的几个字段全是「参数」，
  不知道该改哪一个。
* **加倍成本**：`create()` 在**请求失败后仍然清空表单**，于是「改一个字再试一次」
  变成「全部重填再失败一次」。三次之后没人会继续试。

## 根因

三层，从表到里：

### ① 前端完全没有口令策略这个概念（主因）

`grep -r "MIN_PASSWORD" web/src` → **一处都没有**。管理台的口令框是裸 `type="password"`：

```text
表单属性实测：{ label: "初始口令", minLength: -1, required: false, type: "password" }
```

i.e. 没有 `minLength`、没有 `required`、没有任何文案说明规则。
前端的 `create()` 只检查「非空」：

```ts
if (!draft.identifier.trim() || !draft.password) { … "登录标识与初始口令都要填。" }
```

于是「能不能通过」这件事**只有后端知道**，而它知道的方式是**拒绝**。

### ② 错误信封里的 `detail.fields` 从来没被渲染

`web/src/lib/api.ts` 解析 422 时**确实**把 `detail` 收进了 `ApiError`（这层是对的，见其 `toApiError`），
但 `admin/page.tsx::describe()` 对 422 走的是兜底分支：

```ts
return `${exc.message}（HTTP ${exc.status}…）`;   // 只用了 message，detail.fields 丢掉了
```

对比：同一个函数对 404 与 401 都写了**指向具体修法**的话 —— 唯独 422 没有，
而 422 恰恰是唯一一个「字段级原因就在手里」的错误。

### ③ 失败后清空表单（放大伤害）

```ts
void run( … );                                   // 异步，不 await
setDraft({ ...draft, identifier: "", password: "", subjectId: "" });   // 无条件清空
```

`run()` 的 `catch` 只写 `notice`，**不表达成功/失败**；`create()` 也不看结果就清空。
所以失败时草稿已经没了。

### ④ 成功提示被自己清掉（修 ①③ 时用同一脚本一并发现）

`load()` 开头是 `setNotice(null)`，而 `run()` 原先的顺序是
「**先**设成功提示、**再** `await load()`」：

```ts
setNotice({ kind: "ok", text: okText });   // ← 被下面这行的 setNotice(null) 清掉
await load();
```

于是**开号成功时页面一片空白**（实测：HTTP 201、新账号确实在列表里、通知区什么都没有）。
「成功」与「什么都没发生」在界面上长得一样，管理员只能去列表里数行数。
失败路径没有这个问题（失败时根本不走 `load()`）。

### 为什么测试没发现

* `web/tests/` 是**纯离线**用例（不渲染 React、不发请求），`admin/page.tsx` 只被
  `hook-wiring.test.ts` 用**源码正则**检查「有没有用 `table.data`」这类形状 ——
  组件里「口令框没有 minLength」这种**缺失**不会被任何断言命中。
* 后端用例（`tests/api/test_auth_accounts.py`）自己填的是
  `"temp-pw-123456"` / `"authz-probe-pw-1"` 这类**够长**的口令，
  于是「前端会发一个太短的口令」这条路径在后端用例里根本不存在。
* 结论：这是一个**跨层契约**（后端口令策略 ↔ 前端表单），
  两边的用例各自都绿，中间那条缝没人管。

## 修复

1. **把口令策略的「唯一真相」在前端显式引用一处**：
   新增 `web/src/lib/password.ts`，声明 `MIN_PASSWORD_LENGTH = 12`（与
   `app/security/accounts.py::MIN_PASSWORD_LENGTH` 同值），并提供纯函数
   `passwordProblem(password, identifier)` 返回**能照做**的一句话或 `null`；
2. **管理台两处口令入口都接上**：
   * 「开账号」：发送**之前**先校验，不合格就直接给出原因（不再白跑一次 422）；
     口令框加 `minLength` 与说明文案，让规则**在填的时候就看得见**；
   * 「重置口令」：`window.prompt` 拿到的新口令同样先校验；
3. **422 的字段级原因渲染出来**：`describe()` 增加 `validation_error` 分支，
   把 `detail.fields` 的 `loc`/`msg` 翻成中文定位（`password` → 「口令」…）；
4. **失败不清空表单**：`create()` 改成先 `await`，只有成功才清空；
5. **成功提示移到 `load()` 之后**（修 ④）：否则会被 `load()` 开头的 `setNotice(null)` 清掉；
6. **补防漂移用例**（`web/tests/password.test.ts`，10 条）：
   * 前端常量必须**等于** `app/security/accounts.py` 里的同一个数
     （直接读 Python 源码取值，而不是两处各写一个 12）；
   * `passwordProblem()` 的边界（11 位拒 / 12 位过 / 全空白拒 / 等于标识拒 / 不回显口令）；
   * `fieldProblemText()` 把 422 字段错误翻成「改哪里」，认不出来时原样透传；
   * 源码级断言：管理台口令框必须带 `minLength`、规则要写在界面上、
     口令校验必须排在请求**之前**、重置走同一套判据、
     **失败不得清空表单**、**成功提示必须在 `load()` 之后**、422 必须走字段级分支。

### 刻意**不做**的两件事

* **不把后端的完整弱口令黑名单抄到前端**：那是一个会持续增长的集合，
  抄一份必然漂移（漂移的后果是「前端说没问题、后端拒」，比没有更糟）。
  前端只镜像**长度**这个会静默变化的数字 + 两条一眼可判的规则（全空白、与标识相同），
  **后端始终是权威**；前端放行而仍被后端拒时，第 ③ 条会把原因说清楚。
* **不在登录页加长度限制**：登录只负责「试一下这组凭据」，口令多长与它无关
  （加了会把「老账号的短口令」挡在门外）。

## 验证

见 [`docs/实测报告-2026-10-16.md`](../docs/实测报告-2026-10-16.md)；

```text
修复前（无头 Chrome 真点页面）：password="pass1234" -> 422，页面「请求参数不合规」，表单已清空
修复后（同一脚本同一路径）：     password="pass1234" -> 前端**不发请求**，
                                直接提示「口令至少 12 位（当前 8 位）…」，表单内容保留
修复后（同一脚本同一路径）：     password="passphrase-12" -> 201，新账号出现在列表里
```
