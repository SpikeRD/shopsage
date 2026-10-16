# 003 · 退款申请/审批闭环 + 入口权限贯通（管理员三入口、客户订单页、员工退款台）

| 项 | 值 |
|---|---|
| 状态 | `已完成`（T1–T17 全部打勾；§4 验收逐条实测，§7.B4 记录了开发期抓到的 4 个真实缺陷） |
| 提出日期 | 2026-10-16 |
| 定稿日期 | 2026-10-16 |
| 影响模块 | `app/security/`（rbac / accounts / policy）、`app/api/`（chat / 新增 orders+refunds / schemas / scope / middleware / ratelimit / config）、`app/graph/`（config / routing / nodes / builder / state）、`app/agents/`（router / synthesis / 新增 refund / state）、`app/tools/refund.py`、`app/db/`（models / queries）、`alembic/`、`web/`、`scripts/seed.py`、`tests/`、`docs/` |
| 关联 | 承接 [变更 001](001-account-login-and-admin-console.md)、[变更 002](002-light-ui-sidebar-and-autoprobe.md)；涉及 `app/tools/refund.py` 的 SoD 设计（模块 docstring 第 2 条） |

> **本文档是本次变更的唯一权威描述。** 在状态变为 `已定稿` 之前，不动任何业务代码。
> §9 记录了本次澄清的 **26 条决策**与 **2 个待你裁决的点**（D1/D2）。

---

## 1. 变更动机（为什么改）

### 1.1 现状（只读实测，逐条给了证据位置）

| # | 现状事实 | 证据 |
|---|---|---|
| S1 | `admin` 角色在 RBAC 里**已经有全部 6 个权限点**（含 `read:all_orders`），后端并不缺权限 | `app/security/rbac.py:66-75` |
| S2 | 但登录只把令牌写进**一个**入口钥匙（`payload.entry` 决定哪一个），而 `/` 只读 `shopsage.token.customer`、`/internal` 只读 `.internal` | `web/src/lib/session.ts:75-90`、`web/src/components/useChatStream.ts:351-358`、`web/src/lib/auth.ts:310-337` |
| S3 | 于是管理员登录后进 `/` 或 `/internal`，页面显示「**需要先登录**」——这正是「能看到入口但没有权限操作」的现场 | `web/src/app/(app)/page.tsx:37-48`、`web/src/app/(app)/internal/page.tsx:53-68` |
| S4 | 对客路径默认**开**人工审批闸门（`GATED_ROLES={customer}`），客户自己就能点「批准并继续」 | `app/graph/config.py:68`、`app/graph/builder.py:106-116` |
| S5 | 图里**根本没有退款写节点**：`ROUTE_NODES["ticket"] → synthesis`，被批准之后也只是「如实作答」，不落任何库 | `app/graph/state.py:178-188` |
| S6 | `customer` 角色刻意**没有** `write:refund`（职责分离），只有 `customer_service`/`internal_employee`/`manager`/`admin` 有 | `app/security/rbac.py:44-76`、`app/tools/refund.py:191-197` |
| S7 | `refunds` 表**没有**审批留痕字段（无 decided_by / decided_at / decision_note） | `app/db/models.py:167-196` |
| S8 | 全仓**没有任何退款审批接口**：已注册的路由只有 `/api/admin/*`、`/api/chat*`、`/api/tasks*`、`/api/auth/*` | `grep '@router\.' app/api` → 15 处，无 refund |
| S9 | 客户侧**没有订单查询页**，`/orders` 不存在；`app/db/queries.py` 已有现成的对客查询（带行级过滤） | `app/db/queries.py:68-138`；`web/src/app/(app)/` 下无 `orders/` |
| S10 | `customer_service` **没有** `read:all_orders`，而入口判据是「有 `read:all_orders` 才进对内」 | `app/security/rbac.py:48`、`app/security/accounts.py:245-251` |
| S11 | `DEMO_ACCOUNTS` 只有 4 个演示账号（admin / 两个客户 / 一个 internal_employee），**没有** `customer_service` | `scripts/seed.py:392-402` |
| S12 | `masking.FIELD_RULES` 里**没有** `amount` / `total_amount`；而 `payment_method` 登记为「财务级(3)」→ 客户（上限 1）看不到支付方式，金额却会**原样输出** | `app/security/masking.py:127-137`、`app/security/policy.py:96-115` |
| S13 | `guardrail.INTERNAL_TERMS` 含 `refund_id` 与 `refunds`，对客回答里出现这些**英文字段名**会被输出侧护栏拦下 | `app/agents/guardrail.py:73-104` |
| S14 | 基线实测：后端 `1780 passed, 50 skipped, 1 failed, 6 errors`；前端 `171 passed`（详见 §7.B1/B2） | 见 §7 |

### 1.2 问题（现状带来的具体损失）

- **P1（S1–S3）**：需求 1 的现象**不是权限缺失，而是登录态写入不完整**。改 RBAC 是改错地方（admin 本来就有全权），真正要改的是「登录把令牌写到哪几把钥匙」。现状的直接后果：管理员必须用三个账号各登一次（或去 `/login` 反复切），三入口实际上**不可同时使用**。
- **P2（S4+S5）**：这是**最严重的一条**。对客路径上客户能**自己批准自己的敏感操作**（自审批），而批准之后由于没有写节点，「批准」既没有产生退款、也没有留下任何记录 —— 用户点了一次「批准并继续」，系统回一句话术，**资金动作既没发生也没留痕**。这在演示中会直接暴露为「退款点不动 / 批了没反应」。
- **P3（S6+S8）**：客户**没有**任何自助退款通道：图里没有写节点、接口里没有退款端点、页面上没有入口。而 `create_refund_request` 的能力（行锁 + 剩余额度 + 幂等 + 审计）**已经实现并验过**，只是没有任何对客出口。
- **P4（S7）**：退款审批没有留痕字段，即便今天把审批做出来，也回答不了「谁批的、什么时候批的、为什么」——而这是资金动作的**基本审计要求**。
- **P5（S10+S11）**：即便给了 `customer_service` 审批权，他也**进不去**对内入口（判据不含审批能力），且演示账号里根本没有客服角色 —— 这条路径**无法端到端验收**。
- **P6（S9）**：客户查不到自己的订单，也就无从判断「哪一单可以退、还剩多少可退额度」。

### 1.3 不改的代价

- 需求 2 的「明显有问题」会一直存在：**客户可以批准自己的退款**，而系统把「批过了」如实渲染给用户，制造「已经办好了」的错觉。
- 需求 1 的现状会被反复误诊为「权限没配」，导致有人真的去给 admin 加权限（它已经全有了），**改动落在错误的位置**且不解决问题。
- 需求 3 完全没有落点：员工没有任何界面能看到「有哪些退款单在等我批」。
- 退款这条业务线**永远停在「能力已实现、没有任何入口」**的状态：`app/tools/refund.py` 的 346 行与它的一批用例在业务上不可达。

---

## 2. 变更点（改哪些地方）

