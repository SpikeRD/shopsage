# 003 · 实测发现的 2 个 P0 安全问题与 3 个 P1 问题（已修复）

* **状态**：已修复（2026-10-15）
* **发现于**：2026-10-15，项目实测（真后端 + 真依赖栈 + 真 LLM，走真实 HTTP/WS 链路）
* **归属模块**：见下表（跨 4 个模块）
* **完整报告**：[`docs/实测报告-2026-10-15.md`](../docs/实测报告-2026-10-15.md)
* **回归口径**（修复后实测）：
  * 后端全量 `1781 passed, 1 failed, 50 skipped`（164s）
  * 前端 `159 passed`（修复前 150，新增 9 条）
  * 前端 `npm run typecheck` 退出码 0
  * 那 1 failed 是**既有的非幂等用例**（与本次五个问题无关、修复前后都红），见文末「未处理」

> ⚠️ **实测期间的两次假警报（记下来免得后人重走）**
>
> 1. **一次全量跑出现 `tests/rag/...::test_keyword_search_finds_exact_clause_number` 失败** ——
>    单独跑、按目录跑都通过。真因：我当时还留着一个**未被回收的后端进程**占着连接池与库锁。
> 2. **随后一次全量跑在 18% 处挂死**（卡在 `tests/cache/test_cache_db.py` 第一条）。
>    真因：更早几次被我 `kill` 掉的测试进程留下了 **`idle in transaction` 会话**，
>    它们持有 `test_cache_corpus_versions` 的行锁，后续每一次运行都在 `create table if not exists`
>    上无限等待。清理方式（取证 + 修复）：
>
>    ```sql
>    -- 取证：谁在挡路
>    select pid, state, wait_event_type, wait_event, now()-xact_start, left(query,80)
>    from pg_stat_activity
>    where datname = current_database() and pid <> pg_backend_pid();
>
>    -- 处置：只杀「空闲在事务里」或「正等 test_cache 锁」的会话
>    select pg_terminate_backend(pid) from pg_stat_activity
>    where datname = current_database() and pid <> pg_backend_pid()
>      and (state like 'idle in transaction%'
>           or (wait_event_type = 'Lock' and query ilike '%test_cache%'));
>
>    drop table if exists test_cache_corpus_versions cascade;
>    drop table if exists test_cache_semantic_entries cascade;
>    ```
>
> **两次都不是代码缺陷**（第 2 条在「原始代码 + 原始测试」上同样复现）。
> 判据：**单独跑通过、全量跑失败/挂死 → 先查库里的僵尸会话与残留进程，别先怀疑自己的改动。**

---

## 总览

| # | 问题 | 严重度 | 归属 | 状态 |
|---|---|---|---|---|
| P0-1 | 登录接口限流**完全没生效**（可无限爆破） | P0 安全 | `app/api/middleware.py` | 已修复 |
| P0-2 | 对客 SQL 行级过滤可绕过 → **跨租户读别人订单** | P0 安全 | `app/agents/agent_llm.py` | 已修复 |
| P1-1 | 知识库版本号不推进 → 空库时缓存的答案**永久**被返回 | P1 正确性 | `app/cache/corpus.py` | 已修复 |
| P1-2 | 前端**从不使用刷新令牌** → 30 分钟后硬性掉线 | P1 功能 | `web/src/lib/session.ts` 等 | 已修复 |
| P1-3 | 管理员可把口令设成 **1 个字符** | P1 安全 | `app/security/accounts.py`、`app/api/schemas.py` | 已修复 |

---

## P0-1 · 登录限流完全没生效

### 现象

配置了全项目最严的额度（`ratelimit.py`：`auth.login = 10 次 / 300 秒`），
实测**连续 40 次错误登录全是 401，一次 429 都没有**：

```text
POST /api/auth/login x40 (configured limit 10/300s): {401: 40}
POST /api/auth/refresh x40 (configured limit 60/300s): {401: 40}
```

对照 `POST /api/chat`（登记过的）是**正常生效**的：`Counter({401: 30, 429: 6})`。

### 影响

登录是**唯一**能用「猜」换到令牌的入口。不限流 = 密码爆破没有次数上限
（只受 Argon2id 计算耗时这一个天然限速）。而 `scope.py` 的注释与 `.env.example`
都明确声称「不限流等于给暴力破解无限次尝试」—— **文档与实现不符**。