> 类型只有这几种：**行为** / **接口** / **数据模型** / **配置** / **文案** / **UI**。

### 2.1 做（本次范围内）

#### A. 权限模型（需求 1/3 的前置）

| # | 类型 | 位置 | 原来 | 改成 | 理由 |
|---|---|---|---|---|---|
| 2.1.1 | 行为 | `app/security/rbac.py` | 6 个权限点 | **8 个**：新增 `request:refund`、`approve:refund` | 决策 2/3/24：客户「申请」与员工「审批」是两个不同动作，各自成点 |
| 2.1.2 | 行为 | `app/security/rbac.py` | `customer` 无退款权限 | `customer` += `request:refund`；**仍然没有** `write:refund` | 决策 2：保留 SoD（`write:refund` 语义不变，客户拿不到） |
| 2.1.3 | 行为 | `app/security/rbac.py` | 审批权不存在 | `customer_service` / `internal_employee` / `manager` / `finance_auditor` / `admin` 各 += `approve:refund` | 决策 3 |
| 2.1.4 | 行为 | `app/security/accounts.py:245-251` | `entry_for_role` 只看 `read:all_orders` | 改为「有 `read:all_orders` **或** `approve:refund` → 对内」 | 决策 15：否则客服进不去审批页 |
| 2.1.5 | 行为 | `web/src/lib/auth.ts` | `canUseInternal` = `read:all_orders` | **拆成两个谓词**：`canUseInternal`（保持原义＝能不能查全站）与 `canUseInternalEntry`（＝能不能进对内入口，含审批能力） | 决策 15 的配套：否则客服会看到**可用的**查询框而后端拒绝（§4.9 的验收点） |

#### B. 入口与登录态（需求 1）

| # | 类型 | 位置 | 原来 | 改成 | 理由 |
|---|---|---|---|---|---|
| 2.1.6 | 行为 | `web/src/lib/session.ts` | `writeSessionPair` 只写 **1** 个入口钥匙 | 写进**该角色可进入的全部入口**钥匙（同一对令牌） | 决策 1（修 P1 的真正落点） |
| 2.1.7 | 行为 | `web/src/lib/session.ts` / `SideNav.tsx` | 退出只清当前入口 | 退出清掉**本账号写过的所有入口**钥匙（按令牌 `aid` 判定归属） | 决策 14：否则退出后 `/` 与 `/internal` 仍是可用会话 |
| 2.1.8 | UI | `web/src/components/SideNav.tsx` | 只列三个入口 | 入口之下再列**子页**：对客→订单查询；对内→退款审批 | 决策 6 |

#### C. 对客退款通道（需求 2）

| # | 类型 | 位置 | 原来 | 改成 | 理由 |
|---|---|---|---|---|---|
| 2.1.9 | 行为 | `app/graph/config.py` | `GATED_ROLES={customer}`、`SENSITIVE_ROUTES=("ticket",)` | **`GATED_ROLES=frozenset()`、`SENSITIVE_ROUTES=()`** | 决策 5/9：对客不再有任何 interrupt（客户不再能自审批） |
| 2.1.10 | 接口 | `app/api/chat.py` | `POST /chat/approve` 存在 | **删除**该路由；同步删 `schemas.ApprovalDecision`、`scope` 的 `chat.approve`、`middleware.LIMITED_SCOPES` 项、`ratelimit.DEFAULT_RULES` 项、`config.rate_limit_specs` 映射、`.env.example` 的 `RATE_LIMIT_CHAT_APPROVE` | 决策 21：闸门永不触发 → 该接口恒 409，留着是死能力 |
| 2.1.11 | 行为 | `app/agents/state.py`、`app/graph/state.py` | `ROUTES` 5 项 | += `"refund"`；`ROUTE_NODES["refund"] = NODE_REFUND_REQUEST` | 决策 20 |
| 2.1.12 | 行为 | 新增 `app/agents/refund.py` | 不存在 | 新增 `refund_node`：自动挑可退订单 → 落库为 `requested`；多笔则追问；无可退则如实说明 | 决策 5/10 |
| 2.1.13 | 接口 | 新增 `app/api/orders.py` | 不存在 | `GET /api/orders`、`GET /api/orders/{order_id}`（**强制 `WHERE user_id = 本人`**） | 决策 6/13 |
| 2.1.14 | 接口 | 新增 `app/api/refunds.py` | 不存在 | `POST /api/refunds`（客户申请，要 `request:refund`） | 决策 2/8 |
| 2.1.15 | 行为 | `app/tools/refund.py` | `create_refund_request` 只认 `write:refund` | 抽出共享写核心；新增 `request_refund`（认 `request:refund`）与只读 `remaining_refundable` | 决策 26：行锁/额度/幂等/审计**复用**，不写第二份 |
| 2.1.16 | UI | 新增 `web/src/app/(app)/orders/page.tsx` | 不存在 | 订单列表 + 明细 + 该单退款记录 + 「申请退款」（金额默认剩余可退额度、可改小；备注可选 ≤255） | 决策 6/8/13 |

#### D. 员工退款审批（需求 3）

| # | 类型 | 位置 | 原来 | 改成 | 理由 |
|---|---|---|---|---|---|
| 2.1.17 | 数据模型 | `alembic/versions/`（新迁移） | `refunds` 无审批字段 | += `decided_by BIGINT NULL`（FK `employees.emp_id` ON DELETE SET NULL）、`decided_at TIMESTAMPTZ NULL`、`decision_note VARCHAR(255) NULL`、`approver_role VARCHAR(32) NULL`；另加 `ix_refunds_status_created_at` | 决策 7/11 |
| 2.1.18 | 接口 | `app/api/refunds.py` | 不存在 | `GET /api/refunds`（按退款单号/订单号/客户 user_id/状态多选/时间范围过滤）与 `POST /api/refunds/{id}/decision`（要 `approve:refund`） | 决策 3/12/16 |
| 2.1.19 | 行为 | `app/api/refunds.py` | — | 状态机**只允许** `requested → approved / rejected`；其余一律 **409** 并给出原因；审批走 `SELECT ... FOR UPDATE` | 决策 16 |
| 2.1.20 | UI | 新增 `web/src/app/(app)/internal/refunds/page.tsx` | 不存在 | 纯表单界面：筛选 + 列表 + 通过/驳回 + 审批意见（**不接 LLM**） | 决策 6/12/23 |
| 2.1.21 | 配置 | `app/api/scope.py`、`middleware.py`、`ratelimit.py`、`config.py`、`.env.example` | — | 为 4 条新路由登记 **scope + 限流 + 默认额度**（三处缺一即静默失效，有用例钉住） | 仓库既有纪律（`tests/api/test_api_contracts.py:192-214`） |

#### E. 演示与文档