### 根因

限流要生效需要**三处登记**（`middleware.py` 自己写了这条纪律）：

| # | 位置 | 作用 | 修复前 |
|---|---|---|---|
| ① | `scope.SCOPE_BY_ROUTE` | 用哪个 scope 名 | ✅ 有 |
| ② | `ratelimit.DEFAULT_RULES` | 默认额度 | ✅ 有 |
| ③ | `middleware.LIMITED_SCOPES` | **中间件要不要限** | ❌ **缺** |

中间件只在 `(method, path) in LIMITED_SCOPES` 时才调限流器，
所以登录/刷新**根本没进限流分支** —— ① ② 配得再对也不生效，且**没有任何报错**。

### 为什么测试没发现

既有用例只断言了**单向包含**（`test_api_contracts.py`）：

```python
assert LIMITED_SCOPES <= set(SCOPE_BY_ROUTE)   # 限流的路径必须有 scope
```

这条拦得住「限流了但没 scope」，**拦不住反方向**（有 scope、有额度、却没登记限流）。

### 修复

1. `app/api/middleware.py`：把 `("POST", "/api/auth/login")` 与
   `("POST", "/api/auth/refresh")` 加进 `LIMITED_SCOPES`；
2. **补反向断言**（`tests/api/test_api_contracts.py`）：
   `test_every_scoped_route_is_actually_enforced` —— 每条有 scope 的路由都必须在
   `LIMITED_SCOPES` 里，把三处钉在一起；
3. **补行为用例**（`tests/api/test_rate_limit.py::test_login_is_rate_limited`）：
   走真实 HTTP 路径，断言第 3 次起返回 429。

### 验证

```text
修复前： POST /api/auth/login x40 -> {401: 40}
修复后： POST /api/auth/login x14 -> {401: 10, 429: 4}   ✅ 额度生效
```

新用例在「把登记删掉」时会红（实测）：

```text
FAILED test_every_scoped_route_is_actually_enforced
FAILED test_auth_endpoints_are_rate_limited
FAILED test_login_is_rate_limited   ->  assert [401, 401] == [429, 429]
```

---

## P0-2 · 对客 SQL 行级过滤可绕过（跨租户读数据）

### 现象

调用者是 `user_id=2`，下面这条 SQL 的**两道检查全部放行**：

```sql
SELECT order_id, user_id FROM orders WHERE user_id = :uid OR user_id = 3 LIMIT 10
```

```text
1) validate_sql -> ALLOWED
2) customer_row_filter_error -> PASS (no error)
```

其他被放行的绕过形态：

| SQL | 修复前 |
|---|---|
| `WHERE user_id = :uid OR user_id = 3` | ❌ 放行 |
| `WHERE user_id = :uid AND user_id = 3` | ❌ 放行 |
| `WHERE user_id = :uid AND user_id IN (1,2,3)` | ❌ 放行 |
| `WHERE user_id = :evil`（占位符名不对） | ❌ 放行 |
| `... UNION SELECT ... WHERE user_id = 3` | ❌ 放行（任一分支合规即可） |
| `SELECT (SELECT count(*) FROM orders WHERE user_id=3) FROM orders WHERE user_id = :uid` | ❌ 放行 |

### 影响

对客路径的「只能查自己」是 D1 的核心，也是「对客/对内两条业务线」隔离的基础。
越权成功时**图有降级兜底、不报错**，答案看起来完全正常 —— 最危险的一类失败。

### 根因

旧实现只查「**形状**」：WHERE 里存在**任意一个** `user_id = 占位符` 就放行
（`_equalities()` 遍历所有 `EQ`，命中一个即 `return None`）。它**从不检查语义**
（这个条件是否真的把结果限定到了本人），于是：

* `A OR B` 里只要 A 合规，B 写死越权值也放行；
* `UNION` 只要任一分支合规就放行；
* 占位符名不是 `uid` 时，`data.py::_bind_customer_id` 不会覆写它
  （它只在 SQL 含 `:uid` 时才绑身份），模型可自带 `{'evil': 3}`。

`validate_sql` 帮不上忙：它明确「不知道你是谁」，只要求 WHERE 存在且不恒真。

> 📌 **已做对的部分**：`_bind_customer_id` 强制覆写绑定值，防住了「模型谎报身份」。
> 它防不住的是「SQL 文本里**额外写死**一个越权条件」—— 那部分不走绑定参数。

### 修复

`app/agents/agent_llm.py::customer_row_filter_error` 从「查形状」升级为「**查语义**」：

1. **顶层不许是集合运算**（`UNION`/`INTERSECT`/`EXCEPT`）：一个 WHERE 管不住所有分支；
2. 把 WHERE 摊平成**合取项**（AND 链）后，必须有**恰好一个** `user_id` 比较；
3. 那一个必须是 `user_id = :uid`（等值 + 占位符名**精确等于** `uid`）；
4. **其它任何**合取项都不许再出现 `user_id`；
5. 逐个检查**所有查表的 SELECT**（含子查询），每个都必须自己限定到本人。

⚠️ **刻意不采用**「WHERE 里出现 OR 就拒」：那会误伤
`WHERE user_id = :uid AND (status='a' OR status='b')` 这种合法写法。
真正危险的是**顶层 OR**，所以按「合取项」而非「出现 OR」判定。

### 验证

19 条向量全部符合预期（7 条合法放行 + 12 条越权拒绝），
并补了参数化回归用例（`tests/test_agents_state.py`）：

```text
test_customer_row_filter_rejects_cross_tenant_bypass   # 9 条越权形态
test_customer_row_filter_still_accepts_legitimate_queries  # 7 条合法形态（防过度收紧）
```

---

## P1-1 · 知识库版本号不推进 → 旧答案永久返回

### 现象

1. 迁移后知识库是空的（`rag_documents=0`、`rag_chunks=0`）；
2. 此时问「退货政策是怎么规定的？」→ 走 RAG、**答案无任何出处**，并被写进语义缓存；
3. 灌完知识库（8 文档 / 61 片）后问**同一句话**，仍返回那条无出处的旧答案：

```text
第一次（空库）：citations=0   ← 被缓存
灌库后连问 4 次：citations=0   ← 全部命中旧缓存
```

缓存表里这条记录 `hit_count=10`：

```text
1 | customer:2 | 退货政策是怎么规定的？ | cits=(空) | rev=1 | active=True | hits=10
```

### 影响

「推了新文档但缓存不失效」→ 用户长期拿到旧答案/无出处答案。
症状**极难归因**：接口 200、`/health` 全绿、日志无异常，只是答案悄悄不对。

### 根因

`app/cache/corpus.py::bump` 有一句「`version` 与当前版本相同就不 bump」，
本意是「同一份文档重复入库不算变化」。但它比较的两个量**量纲不同**：

* `version` 是调用方传进来的 `rag_documents.version`（**每份文档各自的**版本号）；
* `current()` 是全局知识库版本（`cache_corpus_versions.corpus_revision`）。

于是只要某份文档的版本号**恰好等于**当前全局版本号，真实变更就**不会推进版本**。
首次全量灌库尤其容易撞上（新文档 version 普遍是 1，初始 `corpus_revision` 也是 1）。
实测（`corpus_revision=1`）：

```text
bump(version=1, force=False) -> 1    # 新文档入库，但版本没动 → 缓存不失效
bump(version=2, force=False) -> 2    # 只有碰巧不等时才推进
```

### 修复

**无条件推进版本**（`version` 参数保留**只用于日志**）。
理由：「重复入库不要白清缓存」这件事**已经由上游闸门负责** ——
`app/rag/ingest.py` 只在 `report.doc_changed` 为真时才调 `on_change`
（`_is_noop()` 已经把「什么都没变」挡在外面）。所以走到 `bump` 就意味着确实有变更。

改动：`PostgresRevisionStore.bump`、`MemoryRevisionStore.bump` 去掉短路；
`RevisionStore` 协议与 `bump_after_ingest` 去掉 `force` 参数。

### 验证

```text
修复前： bump(version=当前版本) -> 不变
修复后： start=4, bump(version=4) -> 5，advanced=True   ✅
```

更新了两条**把旧语义钉住**的用例（它们正是这个 bug 的「守门人」，必须一起改）：

* `tests/cache/test_cache_semantic.py::test_revision_always_advances_on_ingest`
* `tests/cache/test_cache_gateway.py::test_make_ingest_callback_bumps_the_revision`

---

## P1-2 · 前端从不使用刷新令牌

### 现象

`web/src/lib/session.ts` 完整实现了 `ensureFreshToken`（含提前 60s 续期、失败清干净），
`api.ts` 也实现了 `postRefresh`，但**全项目没有任何地方调用它们**：