| # | 类型 | 位置 | 原来 | 改成 | 理由 |
|---|---|---|---|---|---|
| 2.1.22 | 配置 | `scripts/seed.py:392-402` | 员工 2 绑 `internal_employee` | 员工 **3** 绑 `internal_employee`；员工 **2**（客服专员）绑 `customer_service` | 决策 22/25（`accounts.subject_id` 有唯一约束，一个主体只能一个账号） |
| 2.1.23 | 文案 | `README.md`（§角色与权限、§当前进度）、`docs/changes/README.md`（文件清单） | 6 权限点 / 6 角色表 | 8 权限点；补 003 行 | 收口（阶段 5）执行，本阶段先登记 |

### 2.2 不做（显式排除项，防范围蔓延）

| # | 不做的事 | 为什么 |
|---|---|---|
| 2.2.1 | **不接任何支付通道**，不做「已打款」第二动作，**不把 `orders.status` 改成 `refunded`** | 决策 4：本次 `requested → approved` 即终态，避免造一个假的打款闭环 |
| 2.2.2 | 员工退款页**不接 LLM**（不做「问一句」入口） | 决策 23 |
| 2.2.3 | 员工查询**不支持按客户手机号**搜索 | 决策 12：手机号是 PII，而 `customer_service` 的清晰度上限是「公开级(0)」 |
| 2.2.4 | **不新增 `amount` 的脱敏规则** | 见 §9 待裁决 **D1**：加了会连带让 `customer_service`(0) 与 `internal_employee`(1) 在审批页看不到金额 —— 恰好弄瞎必须审批的人 |
| 2.2.5 | 不改 `ROLE_MAX_SENSITIVITY`（角色清晰度上限） | 与本次需求无关；改它会全站生效 |
| 2.2.6 | 不改 MCP 暴露面（`create_refund_request` 仍不暴露） | 与本需求无关 |
| 2.2.7 | 不做导出/报表/批量审批/分页以外的列表能力 | 决策 12 只要求筛选查询 |
| 2.2.8 | 不删除图侧 `InterruptPolicy` 原语（`make_gate` / `NODE_GATE` / `Turn.interrupt`） | 见 §3.2 方案 A/B 与待裁决 **D2** |

---

## 3. 方案设计（怎么做 + 为什么这么做）

### 3.1 目标方案

**总原则：一处真相。** 能复用的能力一律复用，不写第二份 —— 尤其资金规则（行锁、剩余额度、幂等、审计）与脱敏规则表。

#### 3.1.1 需求 1：入口贯通（改的是**登录态写入**，不是 RBAC）

```
登录成功
  → visibleEntriesFor([role]) 算出该角色可进入的入口集合
  → 把**同一对令牌**写进这些入口的钥匙（token + refresh）
  → 按服务端返回的 entry 跳转（规则不变）
退出
  → 读三把钥匙，凡是令牌 claims.aid == 当前账号 id 的，全部清掉
```

**为什么不改 RBAC**：S1 已实测 `admin` 持有全部权限点，后端不欠任何权限。改 RBAC 是「改错地方」—— 它不会让 `/` 认得出管理员，因为那一页读的是**另一把钥匙**。

**必须显式承认的破坏性后果**（决策 1 的直接推论）：管理员在「已用客户身份登录」的浏览器上再登录时，会**覆盖** `shopsage.token.customer`。变更 002 建立的「三把钥匙互不覆盖」因此变为：

> 登录会把同一账号写进**它可进入的所有入口**钥匙；退出按**账号**清除；不同账号之间**后写覆盖先写**。

这是决策 1 的必然代价，不是实现瑕疵，因此 §4 有专门验收点（4.2）并有对应用例改写（§7.B3）。

#### 3.1.2 需求 2：客户申请退款（LLM + 手动，两条路一个出口）

**唯一的写出口**是 `app/tools/refund.request_refund(...)`，两条 UI 路径都走它：

| 入口 | 触发 | 订单与金额怎么定 |
|---|---|---|
| LLM | 客户说「帮我退款」，router 判为 `refund` → `refund_node` | 查该客户**可退订单**（`shipped`/`delivered`）：恰好 1 笔 → 直接落单，金额＝剩余可退额度；多笔 → **不落库**，回答里追问订单号；0 笔 → 如实说明 |
| 手动 | 客户在 `/orders` 点「申请退款」 | 页面默认填剩余可退额度，客户可改小；备注可选 ≤255 |

`app/tools/refund.py` 的重构（决策 26）：

```
create_refund_request(...)   ← 校验 write:refund   ┐
                                                    ├→ 共享核心 _insert_refund_once(...)
request_refund(...)          ← 校验 request:refund ┘   （行锁 + 剩余额度 + 幂等 + 审计，只有一份）
```

**为什么必须是「共享核心」而不是「`request_refund` 里传一个内部角色去调 `create_refund_request`」**：后者等于**伪造身份**（拿一个有权角色去顶替客户），一旦 `ROLE_PERMISSIONS` 变动就会静默取得或失去权限 —— 授权判断必须作用在**真实调用者**身上。

**LLM 节点的重放安全**（必须明说，否则是一个真实的重复退款风险）：图在失败时会**从断点重放**（`app/graph/run.py`），节点可能被执行两次。所以节点的幂等键必须是**确定性**的，而不是随机数：

```
key = "llm-" + sha256(f"{thread_id}|{order_id}").hexdigest()[:24]   # 确定性 + 长度 ≤64
```

（`thread_id` 形状是 `c2:<session>`，含 `:` 但幂等键字符集允许 `[A-Za-z0-9._:-]`；用哈希是为了守住 64 字符上限。）

**手动路径的幂等键**（决策 17）：前端在**打开表单时**生成一次随机键，同一次提交重复点只落一单；关闭再开会得到新键（那是一次新的申请意图）。

#### 3.1.3 需求 3：员工审批台

- `GET /api/refunds`：筛选 = 退款单号 / 订单号 / 客户 `user_id` / 状态（多选）/ 时间范围；默认按 `created_at DESC`；结果按调用者角色走**现有** `masking.mask_pii` 出口（`role=actor.role`）。
- `POST /api/refunds/{id}/decision`：`{decision: "approve"|"reject", note?}`。
  - 授权：`@require_permission(PERM_APPROVE_REFUND, identity_from="actor")`（仓库既有写法）。
  - 状态机：`SELECT ... FOR UPDATE` 锁行 → 只有 `requested` 可决策，否则 **409** + `error.code="refund_not_decidable"`。
  - 留痕：`status`、`decided_by = subject_id`（**仅当 `subject_type=="employee"`**，否则 `NULL`）、`approver_role`、`decided_at`、`decision_note`。
  - **不动** `orders.status`、**不接**支付通道（决策 4）。

#### 3.1.4 金额与脱敏（S12 的诚实结论）

- `payment_method` 在 `FIELD_RULES` 里是「财务级(3)」→ 客户（上限 1）**会被抹成 `***`**。
- `amount` / `total_amount` **不在** `FIELD_RULES` 里 → 走 `mask_pii` 会**原样输出**。

因此「金额走现有 masking」（决策 18）的**实际效果是：金额对客户可见**。这与「客户只看到自己能看的等级」这句话在字面上**不一致**，必须由你裁决（§9 待裁决 **D1**），我不替你选。