```text
grep "ensureFreshToken" web/src  → 只有定义处
grep "postRefresh"      web/src  → 只有定义处
```

唯一引用出现在**测试**里（`web/tests/session.test.ts`）——
即「功能被测试覆盖得很完整，但从未接线到应用」。

### 影响

登录时**确实存了**刷新令牌（14 天有效），但永远不用；访问令牌 30 分钟到期后
用户被踢回登录页，与文档承诺的「无感续期」完全不符。
更糟的是症状是「莫名其妙掉线」—— 因为刷新令牌明明还在 localStorage 里。

### 根因

实现层与调用层脱节：函数写好了、单测过了，但**没有调用点**。
这类问题单测**测不出来**（单测直接调那个函数，当然能过）。

### 修复

1. `session.ts` 新增 `withFreshToken(entry, storage, run)`：
   **请求前** `ensureFreshToken`（预防）+ **被 401 拒后** `forceRefreshToken` 重试一次（补救）；
2. **并发去重**（`inFlightRefresh`）：同一入口的并发刷新共享一个 Promise ——
   这是**正确性要求**，因为刷新令牌在服务端是「换一对新的」，
   多个请求同时刷新会互相作废（症状：页面一打开就莫名跳登录页）；
3. 接入**全部**受保护请求路径：
   * `app/(app)/internal/page.tsx`（`postChat`）
   * `app/(app)/admin/page.tsx`（`getAccounts` / `postAccount` / `patchAccount` / `postPasswordReset`）
   * `components/SideNav.tsx`（`getMe`）
   * `components/useChatStream.ts`（WS 建连前续期 + `postApprove`）
4. 新增 `SessionExpiredError`：登录态彻底不可用时明确跳登录页，而不是反复失败。

⚠️ WS 也要续期：`openChatStream` 把令牌写进握手子协议，而**握手只发生一次** ——
用即将过期的令牌握手会立刻收到 4401。

### 验证

新增 5 条运行期用例（`web/tests/session.test.ts`）+ 3 条**源码级**用例
（`web/tests/hook-wiring.test.ts`）：

```text
✔ ★401 时自动刷新令牌并重试一次
✔ ★只重试一次：第二次仍 401 就抛错（不进入死循环）
✔ 非 401 的错误不触发刷新
✔ ★并发刷新只发一次请求
✔ 没有登录态时抛 SessionExpiredError
✔ ★withFreshToken 必须在真实调用点被使用
✔ ★建连前必须先续期令牌
✔ ★401 重试必须有次数上限
```

源码级用例实测能抓到漏项（把 SideNav 的调用改回 `getMe(active.token)` → 红）：

```text
AssertionError: ../src/components/SideNav.tsx 没有接上令牌续期：该文件的请求会在
令牌过期后直接 401 —— 用户被踢回登录页，而刷新令牌还在本地（P1-2 的原始故障）
```

> ⚠️ 该用例第一版判据写成了 `/withFreshToken/`（不带 `(`），结果被 **import 那一行**
> 满足 —— 「删掉调用、留着 import」这种最常见的漏项照样绿。已收紧为 `/(...)\s*\(/`。

---

## P1-3 · 管理员可把口令设成 1 个字符

### 现象

```text
POST /api/admin/accounts {"identifier":"probe_x","password":"x", ...}
  -> 201 Created        ← 1 个字符的口令被接受
POST /api/auth/login {"identifier":"probe_x","password":"x"}
  -> 200 拿到令牌       ← 并且真的能登录
```

空串被拒（`min_length=1` 拦下），但 `"x"`、`"a"`、`"  "` 这类都通过。

### 影响

管理员误设/偷懒设弱口令 = 给该账号开后门。
与 P0-1 叠加（登录限流当时也失效）＝ 弱口令 + 无限次尝试，可直接爆破。
与项目自身「表白名单、fail-fast、默认拒绝」的安全纪律不一致。

### 根因

`app/api/schemas.py` 里口令只有 `min_length=1`：

```python
password: str = Field(min_length=1, max_length=200, ...)
```

项目对其它配置项都 fail-fast 且写清理由（限流表达式、`API_CORS_ORIGINS` 的 `*`），
唯独口令强度没有任何约束。

### 修复