#### 3.1.5 对客话术的一个硬约束（S13）

`guardrail.INTERNAL_TERMS` 含 `refund_id` 与 `refunds`。所以给客户的退款结果块**不得出现英文字段名**，一律用中文标签（`退款单号` / `退款金额` / `处理状态`），否则模型复述字段名就会被输出侧护栏拦下 —— 表现为「退款成功了但客户看到一句拦截话术」。

### 3.2 备选方案与取舍

| # | 方案 | 优点 | 代价 | 结论 |
|---|---|---|---|---|
| 3.2.1 | **A（采用）**：登录写多入口钥匙 + 退出按账号清 | 一处改动解决 P1；存储层结构不变 | 破坏「三把钥匙互不覆盖」（后写覆盖先写） | **采用**（决策 1/14） |
| 3.2.2 | B：页面读取时回退到任一已登录入口的令牌 | 不改写入 | 「当前入口是谁」语义变模糊；三个页面会共用同一身份，与变更 002 的分离设计冲突 | 舍弃（决策 1 未选） |
| 3.2.3 | **A（采用）**：拆共享写核心 + `request_refund` | 资金规则只有一份；授权作用在真实调用者上 | `refund.py` 要小幅重构 | **采用**（决策 26） |
| 3.2.4 | B：`request_refund` 内部用有权角色调 `create_refund_request` | 改动更小 | **伪造身份**，权限表一变就静默漂移 | 舍弃：与「授权判真实调用者」相悖 |
| 3.2.5 | **A（采用）**：图侧 HITL 原语**保留**，只把生产默认关死（`GATED_ROLES=∅`、`SENSITIVE_ROUTES=()`），删掉 HTTP 审批接口与前端审批 UI | 改动面可控；框架能力仍有独立用例覆盖（`test_graph_interrupt` 里的最小图用例）；将来真出现「对话轮级写操作」不必重建 | 生产路径上是**未使用**的能力（一段"备用"代码） | **采用**（见 **D2**：若你要零死代码，选 B） |
| 3.2.6 | B：连 `InterruptPolicy`/`make_gate`/`NODE_GATE`/`Turn.interrupt` 一起删 | 零死代码 | 波及 `graph/{config,routing,nodes,builder,state}.py`、`run.py`、`queue/agent_runs.py`、`scripts/api_demo.py` 与多组用例，diff 显著变大且与本次三条需求无直接关系 | 待裁决 **D2** |
| 3.2.7 | **A（采用）**：新增 `refund` route + 真写节点 | 语义清楚；第 16 步评测能单独量「退款意图」准确率 | `ROUTES` 集合断言、few-shot、prompt 用例要同步改 | **采用**（决策 20） |
| 3.2.8 | B：复用 `ticket` route 挂写节点 | 不动 `ROUTES` | 一条路由两种写语义，节点里还要二次判意图，而两者权限点不同 | 舍弃（决策 20 未选） |

### 3.3 兼容性与迁移

- **向后兼容（破坏性，必须知晓）**：
  1. `POST /api/chat/approve` **被删除** → 老前端调用会得到 404。前端同批更新，无灰度。
  2. 登录态存储语义变更（§3.1.1）→ 已登录用户**刷新页面后**会按新规则重新认识自己的钥匙；既有令牌仍有效（不涉及签名/声明变更）。
  3. `flow_state` 的 `approval_required` 变成**不可达**取值（保留在契约里，前端不再渲染该分支）。
- **数据迁移**：需要 Alembic 迁移（2.1.17）。既有 `refunds` 行新列为 `NULL`（语义正确：历史单没有审批记录）。**`scripts/seed.py` 需重跑**（决策 22/25 改了演示账号绑定），且重跑会 `TRUNCATE` 业务表（向量表不受影响，见 `_truncate_business_tables` 的注释）。
- **回滚方式**：
  - 代码：`git revert` 本次提交（无 feature flag，回滚即回到现状）。
  - 库：`uv run alembic downgrade -1`（迁移的 `downgrade()` 删掉那 4 列与索引）；数据无损失（新列本来就是本次新增）。
  - 演示账号：`git checkout scripts/seed.py && uv run python -m scripts.seed`。

---

## 4. 验收判据（怎么算改完了）

> 每条都必须是「跑什么命令 → 看到什么输出才算过」。`§7` 回填实测。

| # | 判据 | 验证命令 | 期望输出 | 实测 |
|---|---|---|---|---|
| 4.1 | 三条新用例：`admin` 一套令牌能同时驱动 `/`、`/internal`、`/admin` 的后端接口 | `uv run pytest -q tests/api/test_entry_permissions.py` | 全绿；且断言 `entry_for_role` 汇总为 `admin` | ✅ **6 passed** |
| 4.2 | 登录写入多入口：`writeSessionPair` 后三个钥匙都能 `readSession` 出**同一账号**；退出按账号清空 | `cd web; npm test` | 用例通过（含「后写覆盖先写」的显式断言） | ✅ **201 passed** |
| 4.3 | `customer_service` 能进对内入口，但**查不到全站订单**（`Actor.internal == False`） | `uv run pytest -q tests/api/test_entry_permissions.py -k customer_service` | 进对内 ✅；`read:all_orders` 请求被拒 | ✅ 通过（入口 `internal` + 无 `read:all_orders`） |
| 4.4 | **客户不能自审批**：审批接口已不存在；客户令牌无法改退款单状态 | `uv run pytest -q tests/api/test_refunds_api.py tests/api/test_authorization.py` | approve 路由 404；客户决策 → 404 | ✅ 通过（HTTP 404 + `not_found_or_forbidden`） |
| 4.5 | LLM 路径：对客「帮我申请退款」在可退订单唯一时**落库为 `requested`** | `uv run pytest -q tests/graph/test_graph_refund.py -k unique` | 库里新增 1 行、`status=requested` | ✅ 通过（真链路：`refund_id=35`） |
| 4.6 | LLM 路径：多笔可退订单时**不落库**，回答里追问订单号 | `uv run pytest -q tests/graph/test_graph_refund.py -k multiple` | 退款行数不变；`answer` 含订单号追问 | ✅ 通过 |
| 4.7 | LLM 路径**重放安全**：同一轮重放两次只落 1 单 | `uv run pytest -q tests/graph/test_graph_refund.py -k replay` | 退款行数 == 1 | ✅ 通过（**本项抓出一个真实设计缺陷**，见 §7.B3 的 F1） |
| 4.8 | 手动路径：金额默认 = 剩余可退额度、可改小、备注可选；超额被拒 | `uv run pytest -q tests/api/test_refunds_api.py -k request` | 正常 201；超额 400/422 且原因可读 | ✅ 通过（超额 → 400 `refund_not_allowed`） |
| 4.9 | 员工接口：筛选（单号/订单号/user_id/状态多选/时间范围）返回正确子集 | `uv run pytest -q tests/api/test_refunds_api.py -k "filter or query or list"` | 每条筛选都有断言 | ✅ 通过 |
| 4.10 | 员工审批状态机：`requested→approved/rejected` 成功；重复决策 **409** | `uv run pytest -q tests/api/test_refunds_api.py -k decision` | 首次 200；再次 409 + `refund_not_decidable` | ✅ 通过（409 带 `current_status=approved`） |
| 4.11 | 审批留痕：`decided_by` = 审批人 `emp_id`、`approver_role`、`decided_at`、`decision_note` 都落库 | `uv run pytest -q tests/api/test_refunds_api.py -k audit` | 四字段与新表结构一致 | ✅ 通过（`decided_by=2`、`role=customer_service`） |
| 4.12 | 迁移可升可降 | `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` | 三步都成功，退出码 0 | ✅ 通过（三步均成功） |
| 4.13 | 限流映射闭合（4 条新路由三处登记齐全） | `uv run pytest -q tests/api/test_api_contracts.py` | 全绿（含反向断言 `test_every_scoped_route_is_actually_enforced`） | ✅ 通过 |
| 4.14 | 前端类型与构建 | `cd web; npm run typecheck; npm run build` | 均通过 | ✅ 通过（12/12 静态页；`/orders` 与 `/internal/refunds` 均在路由表） |
| 4.15 | 全量回归（后端） | `$env:UV_CACHE_DIR=".uv-cache"; uv run pytest -q` | 与基线**同口径或更好** | ✅ **1846 passed / 50 skipped / 1 failed**（基线 1780 passed；唯一 failed 是沙箱 `.env` 产物，见 §7.B1） |
| 4.16 | 全量回归（前端） | `cd web; npm test` | 全绿 | ✅ **201 passed**（基线 171） |
| 4.17 | 真链路取证：三角色各登录一次，三条主路径可用 | `uv run python -m scripts.evidence_refunds` | 三入口全通过 | ✅ **19/19 PASS**（原始输出见 §7.B4） |

---

## 5. 影响面自检（逐项确认过再打勾）

- [ ] `app/`（后端逻辑）—— rbac / accounts / api（chat、schemas、scope、middleware、ratelimit、config、新增 orders+refunds）/ graph（config、routing、nodes、builder、state）/ agents（router、synthesis、新增 refund、state）/ tools/refund / db（models、queries）
- [ ] `alembic/`（迁移）—— 新迁移 1 个（4 列 + 1 索引）
- [ ] `web/`（前端）—— session / auth / api / contracts / stream / useChatStream / SideNav / MessageList / 对客页 / 新增 orders 页 / 新增 internal/refunds 页 / globals.css
- [ ] `tests/`（用例）—— 见 §7.B3 的逐文件清单（改/删/增）
- [ ] `docs/architecture.md`（蓝图）—— 权限点数量与退款闭环（阶段 5 收口）
- [ ] `docs/modules/`（模块文档）—— `4.security.md`（8 权限点）、`6.tools.md`（`request_refund`）、`9.api.md`（新增/删除接口）、`7.agents_module.md`（`refund` route）、`8.graph.md`（闸门默认关闭）、`16.web.md`（新增两页）（阶段 5 收口）
- [ ] `docs/runbook/`（实操手册）—— `3.db-runbook.md`（新迁移）、`9.api-runbook.md`（接口增删）、`16-web-runbook.md`（两页与退出语义）（阶段 5 收口）
- [ ] `.env.example`（配置样例）—— 删 `RATE_LIMIT_CHAT_APPROVE`、加 4 条新 scope
- [ ] `README.md`（进度表 / 常用命令 / 角色权限表）（阶段 5 收口）
- [ ] `eval/`（golden dataset / 评测口径）—— 本变更**不改**（`refund` route 的评测样本属后续）

---

## 6. 任务拆解（定稿后才填，开发时逐条打勾）

> 按依赖排序；每条都必须能独立验证。

- [x] **T1** 权限点与入口判据 —— `app/security/rbac.py`、`accounts.py`；验证：`uv run pytest -q tests/security/test_rbac.py tests/security/test_accounts.py` → 新增断言通过
- [x] **T2** 迁移（4 列 + 索引）—— `alembic/versions/<new>.py`、`app/db/models.py`；验证：`uv run alembic upgrade head` → `alembic downgrade -1` → 再 upgrade，退出码 0
- [x] **T3** 退款工具重构 —— `app/tools/refund.py`（共享核心 + `request_refund` + `remaining_refundable`）；验证：`uv run pytest -q tests/test_tools_refund.py`
- [x] **T4** 对客订单查询接口 —— `app/api/orders.py`、`app/db/queries.py`；验证：`uv run pytest -q tests/api/test_orders_api.py`（含越权 404）
- [x] **T5** 退款接口（申请/查询/决策）—— `app/api/refunds.py`；验证：`uv run pytest -q tests/api/test_refunds_api.py`
- [x] **T6** 限流与 scope 三处登记 —— `app/api/{scope,middleware,ratelimit,config}.py`、`.env.example`；验证：`uv run pytest -q tests/api/test_api_contracts.py`
- [x] **T7** 关闭对客闸门 + 删除审批接口 —— `app/graph/config.py`、`app/api/{chat,schemas}.py`；验证：`uv run pytest -q tests/api/test_authorization.py`
- [x] **T8** 新增 `refund` route 与写节点 —— `app/agents/{state,router,refund}.py`、`app/agents/synthesis.py`、`app/graph/{state,nodes,routing,builder}.py`；验证：`uv run pytest -q tests/graph/test_graph_refund.py tests/test_agents_router.py`
- [x] **T9** 演示账号与种子 —— `scripts/seed.py`；验证：`uv run python -m scripts.seed` → `uv run python -m scripts.db_check --check`
- [x] **T10** 前端存储层（多入口写入 + 按账号退出）—— `web/src/lib/session.ts`；验证：`cd web; npm test`
- [x] **T11** 前端契约与客户端（删审批、加订单/退款）—— `web/src/lib/{contracts,api,stream}.ts`、`useChatStream.ts`、`MessageList.tsx`、`(app)/page.tsx`；验证：`cd web; npm run typecheck; npm test`
- [x] **T12** 客户订单页 —— `web/src/app/(app)/orders/page.tsx`；验证：`cd web; npm run build`
- [x] **T13** 员工退款台 —— `web/src/app/(app)/internal/refunds/page.tsx`；验证：`cd web; npm run build`
- [x] **T14** 侧边栏子页导航 —— `web/src/components/SideNav.tsx`、`web/src/lib/auth.ts`；验证：`cd web; npm test`（shell.test.ts）
- [x] **T15** 用例增删改 —— 按 §7.B3 清单；验证：全量 `uv run pytest -q` 与 `cd web; npm test`
- [x] **T16** 真链路取证 —— 新增 `scripts/evidence_refunds.py`；验证：`uv run python -m scripts.evidence_refunds`
- [x] **T17** 文档收口 —— `README.md`、`docs/modules/*`、`docs/runbook/*`、`docs/changes/README.md`；验证：人工核对 + 命令可执行

---

## 7. 实测证据（开发后回填）