1. **唯一真相**放业务层 `app/security/accounts.py::validate_password_strength`：
   * 长度 ≥ `MIN_PASSWORD_LENGTH = 12`；
   * 拒绝常见弱口令（黑名单）；
   * 拒绝「与登录标识相同」；
   * 拒绝全空白；
   * **报错不回显口令本身**；
2. **三个写入口都接**：`create_account`、`reset_password`、`change_own_password`
   —— 只拦建号会导致「开号时拦住、重置时放回去」；
3. HTTP 层 `schemas.py` 引用同一常量做 `min_length`（更早失败 + 进 OpenAPI 文档），
   **不重写数字**；
4. 长度取 12 而非复杂度规则（与 NIST SP 800-63B 口径一致：优先长度）。

### 验证

```text
修复前： password='x' -> 201 Created（且能登录）
修复后： password='x'           -> 422
        password='short'       -> 422
        password='12345678901' -> 422（差一位）
        password='a-good-enough-passphrase' -> 201
```

新增用例：`tests/security/test_accounts.py`（纯逻辑 + 真库路径）、
`tests/api/test_auth_accounts.py`（HTTP 层 + 重置路径）。

---

## 附：本次修复改动清单

| 文件 | 改动 |
|---|---|
| `app/api/middleware.py` | `LIMITED_SCOPES` 补登录/刷新两条 |
| `app/agents/agent_llm.py` | `customer_row_filter_error` 语义化重写 + 4 个辅助函数 |
| `app/cache/corpus.py` | `bump` 无条件推进；去掉 `force` 参数 |
| `app/security/accounts.py` | 新增 `MIN_PASSWORD_LENGTH` / `validate_password_strength`，三处接入 |
| `app/api/schemas.py` | 口令 `min_length` 引用同一常量 |
| `web/src/lib/session.ts` | 新增 `withFreshToken` / `forceRefreshToken` / 并发去重 / `SessionExpiredError` |
| `web/src/components/useChatStream.ts` | 建连前续期 + `postApprove` 走 `withFreshToken` |
| `web/src/components/SideNav.tsx` | `getMe` 走 `withFreshToken` |
| `web/src/app/(app)/internal/page.tsx` | `postChat` 走 `withFreshToken` |
| `web/src/app/(app)/admin/page.tsx` | 4 个调用走 `withFreshToken` |
| `tests/api/test_api_contracts.py` | 反向限流断言 2 条 |
| `tests/api/test_rate_limit.py` | 登录 429 行为用例 |
| `tests/test_agents_state.py` | 跨租户绕过回归 2 组（参数化） |
| `tests/cache/test_cache_semantic.py`、`test_cache_gateway.py` | 版本推进语义更新 |
| `tests/security/test_accounts.py`、`tests/api/test_auth_accounts.py` | 口令策略用例 + 既有口令加长 |
| `web/tests/session.test.ts`、`web/tests/hook-wiring.test.ts` | 续期运行期 + 源码级用例 |

---

## 未处理（**刻意不在本次范围**）

### 1. `tests/api/test_cors.py::test_prod_defaults_to_no_cross_origin` 仍红

**这不是本次五个问题之一，也没有被本次改动影响**（修复前后都是这 1 条失败）。

* **现象**：本地跑全量必红、CI（无 `.env`）必绿。
* **根因（已实测定位）**：`import litellm` 会调用 `load_dotenv()`，
  把整个 `.env` 注入 `os.environ`。于是用例里的 `monkeypatch.delenv("API_CORS_ORIGINS")`
  **删不掉 `.env` 提供的值**，而本仓 `.env` 恰好定义了它。
  ⚠️ **配置优先级本身是正确的**（进程 env > `.env` > 默认值，三种情形都实测过）——
  最初判成「优先级倒置」是**误判**，已在完整报告里更正。
* **为什么没顺手修**：它属于「测试隔离」范畴（需要给配置类用例加 hermetic fixture），
  与本次五个安全/正确性问题不是一类，单独处理更清楚。
* **建议修法**：用例显式构造「没有 `api_cors_origins` 字段」的 settings 对象，
  或在 `tests/conftest.py` 加 autouse fixture 清掉 `.env` 泄漏进 `os.environ` 的 `API_*` 键。

### 2. `issues/001` 的 JSON 非确定性

本次实测 **3/3 出图**、`finish_reason='stop'`，未复现原先的高失败率。
建议重测后更新该台账状态（不在本次范围）。