### B1 · 后端基线（本次开工前，只读）

```text
$ $env:UV_CACHE_DIR="E:\Desktop\DeepSeekHarness\ShopSage\.uv-cache"; uv run pytest -q
1 failed, 1780 passed, 50 skipped, 3 warnings, 6 errors in 174.61s (0:02:54)
```

两条**环境产物**（与本次改动无关，改造前后都应为同一状态）：

| 项 | 原因 |
|---|---|
| `tests/api/test_cors.py::test_prod_defaults_to_no_cross_origin` | 用例在 `ENV=prod` 下断言「无跨源白名单」，但 `.env` 里**已配** `API_CORS_ORIGINS`（4 条）。属本机 `.env` 与用例前提冲突 |
| `tests/test_config.py` 的 6 个 ERROR | `PermissionError` 读 `.env`（沙箱限制），收集期即失败 |

```text
$ $env:UV_CACHE_DIR="..."; uv run pytest -q tests/api/test_cors.py::test_prod_defaults_to_no_cross_origin
E   AssertionError: assert ['http://local...68.1.13:3000'] == []
FAILED tests/api/test_cors.py::test_prod_defaults_to_no_cross_origin
1 failed, 2 warnings in 0.53s
```

### B2 · 前端基线

```text
$ cd web; npm test
ℹ tests 171
ℹ pass 171
ℹ fail 0
```

### B3 · 预期会变动的现有用例（决策 19：**先列出，确认后再改**）

> 「改」＝ 用例保留但断言随**本次有意的行为变更**调整；「删」＝ 被测能力已移除。

| 文件 | 用例 | 处置 | 依据 |
|---|---|---|---|
| `tests/security/test_accounts.py` | `test_entry_for_role` 里 `customer_service → ENTRY_CUSTOMER` | **改** → `ENTRY_INTERNAL` | 决策 15（有意变更） |
| `tests/test_agents_state.py` | `ROUTES == {data,rag,analysis,ticket,final}` | **改** → 含 `refund` | 决策 20 |
| `tests/graph/test_graph_state.py` | `test_node_names_are_unique_and_stable` 的 `names` 列表 | **改** → 加 `NODE_REFUND_REQUEST` | 决策 20 |
| `tests/graph/test_graph_interrupt.py` | `SENSITIVE_ROUTES == ("ticket",)` 与依赖该默认值的 3 条闸门用例 | **改** → 断言 `SENSITIVE_ROUTES == ()`，需要闸门的用例改为**显式**构造 `InterruptPolicy(enabled=True, routes=("ticket",))` | 决策 5/9 + §3.2 方案 A（框架能力保留，生产默认关死） |
| `tests/graph/test_graph_builder.py` | `test_gate_is_registered_only_when_the_policy_is_on` | **不动**（本就用显式 `enabled=`，语义仍成立） | 同上 |
| `tests/api/test_authorization.py` | `test_sensitive_route_returns_202_with_approval_gate`、`test_internal_role_skips_the_gate`、`test_approve_resumes_the_suspended_turn`、`test_reject_gives_an_honest_sentence`、`test_approve_twice_is_conflict_not_a_rerun` | **删**（接口已移除） | 决策 21 |
| `tests/observability/test_observability_wiring.py` | `test_gate_node_is_instrumented_when_it_runs` | **不动**（本就有「gate 没出现则跳过」的兜底），但会在注释里记明它现在是**条件命中** | 同上 |
| `tests/observability/test_observability_wiring.py` | `test_agent_nodes_are_all_instrumented` 的「7 个图节点」清单 | **改** → 加 `refund_request` | 决策 20 |
| `web/tests/session.test.ts` | `三把钥匙互不覆盖`、`writeSessionPair 用服务端返回的 entry`、`clearSessionPair 把访问令牌与刷新令牌一起清掉` | **改** → 按 §3.1.1 新语义重写，并**显式**断言「后写覆盖先写」这一代价 | 决策 1/14 |
| `web/tests/stream.test.ts` | `挂起（待人工确认）：approval 落进状态…` | **删**（flow_state 分支不再渲染） | 决策 21 |
| `web/tests/transport.test.ts` | `postApprove：thread_id 原样回传…` | **删**（`postApprove` 已移除） | 决策 21 |
| `web/tests/auth.test.ts` | `权限表：客户没有 read:all_orders…` / `默认入口…` | **改** → 覆盖新的 `canUseInternalEntry` 双谓词 | 决策 15 |
| `web/tests/shell.test.ts` | 侧边栏结构断言 | **改** → 加子页导航断言 | 决策 6 |
| `scripts/api_demo.py` | `approve` 场景 | **改** → 删除该场景（它是脚本，非用例；但它会被 §4.17 类取证引用） | 决策 21 |

> 我不在本次自测里修改这些用例的**判据方向**（不把红改成绿了事）：每一条的改动理由都必须能在上表找到「依据」列。

### B4 · 开发期抓到的三个**真实缺陷**（都是自测发现的，不是预想的）

这三个值得单独记：它们都是「编译通过、单测全绿、只有真跑一轮才暴露」的那一类，
而且每一个都对应一条我原本以为已经想清楚的设计。

#### F1 · 幂等键含金额 → 重放时**根本走不到**幂等插入（设计缺陷）

- **现象**：`tests/graph/test_graph_refund.py::test_replay_creates_only_one_refund`
  第一次跑就红，且红得很奇怪 —— 第二次执行返回的是「没有可退订单」，而不是「已提交」。
- **根因**：第一版幂等键是 `sha256(subject|order_id|**amount**|message)`。
  而**重放时额度已经被第一次占满**，`refundable_orders()` 返回空 →
  节点在「候选判断」那一步就返回了 `MSG_NO_REFUNDABLE`，
  **永远到不了幂等插入**。键里含金额，就注定重放时算不出同一个键。
- **修法**（不是改测试）：键只由**稳定事实**派生（谁 + 哪一单 + 原话），
  并新增 `find_refund_by_idempotency_key`，把「按键回查」放在**任何业务判断之前**。
  原则：**幂等回查必须早于业务判断**，否则判断会用「已经变了的世界」否掉一个已完成的事实。
- **证据**：`4.7` 通过；`llm_refund_key` 的 docstring 记了完整理由。

#### F2 · 条件边映射表用 **route 名**当键 → 真跑一轮 `KeyError`（框架接口误用）

- **现象**：真链路取证时 `route=refund` 判对了、节点也注册了、
  `get_graph()` 边表**完全正确**，但用户拿到**一句空回答**。
- **根因**：`route_by_intent` 返回的是**节点名**，而条件边的映射表是
  `{**ROUTE_NODES, ...}`（键是 **route 名**）。
  对 `data` / `rag` / `synthesis` 而言键与值**恰好相同**，所以这个错误潜伏至今；
  `refund` 的返回值 `"refund_request"` 不是那张表的键 → `_branch.py::_finish` 抛 `KeyError`。
- **为什么极难发现**：LangGraph **编译期不报**（它只检查「映射的**值**是不是已注册节点」，
  不检查「分支函数会不会返回映射里没有的键」）。
- **修法**：`_route_map()` 改为 `{node: node for node in set(ROUTE_NODES.values()) | {NODE_SYNTHESIS}}`，
  并新增 `tests/graph/test_graph_builder.py::test_conditional_edge_map_is_keyed_by_node_names`
  把「分支函数所有可能的返回值都必须是映射的键」钉住。
- **证据**：`visited = ['guardrail_input','router','refund_request','synthesis','guardrail_output']`。

#### F3 · 护栏的「材料摘要」漏了退款结果 → **真话被当成编造拦下**（假阳性）

- **现象**：图跑通了、退款也落库了，但客户拿到的是输出侧护栏的拦截话术。
  日志：`guardrail 输出侧拦截 stage=output rules=['llm_judge:…涉嫌越权'] detail=LLM 判定不合规`。
- **根因**：对客退款路径**不查 SQL、不检索知识库**（`rows`/`docs` 都是 0），
  而 `_materials_digest` 只报这几样。判定者看到「材料：0 行 / 0 条 / 无」，
  而回答里出现「退款单号 28、订单 12、金额 12346.75 元」——**它判定编造是正确的**。
  错在我们漏喂了「本次已经受理了一笔退款」这条事实。
- **修法**：把 `state["refund"]` 摘要进材料，并显式写明「这是**已发生**的事实」。
  **补事实而不是降低审查强度** —— 这条例外不该开。
- **教训（写给下一个人）**：新增一条「不经过检索、直接由节点产出事实」的路径时，
  必须同时更新所有「核对有没有编造」的下游，否则护栏会把**真话**当编造。
- **证据**：`tests/test_agents_guardrail.py` 新增 3 条用例（含「没有退款时摘要不多行」）。

#### F4 · 前端契约加了 `refund`，后端响应模型漏了（三出口没接齐）

- **现象**：真链路取证最后一条红：`refund: 无`，而图 state 里明明有。
- **根因**：我把 `refund` 加进了 WS 的 `FRAME_SAFE_KEYS` 与前端契约，
  **漏了 HTTP 的 `ChatResponse`**。于是走 WS 能看到退款卡片，走 `POST /api/chat` 的
  调用方（以及任何按 OpenAPI 生成的客户端）拿到的是一个没有该字段的信封。
- **修法**：`ChatResponse.refund` + `build_response` 补上。
- **教训**：新增「节点产出的事实」时，**三个出口都要接**：
  `state` 键 → HTTP 响应模型 → WS 帧白名单。

### B5 · 交付前最终实测（原始输出）

```text
# 后端全量（本次改动之后）
$ $env:UV_CACHE_DIR="E:\Desktop\DeepSeekHarness\ShopSage\.uv-cache"; uv run pytest -q
1 failed, 1846 passed, 50 skipped, 1 warning in 177.83s (0:02:57)

# 前端全量
$ cd web; npm test
ℹ tests 201
ℹ pass 201
ℹ fail 0

# 前端构建
$ cd web; npm run build
✓ Generating static pages (12/12)
Route (app)                              Size  First Load JS
├ ○ /                                 4.18 kB         131 kB
├ ○ /internal                         5.14 kB         132 kB
├ ○ /internal/refunds                 5.02 kB         132 kB
└ ○ /orders                           4.47 kB         131 kB
exit=0

# 迁移可升可降
$ uv run alembic upgrade head   → Running upgrade f3b8c41d9e27 -> c7a4e2f9b1d8
$ uv run alembic downgrade -1   → Running downgrade c7a4e2f9b1d8 -> f3b8c41d9e27
$ uv run alembic upgrade head   → Running upgrade f3b8c41d9e27 -> c7a4e2f9b1d8
```

### B6 · 真链路取证（`scripts/evidence_refunds.py`，19/19）

> 跑法：**另起一个实例在 8001** 核对新代码（`SHOPSAGE_API=http://127.0.0.1:8001`），
> 不去杀别人正在跑的 8000 进程 —— 那个进程是**旧代码**（实测 `/api/orders` 返回 404
> 而 `/api/chat/approve` 还活着）。这一步本身就是一条经验：
> **改完代码后原进程不会自动更新，取证前先确认端口上跑的是哪一版。**

```text
=== 需求 1：管理员入口贯通 ===
PASS  管理员 /me                                     HTTP 200 entry=admin
PASS  管理员持有全部权限点                            8 个：admin approve:refund read:all_orders
                                                     read:finance read:own request:refund
                                                     write:refund write:ticket
PASS  管理台可用（同一令牌）                          HTTP 200
PASS  对内退款审批台可用（同一令牌）                   HTTP 200
PASS  对客订单口：授权成立但无客户身份 → 400          HTTP 400 code=no_customer_identity

=== 需求 2：客户订单页与申请退款 ===
PASS  客户能查自己的订单                            HTTP 200 订单数=2
PASS  金额明文可见（决策 D1）                        total_amount='12346.75'
PASS  支付方式是 *** 或 null（财务级字段）            payment_method='***'
PASS  客户提交退款申请 → 201 且状态是 requested       HTTP 201 refund_id=33 status=requested
PASS  ★客户审批退款被拒（404，越权对外口径）          HTTP 404 code=not_found_or_forbidden
PASS  ★旧的对话轮审批接口已删除（404）                HTTP 404
PASS  重复提交同一个幂等键 → 同一张单                 HTTP 201 refund_id=33

=== 需求 3：员工退款审批台 ===
PASS  客服的入口是对内                              HTTP 200 entry=internal
PASS  员工能按单号查到那张退款单                      HTTP 200 命中=1
PASS  员工审批通过 → approved 且四样留痕都落库        HTTP 200 status=approved decided_by=2
                                                     role=customer_service
PASS  重复审批 → 409 且带上当前状态                   HTTP 409 code=refund_not_decidable
                                                     current_status=approved

=== 需求 2（LLM 路径）：一句话申请退款 ===
HTTP 200 route=refund flow_state=delivered
refund: {"status": "submitted", "refund_id": 35, "order_id": 12,
         "amount": "12345.75", "state": "requested", "remaining_after": "0.00"}
PASS  LLM 退款路径给了结构化结果                      status=submitted
PASS  ★对客路径不再挂起等批准（没有 approval）        flow_state=delivered approval=None
PASS  LLM 路由判成 refund（不是 ticket）              route=refund

=== 汇总 ===
共 19 条：19 通过、0 失败
```

---

## 8. 遗留与后续

以下是本次**明确不做**、但已识别的问题，开发完成后逐条落到 `issues/`：

| # | 问题 | 为什么不在本次范围 |
|---|---|---|
| L1 | **SoD 在员工侧不严格**：`customer_service` / `internal_employee` 同时持有 `write:refund` 与 `approve:refund`，即**可以发起并批准自己的退款** | 现有角色表本来就允许他们发起退款；收紧它属于「重新设计岗位分工」，需你单独裁决（见 **D2** 旁注） |
| L2 | 客户端的 `payment_method` 被判为财务级而**永远看不到**（连自己的订单也是） | 客户看自己订单的支付方式是否合理，属策略问题，不在本次需求内。**开发期实测把它定性了**：不是「策略保守」，而是脱敏表给枚举值配了 `mask_bank_card` → 六个角色**全部**拿到 `***`。已落 [`issues/008`](../../issues/008-payment-method-masked-for-everyone.md) |
| L3 | `flow_state="approval_required"` 保留但不可达 | 删它等于改动响应契约的取值域，收益小于风险；记入 `issues/` 备查 |
| L4 | 员工退款页无分页上限之外的保护（`limit` 夹取） | 与既有列表接口同口径（`MAX_LIMIT`） |
| L5 | 本机 `.env` 让 `test_cors.py` 与 `test_config.py` 6 条用例在沙箱内不可信 | 属环境配置问题，已在 §7.B1 记录口径 |
| L6 | 无浏览器级 E2E（`/orders` 与 `/internal/refunds` 的渲染只在 `npm run build` + 手工路径验证） | 仓库无 Playwright（变更 002 已记录同一局限） |

---

## 9. 澄清决策台账（26 条）+ 待裁决（2 条）

### 9.1 已裁决（按你逐轮的选择）

| # | 议题 | 决定 |
|---|---|---|
| 1 | 需求 1 根因与修法 | 登录时把同一令牌写进**该角色可进入的每个入口**钥匙 |
| 2 | 客户申请退款的权限模型 | 新增 `request:refund` 给 `customer`，新增独立「提交退款申请」服务 |
| 3 | 谁有权审批 | 新增 `approve:refund`，给 `customer_service` / `internal_employee` / `manager` / `finance_auditor` / `admin` |
| 4 | 审批通过后的终态 | `requested → approved`（不接支付、不改订单状态） |
| 5 | LLM 退款链路 | 对客去掉人工闸门 + 新增真写节点 |
| 6 | 两个新界面的位置 | 客户 `/orders`；员工 `/internal/refunds` |
| 7 | 是否新增字段 | 需要迁移：`decided_by` / `decided_at` / `decision_note` |
| 8 | 客户填什么 | 金额默认整单剩余可退额度、可改小；备注可选 ≤255 |
| 9 | 闸门移除范围 | `GATED_ROLES = 空集` |
| 10 | LLM 如何定订单与金额 | 唯一可退单直接落单；多笔则追问订单号 |
| 11 | 审批人如何落库 | `decided_by` 存 `emp_id`，另存 `approver_role` |
| 12 | 员工查询条件 | 退款单号 / 订单号 / 客户 `user_id` / 状态多选 / 时间范围 |
| 13 | 客户订单页范围 | 列表 + 明细 + 该单退款记录 + 直接发起申请 |
| 14 | 退出语义 | 清掉本账号写过的**所有**入口钥匙 |
| 15 | 客服进不去对内 | `entry_for_role` 把 `approve:refund` 也算作「可进对内」 |
| 16 | 状态机约束 | 只允许 `requested → approved/rejected`，其余 409 |
| 17 | 幂等键 | 前端每次打开表单生成随机键 |
| 18 | 订单页脱敏 | 走现有 masking 出口（**注意 §3.1.4 与 D1**） |
| 19 | 回归处理 | 先跑基线、把预期变红的用例列进变更单，确认后再改 |
| 20 | 路由设计 | 新增 route `refund` + 新增 `refund_request` 节点 |
| 21 | `/api/chat/approve` | 连同 rate-limit scope、前端 UI、相关用例一并删除 |
| 22 | 客服演示账号 | 在 `DEMO_ACCOUNTS` 里补一个 `customer_service` |
| 23 | 员工页是否接 LLM | 不接，纯表单 |
| 24 | 权限点命名 | `request:refund` + `approve:refund` |
| 25 | 客服绑哪个员工 | `internal_employee` 改绑员工 3；员工 2 改为 `customer_service` |
| 26 | 退款写路径 | 新增 `request_refund`，内部**复用** `create_refund_request` 的校验与写路径 |

### 9.2 「你决定」项（我按推荐项执行，在此复述）

| # | 我替你定的 | 风险 |
|---|---|---|
| A1 | 未知 route 仍落到 synthesis（既有兜底不变）；`refund` 节点失败按**网络型**节点处置（记 `refusals` 继续）而非收口型 | 落库失败时用户会看到「没办成」而不是 500 —— 与既有图级兜底口径一致 |
| A2 | 新迁移的列名与索引名按仓库命名习惯（`ix_refunds_status_created_at`） | 无 |
| A3 | `decided_by` 对非员工主体落 `NULL`（不是 0） | `0` 会被误读成「员工 0 审批的」；`NULL` 语义是「非员工审批」 |

### 9.3 待裁决（**定稿时的裁决结果**）

> 2026-10-16 你回复「确认」= 文档定稿 + 两处采用我的推荐项。按提示词的规则，
> 「你决定」项一律记入本台账，交付时复述。

- **D1 · 客户在订单页能否看到金额明文？** → **裁决：同意明文可见**（采用推荐项）。
  - 依据：`amount` 不在 `FIELD_RULES` 里，走 `mask_pii` 会原样输出；而 `/orders` 的核心用途就是「还剩多少可退」。
  - **明确不做**：不给 `amount` 加「财务级」规则 —— 那会连带让 `customer_service`(上限 0) 与 `internal_employee`(上限 1) 在**审批页也看不到金额**，恰好弄瞎必须做审批决策的人（见 §2.2.4）。
  - 副作用（已记录，不视为缺陷）：`payment_method` 仍是财务级 → 客户会看到 `***`。
- **D2 · 图侧 HITL 原语留还是删？** → **裁决：保留原语**（采用推荐项，§3.2.5）。
  - 保留 `InterruptPolicy` / `make_gate` / `NODE_GATE` / `after_gate` / `after_router_gate` / `Turn.interrupt`；只把生产默认关死（`GATED_ROLES=frozenset()`、`SENSITIVE_ROUTES=()`）并删掉 HTTP 接口与前端 UI。
  - 代价：生产路径上是一段**未使用**的能力。已记入 §8 遗留（L3 同族），并在 §7.B3 说明 `test_graph_interrupt.py` 改为显式构造 `InterruptPolicy(enabled=True, routes=("ticket",))` 以保住该原语的覆盖。

---

> **补充：一处需要你知晓的既有设计张力（L1）**
> 决策 3 让 `customer_service` / `internal_employee` 同时拥有「发起退款」与「审批退款」。
> 这**不引入新问题**（他们本来就能发起），但意味着**它们可以批准自己发起的退款** ——
> 本次的职责分离只对**客户**路径成立（客户只有 `request:refund`，永远无法自批，这正是需求 2 要修的）。
> 如果你要的是「发起人不得审批自己的单」，那是另一条规则（需在 `refunds` 上记发起人并与审批人比对），
> 请明确告知，我会把它作为**独立变更**处理，不塞进本次范围。
