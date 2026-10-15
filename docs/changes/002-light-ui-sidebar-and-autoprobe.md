# 002 · 前端改版：浅色护眼 + 登录/主界面分离 + 左侧边栏权限分流 + 自动探活 + 管理台表格修复

| 项 | 值 |
|---|---|
| 状态 | `已完成`（T1–T19） |
| 提出日期 | 2026-10-15 |
| 定稿日期 | 2026-10-15 |
| 影响模块 | `web/`（`src/app/`、`src/components/`、`src/lib/`、`tests/`、`scripts/`）、`scripts/`（取证脚本）、`docs/changes/README.md` |
| 关联 | 承接 [变更 001](001-account-login-and-admin-console.md)；不改后端 |

> **本文档是本次变更的唯一权威描述。** 在状态变为 `已定稿` 之前，不动任何业务代码。

---

## 1. 变更动机（为什么改）

### 1.1 现状（只读实测）

| # | 现状事实 | 证据位置 |
|---|---|---|
| S1 | 主题 `:root` 默认是**深色**（`--bg: #0b1020`），浅色只是 `[data-theme="light"]` 分支 | `web/src/app/globals.css:17-45` |
| S2 | 仓库里**没有任何切换主题的 UI**；`layout.tsx` 只读了一次 `localStorage` 却没人写它 | `web/src/app/layout.tsx:32-34`（全仓 grep `shopsage.theme` 仅此 1 处写入方，无设置方） |
| S3 | 因此**当前实际呈现是深色**，与「浅色护眼」需求相反 | 由 S1+S2 推出 |
| S4 | 导航是**顶部** `TopNav`，没有左侧边栏 | `web/src/components/TopNav.tsx:58-70` |
| S5 | 登录页有**四块**：①账号+口令 ②三个入口的登录态卡片 ③开发模式粘贴令牌 ④演示身份卡片 | `web/src/app/login/page.tsx:211-400` |
| S6 | 主界面**没有**「个人信息 + 权限状态」区；只有页面内零散 chip（如 `user_id=… · role`） | `web/src/app/page.tsx:113-117`、`web/src/app/internal/page.tsx:133-137` |
| S7 | 探活是登录页上一个**手动按钮**「探活 /health」，只在点击后执行 | `web/src/app/login/page.tsx:167-175, 191-193` |
| S8 | 管理台账号表写 `className="table"`，但 `globals.css` 里**只定义了 `table.data`**，`.table` 规则不存在 | `web/src/app/admin/page.tsx:311` vs `web/src/app/globals.css:163-181` |
| S9 | 后端**已实现**「账号+口令落库」：`accounts` 表 + Argon2id 散列 + `POST /api/auth/login`；实测库里 4 个账号 | `app/db/models.py:337-374`、`app/security/accounts.py:282-322`、`app/api/auth.py:100-116`；实测见 §7.E1 |
| S10 | `GET /api/auth/me` **已存在**，返回权威 `role` + `permissions` + `entry`，且会做状态/世代号校验 | `app/api/auth.py:161-175` |
| S11 | `GET /health` **恒 200**，依赖故障写在 body 的 `components` 里 | `app/api/health.py:251-299` |

### 1.2 问题（现状带来的具体损失）

- **P1（S1–S3）**：默认深色不满足「浅色护眼」；且主题切换的代码路径是**死代码**（没人写 `shopsage.theme`），却让人以为"能切"。
- **P2（S5）**：登录页把「登录」「看登录态」「粘令牌」「看演示身份」四件事堆在一页，与「登录界面要简洁，只管登录账户」直接冲突；演示卡片还泄露了演示账号的标识（`admin` / `13800000002` / `employee`）。
- **P3（S4+S6）**：入口散在顶部导航、个人信息散在页面正文里；「不同权限看到不同页面」这条规则没有一处统一的、可见的落点。
- **P4（S7）**：探活要人工点。用户打开页面时如果后端没起，看到的是一片空白或后续操作的报错，**不会**在第一时间被告知"后端没连上"。
- **P5（S8）**：管理台那张表**没有任何样式**——无边框、无内边距、无表头区分、长文本自由换行。这是「文本超长导致格式混在一起」的**根因**，不是内容问题。
- **P6（S11）**：探活"失败"的判据不能只看 HTTP 状态码——`/health` 恒 200。只看 `response.ok` 会把 `status:"down"`（Postgres 不可达）误判成"后端正常"。

### 1.3 不改的代价

- 每次演示都要解释"为什么是黑的"、"入口在哪"、"后端到底起没起"。
- 管理台账号表在真实数据（长标识、长时间戳）下不可读，管理员**看不清**自己正在停用谁 —— 这是一个**误操作风险**，不只是美观问题。
- 「账号密码落库」这条需求（S9）已经满足，但没有一份可复现的证据；下次有人问"真的存库里了吗"仍要重新翻代码。

---

## 2. 变更点（改哪些地方）

> 类型只有这几种：**行为** / **接口** / **数据模型** / **配置** / **文案** / **UI**。
> **本变更不涉及：接口、数据模型、配置、后端 `app/` 任何代码。**

### 2.1 做（本次范围内）

| # | 类型 | 位置 | 原来 | 改成 | 理由 |
|---|---|---|---|---|---|
| 2.1.1 | UI | `web/src/app/globals.css` | `:root` 深色 + `[data-theme="light"]` 浅色分支 | **单一浅色护眼主题**：米白/浅灰底 + 低饱和蓝绿强调色；删除 `[data-theme="light"]` 分支 | P1：需求要浅色护眼；删分支是为了不留"死代码"（S2 那条没人写的路径） |
| 2.1.2 | UI | `web/src/app/layout.tsx` | `beforeInteractive` 主题脚本 + `<header class="topbar">` + `<TopNav/>` | 删掉主题脚本与顶部导航；`layout.tsx` 只留 `<html><body>{children}</body></html>` | P1+P3：主题不再可变（2.1.1），导航改由侧边栏承担（2.1.5） |
| 2.1.3 | UI | `web/src/app/(auth)/login/page.tsx`（由 `web/src/app/login/page.tsx` 迁入） | 四块内容 | **只留账号 + 口令表单**；删掉 ②登录态卡片 ③开发模式 ④演示身份卡片；配全屏空白布局 | P2：需求原文"登录界面要简洁，只管登录账户" |
| 2.1.4 | UI | 新增 `web/src/app/(auth)/layout.tsx` | 无 | 登录专用布局：**不渲染侧边栏**，只给一个居中的卡片容器 | 需求原文"登录界面和主界面分开" |
| 2.1.5 | UI | 新增 `web/src/app/(app)/layout.tsx` + `web/src/components/SideNav.tsx` | 无（顶部 `TopNav`） | **左侧固定侧边栏**：按权限显示入口；底部固定「个人信息 + 权限状态 + 退出登录」 | P3：需求原文"页面入口放在左侧边栏" |
| 2.1.6 | UI | `web/src/app/(app)/page.tsx`、`(app)/internal/page.tsx`、`(app)/admin/page.tsx`、`(app)/citation/page.tsx` | 位于 `web/src/app/*/page.tsx` | 移入 `(app)` 路由组（**URL 不变**：`/`、`/internal`、`/admin`、`/citation`） | 用路由组做布局隔离，不改 URL（避免动 runbook 与 live 用例的地址） |
| 2.1.7 | UI | `web/src/components/SideNav.tsx` | 无 | 底部的个人信息与权限状态：**优先**调 `GET /api/auth/me` 取权威 `role`/`permissions`/`entry`；请求失败时回退本地令牌解析，并在界面上**注明这是回退值** | P3 + 需求"主界面展示个人信息和权限状态"；用 `/me` 而不是前端权限表副本（`lib/auth.ts` 自己标了"是副本不是真相"） |
| 2.1.8 | 行为 | 新增 `web/src/components/HealthBanner.tsx`，挂在 `(auth)/layout.tsx` 与 `(app)/layout.tsx` | 探活只在登录页点按钮时执行 | **每次打开/刷新页面各自动执行一次** `GET /health`；失败或 `status != "ok"` 时在页面**顶部**显示醒目异常横幅（含 `apiBase`、组件级状态、分诊步骤）；保留一个「重新探活」小按钮 | P4 + 需求原文；横幅挂在布局上 = 所有页面都覆盖 |
| 2.1.9 | 行为 | `web/src/lib/health.ts`（新增） | 判据散在页面里 | 抽出**纯函数** `judgeHealth(payload)`：区分 `ok` / `degraded` / `down` / `unreachable`（前者是响应读不到）；`/health` 恒 200，**不能只看 `response.ok`** | P6：这是最容易写错的一处判据，必须可单测 |
| 2.1.10 | UI | `web/src/app/(app)/admin/page.tsx` | `className="table"`（无对应样式） | 改用**已存在**的 `table.data` + `scroll-x` 容器；长文本（标识、最近登录）单行省略号 + `title` 悬浮全文；操作列 `white-space: nowrap` | P5：修根因，且不新造一套样式（`table.data` 已存在，见 `globals.css:163-181`） |
| 2.1.11 | UI | `web/src/app/globals.css` | 无侧边栏样式 | 新增 `.shell`（左侧栏 + 内容区两栏）、`.sidenav`、`.sidenav-link`、`.sidenav-foot`、`.health-banner`、`.cell-ellipsis` 等 | 承载 2.1.5 / 2.1.8 / 2.1.10 |
| 2.1.12 | 文案 | `(auth)/login/page.tsx` | 含演示账号标识与令牌排障话术 | 只留登录所需的提示（口令错误与账号不存在是同一句话=防枚举）；**不再展示任何演示账号标识** | P2：登录页只讲登录 |
| 2.1.13 | UI | `web/src/components/TopNav.tsx` | 顶部导航组件 | **删除该文件**（能力并入 `SideNav.tsx`） | 2.1.2 之后无引用方，留着是死代码 |

### 2.2 明确不做（防范围蔓延）

| # | 不做的事 | 理由 |
|---|---|---|
| 2.2.1 | **不改任何后端代码**（`app/`、`alembic/`、`scripts/`） | 需求 2「账号密码存数据库」**已实现**（S9/S10）。本次只在第 4 步用真实运行取证，不新增功能 |
| 2.2.2 | 不做「强制口令复杂度 / 首次登录改密 / 登录失败锁定」 | 会改后端安全策略与测试口径，属于**另一次变更** |
| 2.2.3 | 不做注册 / 自助改密页面 | 需求没提；`change_own_password` 目前无 HTTP 出口，补它要改 `app/api/` |
| 2.2.4 | 不删除 `lib/session.ts` / `lib/auth.ts` 里"三把钥匙互不覆盖"的存储能力 | 那是**存储层**能力，`tests/live` 与排障仍依赖；本次只删**界面**（2.1.3） |
| 2.2.5 | 不改 `lib/api.ts` 的 `getHealth` / `getMe` 契约 | 现有实现已够用（`getHealth` 已解析 `components`） |
| 2.2.6 | 不引入 UI 组件库 / 图标库 / `tailwindcss-animate` | 与 `globals.css` 顶部注释同一条取舍：类名静默失效是最难查的前端问题 |
| 2.2.7 | 不改 URL 结构（`/`、`/internal`、`/admin`、`/citation`、`/login` 全部不变） | `docs/runbook/16-web-runbook.md` 与 `web/tests/live` 都按这些地址写 |
| 2.2.8 | 不改 `/health` 的后端语义（恒 200、不引入 DB 客户端） | 那是 `app/api/health.py` 刻意定下的口径，改它属于后端变更 |
| 2.2.9 | 不做移动端专门适配 | 只保证窄屏可用（侧边栏收成顶部条 / 表格横向滚动），不做响应式重构 |

---

## 3. 方案设计（怎么做 + 为什么这么做）

### 3.1 目标方案

#### 3.1.1 目录与布局（路由组隔离）

```text
web/src/app/
├── layout.tsx              # 根：只留 <html lang="zh-CN"><body>{children}</body></html>
├── globals.css             # 唯一主题（浅色护眼）
├── (auth)/                 # 登录专用外壳：全屏、无侧边栏
│   ├── layout.tsx          #   + <HealthBanner/>（2.1.8）
│   └── login/page.tsx      #   → URL 仍是 /login
└── (app)/                  # 主界面外壳：左侧边栏
    ├── layout.tsx          #   <SideNav/> + <HealthBanner/> + <main class="shell-main">
    ├── page.tsx            #   → URL 仍是 /
    ├── internal/page.tsx   #   → URL 仍是 /internal
    ├── admin/page.tsx      #   → URL 仍是 /admin
    └── citation/page.tsx   #   → URL 仍是 /citation
```

**为什么用路由组而不是 `pathname` 判断**：`pathname` 方案要求每个页面都渲染外壳再隐藏它，会带来一次"外壳闪一下"的 hydration 抖动；路由组在**构建期**就把两套外壳分开，登录页的 HTML 里根本不存在侧边栏节点。

**为什么 URL 不变**：`(auth)` / `(app)` 是 Next 的**路由组**语法，括号目录**不参与 URL**。

#### 3.1.2 权限分流（唯一一处界面边界）

```text
SideNav 读当前登录态（沿用 lib/session.ts 的 readActiveSession）
  ├─ 调 GET /api/auth/me（权威：role / permissions / entry）
  │    ├─ 成功 → 用它渲染「个人信息 + 权限状态」，并据此决定显示哪些入口
  │    └─ 失败 → 回退 decodeJwtPayload + ROLE_PERMISSIONS 本地副本
  │              并在界面上明确标注「回退值（未与后端核对）」
  └─ 入口显示规则（复用 lib/auth.ts 已有函数，不新写一套）
       ├─ 对客问答：始终显示
       ├─ 对内查询：canUseInternal(role)
       └─ 管理台  ：canUseAdmin(role)
```

**⚠️ 与 `lib/auth.ts` 模块注释同一条纪律**：这里只决定「显示哪些链接」，**不是安全边界**。客户把地址栏敲成 `/admin` 一样能打开，而那一页的每个请求都会被后端拒。前端分流的价值是"别让用户看见自己点不动的东西"。

**为什么优先 `/me` 而不是只用本地令牌**：`/me` 会做**账号状态 + 世代号**校验（`app/api/deps.py`），所以它同时回答了两个问题——"我是谁"和"这条令牌还有效吗"。本地解析做不到后者。

#### 3.1.3 自动探活（判据必须含 body）

```text
页面挂载（(auth)/layout.tsx 或 (app)/layout.tsx）→ 自动执行一次 getHealth()
  ├─ fetch 抛错（没有 HTTP 状态码）      → unreachable（后端没起 / 跨源被拦）
  │     └─ 提示语用现有 lib/diagnostics.ts::probeFailureHint（已覆盖这三种原因的区分）
  ├─ 200 且 body.status === "ok"          → 正常，不显示横幅
  └─ 200 但 body.status === "degraded"    → 黄色警告横幅，列出非 ok 的 components
     或 body.status === "down"           → 红色异常横幅，列出非 ok 的 components
```

**⚠️ 关键判据（P6）**：`/health` **恒返回 200**（`app/api/health.py:251` 的刻意设计——依赖故障不能触发存活探针重启进程）。所以：

| 只看 `response.ok` | 实际含义 | 结论 |
|---|---|---|
| `true` | 可能是 `status:"ok"`，也可能是 `status:"down"`（Postgres 不可达） | **判据不完整** |
| 必须再看 `body.status` | `ok` / `degraded` / `down` | 这才是完整判据 |

这也是为什么 `judgeHealth()` 要抽成纯函数单独测（2.1.9）——**只测"fetch 成功"的用例会漏掉这整个分支**。

#### 3.1.4 浅色护眼配色

```css
:root {
  --bg:        #f6f7f4;   /* 米白（略偏暖，比纯白 #fff 不刺眼） */
  --bg-soft:   #ffffff;   /* 卡片面 */
  --bg-card:   #ffffff;
  --border:    #dfe3dc;   /* 低对比边框，不抢视线 */
  --text:      #23282d;   /* 近黑但不纯黑（纯黑在浅底上对比过强） */
  --text-dim:  #6b7280;
  --accent:    #2f7d6e;   /* 低饱和青绿（护眼向），替代原 #5b8cff 高饱和蓝 */
  --accent-soft: #e7f1ee;
  --ok:        #2f8f5b;
  --warn:      #b07d1f;
  --danger:    #c0503f;
}
```

**取舍**：强调色从高饱和蓝改成低饱和青绿，是为了满足「护眼」；代价是**与 `ok` 绿色系更接近**，所以 `--ok` 刻意用更饱和的绿、`--accent` 偏青，让"按钮"与"成功徽章"仍可区分。

### 3.2 备选方案与取舍

| # | 方案 | 优点 | 代价 | 结论 |
|---|---|---|---|---|
| 3.2.1 | **A：路由组 `(auth)` / `(app)` 隔离外壳** | 构建期分离，登录页 HTML 里没有侧边栏；URL 不变 | 需要移动 4 个页面文件 | **采用** |
| | B：保留现有目录，客户端按 `pathname` 决定是否渲染侧边栏 | 不动目录 | 每个页面都渲染外壳再隐藏 → hydration 抖动；登录页 HTML 里仍带侧边栏节点 | 舍弃：代价是**用户体验可见的**闪烁 |
| | C：主界面挪到 `/app/…` | 隔离最直白 | URL 变了 → 要改 runbook + live 用例 + 可能的书签 | 舍弃：范围蔓延（违反 2.2.7） |
| 3.2.2 | **A：固定单一浅色主题** | 最简；没有"切到深色又刺眼"的回头路；删掉 S2 那处死代码 | 失去深色偏好 | **采用** |
| | B：浅色为默认 + 保留 dark 分支与切换按钮 | 保留弹性 | 界面上多一个与需求无关的控件；dark 分支没人测 → 长期腐烂 | 舍弃 |
| | C：保留深色默认，只调柔浅色分支 | 改动最小 | 不满足需求字面要求（S3 说明现在就是深色） | 舍弃 |
| 3.2.3 | **A：`/me` 优先 + 本地回退并标注** | 权限状态是权威值；顺便验证令牌有效性 | 多一次请求 | **采用** |
| | B：只用本地令牌 + 权限表副本 | 无额外请求 | 用的是 `lib/auth.ts` 自己标注"是副本不是真相"的表；且不发现"令牌已被作废" | 舍弃 |
| | C：只用登录响应里的 permissions | 无额外请求 | 刷新页面后拿不到（登录响应不落盘）；首屏无权限状态 | 舍弃 |
| 3.2.4 | **A：自动探活挂在两个 layout 上** | 覆盖"每次打开或刷新"；一处实现，全站生效 | 每次刷新多一次 `/health` 请求 | **采用**（`/health` 是零依赖 TCP 探活，成本极低） |
| | B：只在登录页自动探活 | 改动最小 | 主界面断连时无全局提示 | 舍弃 |
| | C：自动探活 + 失败模态框 | 最醒目 | 打断操作，与"简洁"冲突 | 舍弃 |
| 3.2.5 | **A：管理台表格复用已有的 `table.data` + 省略号** | 不新造样式；同时修根因（P5）与超长文本 | 长文本默认看不全，需悬浮 | **采用** |
| | B：长文本自动换行 | 不丢信息 | 行高失控、列宽仍错乱 | 舍弃 |
| | C：整表横向滚动不截断 | 信息全 | 看操作列必须横向拖 | 舍弃：操作列（停用/改角色/重置口令）是**最该一眼看到**的 |
| 3.2.6 | **A：删掉登录页的"开发模式粘贴令牌"界面** | 满足"只管登录" | 排障少一个入口 | **采用**，代价用 3.3 的"回滚方式"覆盖 |
| | B：收进默认折叠区 | 排障入口仍在 | 登录页仍臃肿 | 舍弃 |

### 3.3 兼容性与迁移

- **向后兼容**
  - **URL 全部不变**（3.1.1 路由组不参与 URL）→ 书签、runbook、live 用例的地址不受影响。
  - **存储层不变**：`localStorage` 的 key（`shopsage.token.*` / `shopsage.refresh.*` / `shopsage.session.*`）一律不动 → **升级后不需要重新登录**（这点与变更 001 不同）。
  - **`lib/auth.ts` / `lib/session.ts` / `lib/api.ts` 的导出签名一律不变** → 119 条离线用例中依赖它们的不受影响。
  - ⚠️ **一处不兼容**：删掉登录页的"开发模式"后，**手粘令牌**这条排障路径**没有界面了**。替代手段（写在文档里，不改代码）：`uv run python -m scripts.dev_token --account admin` 签令牌后，用 `localStorage.setItem("shopsage.token.admin", "<token>")` 在 DevTools 里直接写。
- **数据迁移**：**不需要**。无表/字段/索引变化，无 Alembic 迁移，无需重灌种子。
- **配置**：**无变化**。不动 `.env` / `.env.example` / `NEXT_PUBLIC_API_BASE` 的语义。
- **回滚方式**
  - 纯前端改动，回滚 = `git revert <本次提交>`（或 `git checkout -- web/`）。
  - 无数据库副作用，**回滚不需要重建任何数据**。
  - 唯一需要人工重做的是：如果已经用新布局登录过，回滚后**旧登录态仍有效**（存储 key 没动），无需重新登录。

---

## 4. 验收判据（怎么算改完了）

> 每条都必须能"跑一个命令 / 做一个动作 → 看到确定的输出"。

| # | 判据 | 验证方式（命令/步骤） | 期望输出 | 实测 |
|---|---|---|---|---|
| 4.1 | 前端构建通过 | `cd web; npm run build` | `✓ Compiled successfully`；静态页数 ≥ 现有 9 个；`/login`、`/`、`/internal`、`/admin`、`/citation` 五个路由都在 | ✅ `✓ Compiled successfully in 11.2s`；10/10 静态页；7 条路由含全部 5 个（见 §7.E2） |
| 4.2 | 类型检查通过 | `cd web; npm run typecheck` | 退出码 0，无输出 | ✅ `TYPECHECK_EXIT=0`（见 §7.E2） |
| 4.3 | 离线用例全绿（含新增） | `cd web; npm test` | `pass 119` **+ 本次新增条数**，`fail 0` | ✅ `tests 150 / pass 150 / fail 0`（119 + 新增 31，见 §7.E2） |
| 4.4 | **浅色为唯一主题**：`globals.css` 里不再有深色分支，`layout.tsx` 里不再有主题脚本 | `grep -n 'data-theme' web/src/app/globals.css web/src/app/layout.tsx` | **无匹配**（退出码 1） | ✅ 仅剩**注释**里的说明文字（globals.css:11/13/17、layout.tsx:16）；**代码无 `data-theme` 属性**，由 `shell.test.ts` 去注释后断言（见 §7.E5） |
| 4.5 | **登录页只剩登录**：不含登录态卡片 / 开发模式 / 演示身份 | `grep -nE '开发模式\|演示身份\|当前登录态\|DEMO_PRESETS\|showDevMode' 'web/src/app/(auth)/login/page.tsx'` | **无匹配** | ✅ 仅剩**注释**里的「已删除什么」说明（第 8/9/14/15 行）；代码无匹配（见 §7.E5） |
| 4.6 | **登录页不渲染侧边栏**：`(auth)/layout.tsx` 不引用 `SideNav` | `grep -n 'SideNav' 'web/src/app/(auth)/layout.tsx'` | **无匹配** | ✅ 无匹配；`/login` 实际 HTML 含 `auth-shell`、**不含** `sidenav`（见 §7.E4） |
| 4.7 | **主界面有左侧边栏且按权限分流**：`SideNav` 引用 `canUseInternal` / `canUseAdmin`，`(app)/layout.tsx` 引用 `SideNav` | `grep -n 'canUseInternal\|canUseAdmin' web/src/components/SideNav.tsx` + `grep -n 'SideNav' 'web/src/app/(app)/layout.tsx'` | 两者均有匹配 | ✅ `SideNav` 走 `visibleEntriesFor`（内部用 `canUseInternal`/`canUseAdmin`）；`(app)/layout.tsx` 有 `<SideNav />`（见 §7.E4） |
| 4.8 | **个人信息 + 权限状态来自 `/me`，失败有回退标注** | `grep -n 'getMe\|回退' web/src/components/SideNav.tsx` | 均匹配 | ✅ 有 `getMe(`；回退分支含「本地回退值」字样（见 §7.E4） |
| 4.9 | **自动探活挂两个 layout**（不是按钮触发） | `grep -n 'HealthBanner' 'web/src/app/(auth)/layout.tsx' 'web/src/app/(app)/layout.tsx'` | 两个文件各 1 处 | ✅ 两个 layout 各 1 处 `<HealthBanner />`（见 §7.E4） |
| 4.10 | **探活判据含 body.status**（不只 `response.ok`） | `npm test` 里 `health.test.ts` 的用例：`/health` 返回 200 + `status:"down"` → 判为异常 | 该用例 pass | ✅ `★ HTTP 200 但 status="down" 必须判成异常` pass（见 §7.E2） |
| 4.11 | **管理台表格用 `table.data`**（不再是无样式的 `table`） | `grep -n 'className="table"' web/src/app/\(app\)/admin/page.tsx` | **无匹配**（应为 `table.data`） | ✅ 唯一命中在第 311 行**注释**里（说明修了什么）；实际代码第 316 行是 `className="data"`（见 §7.E5） |
| 4.12 | **管理台长文本有省略号 + 悬浮全文** | `grep -n 'cell-ellipsis\|title=' 'web/src/app/(app)/admin/page.tsx'` | 匹配 | ✅ 标识列与最近登录列都有 `cell-ellipsis` + `title=`；产物 CSS 含 `.cell-ellipsis` 与 `.cell-actions`（见 §7.E3） |
| 4.13 | **URL 未变**：路由组不参与 URL | 起 `npm run dev` 后逐个访问 `http://127.0.0.1:3000/`、`/login`、`/internal`、`/admin` | 全部返回 200（不 404） | ✅ 5 个路由全部 HTTP 200（见 §7.E4） |
| 4.14 | **后端未连上时给出异常提示**（真实断连验证） | 见 §6 自测计划 T14：把 `NEXT_PUBLIC_API_BASE` 指向一个空闲端口重启前端，打开页面 | 页面顶部出现异常横幅，文案含"后端没起 / 跨源被拦"与 `apiBase` | ✅ 真 fetch 到 `127.0.0.1:8999` → `state=unreachable`，分诊文案完整含 apiBase（见 §7.E3） |
| 4.15 | **后端正常时不显示横幅** | 后端在跑时打开页面 | 无横幅（正常态不打扰） | ✅ 真 fetch 到 `127.0.0.1:8000` → `state=ok`，5 个组件全 ok，退出码 0（见 §7.E3） |
| 4.16 | **账号密码确实落库**（需求 2 取证，不改代码） | `uv run python -m scripts.evidence_accounts` | accounts 表存在且有 4 行；`password_hash` 是 Argon2id（**不是明文**） | ✅ 4 行；4/4 均为 `argon2$argon2id$…`（103 字符）；明文行 0（见 §7.E1） |
| 4.17 | **登录真的查库**（需求 2 取证） | `curl -X POST http://127.0.0.1:8000/api/auth/login …` | HTTP 200 + `access_token`；错口令 → 401 且**与"账号不存在"同一句话** | ✅ 正确口令 200（6 个权限点）；错口令与不存在账号**逐字相同**的 `invalid_credentials` + 「账号或口令不正确」（见 §7.E1） |
| 4.18 | 原有功能回归：对客问答 / 对内查询 / 管理台开账号 三条主路径可用 | `uv run python -m scripts.evidence_paths` | 三条路径均可完成，无新增报错 | ✅ 三条路径全 PASS（对客 `delivered`、对内带 `bar` 图、管理台 4 账号）+ 越权对照 404（见 §7.E6） |

---

## 5. 影响面自检（逐项确认过再打勾）

- [x] `app/`（后端逻辑）—— **未改**（见 2.2.1）；仅在 `scripts/` 下新增 2 个**只读取证**脚本
- [ ] `alembic/`（迁移）—— **未改**（无表/字段变化）
- [x] `web/`（前端）—— 本次唯一改动面（`src/app/`、`src/components/`、`src/lib/`、`tests/`、`scripts/`）
- [x] `tests/`（用例）—— 新增 `web/tests/health.test.ts`（10 条）、`web/tests/shell.test.ts`（21 条）；改 `web/tests/hook-wiring.test.ts` 的页面路径
- [ ] `docs/architecture.md`（蓝图）—— 未改（架构未变）
- [x] `docs/modules/`（模块文档）—— 已回写 `docs/modules/16.web.md`
- [x] `docs/runbook/`（实操手册）—— 已补「登录页开发模式已移除，怎么手粘令牌」
- [ ] `.env.example`（配置样例）—— 未改
- [x] `README.md`（进度表 / 常用命令）—— 已加「变更 002」一行
- [ ] `eval/`（golden dataset / 评测口径）—— 未改

---

## 6. 任务拆解（定稿后才填，开发时逐条打勾）

> 按依赖排序；每条都能独立验证。格式：`- [x] T# 描述 —— 文件：… ；验证：命令 → 期望`

- [x] **T1** 浅色护眼主题 —— 文件：`web/src/app/globals.css`（`:root` 变量 + 删 `[data-theme="light"]` 分支）；验证：`grep -n 'data-theme' web/src/app/globals.css` → 代码无匹配（仅注释）
- [x] **T2** 根布局瘦身 —— 文件：`web/src/app/layout.tsx`（删主题脚本与 `<header>`/`TopNav`）；验证：`grep -n 'shopsage-theme\|TopNav' web/src/app/layout.tsx` → 代码无匹配（仅注释）
- [x] **T3** 新增自动探活纯函数 —— 文件：`web/src/lib/health.ts`（`judgeHealth` / `probeHealth`）；验证：`cd web; npm test` → `health.test.ts` 10 条全绿
- [x] **T4** 新增探活横幅组件 —— 文件：`web/src/components/HealthBanner.tsx`（挂载时自动探一次 + 「重新探活」按钮 + 失败用 `probeFailureHint`）；验证：源码含 `useEffect(` / `probeHealth(`
- [x] **T5** 新增侧边栏组件 —— 文件：`web/src/components/SideNav.tsx`（权限过滤入口 + `/me` 权威身份 + 本地回退标注 + 退出登录）；验证：含 `getMe(` / `visibleEntriesFor` / 「本地回退值」
- [x] **T6** 新增两套布局 —— 文件：`web/src/app/(auth)/layout.tsx`、`web/src/app/(app)/layout.tsx`；验证：`(auth)` 无 `SideNav`、`(app)` 有 `SideNav`，两者都有 `HealthBanner`
- [x] **T7** 迁移页面到 `(app)` 组 —— 文件：`page.tsx`、`internal/page.tsx`、`admin/page.tsx`、`citation/page.tsx` 移入 `web/src/app/(app)/`；验证：`npm run build` → 路由仍是 `/`、`/internal`、`/admin`、`/citation`
- [x] **T8** 登录页瘦身并迁入 `(auth)` —— 文件：`web/src/app/(auth)/login/page.tsx`（只留账号+口令）；验证：代码无 `开发模式|演示身份|DEMO_PRESETS|showDevMode`
- [x] **T9** 修管理台表格 —— 文件：`web/src/app/(app)/admin/page.tsx`（`table` → `table.data` + `scroll-x` + `cell-ellipsis` + `title`）；验证：代码无 `className="table"`，产物 CSS 含 `table.data` / `.cell-ellipsis`
- [x] **T10** 删除顶部导航 —— 文件：`web/src/components/TopNav.tsx`；验证：全仓无 `<TopNav` / `from … TopNav` 代码引用（仅注释）
- [x] **T11** 新增离线用例 —— 文件：`web/tests/health.test.ts`（探活四态，**含 200+down**）、`web/tests/shell.test.ts`（侧边栏分流 + 外壳结构 + 主题/表格样式）；验证：`cd web; npm test` → 150 全绿
- [x] **T12** 构建与类型回归 —— 验证：`cd web; npm run build && npm run typecheck` → 均退出码 0
- [x] **T13** 真实起前端逐个访问路由 —— 验证：`npm run dev` → 5 个路由均 200
- [x] **T14** **断连验证探活**：`NEXT_PUBLIC_API_BASE=http://127.0.0.1:8999` → `state=unreachable` + 完整分诊文案
- [x] **T15** 正常态验证：指向 `127.0.0.1:8000` → `state=ok`，5 个组件全 ok，退出码 0
- [x] **T16** 需求 2 取证（不改代码）：`uv run python -m scripts.evidence_accounts` → 4 行、全部 Argon2id、明文 0
- [x] **T17** 需求 2 取证：正确口令 → 200；错口令与不存在账号 → **逐字相同**的 401
- [x] **T18** 三条主路径回归 + 侧边栏三角色分流：`uv run python -m scripts.evidence_paths` + `npx tsx scripts/sidebar-evidence.ts` → 全 PASS
- [x] **T19** 文档回写：本文件 §4/§6/§7 与状态；`README.md`；`docs/modules/16.web.md`；`docs/runbook/16-web-runbook.md`；`docs/changes/README.md`

---

## 7. 实测证据（开发后回填）

> 以下全部为**原始输出**（粘贴自终端，未转述、未美化）。

### E1 · 需求 2 现状取证：账号与口令**确实在数据库里**

```text
$ uv run python -m scripts.evidence_accounts
accounts 表行数 = 4

id  identifier      role                subject       status    password_hash
-----------------------------------------------------------------------------
5   admin           admin               employee#1    active    argon2$argon2id$…（共 103 字符）
6   13800000002     customer            customer#2    active    argon2$argon2id$…（共 103 字符）
7   13800000003     customer            customer#3    active    argon2$argon2id$…（共 103 字符）
8   employee        internal_employee   employee#2    active    argon2$argon2id$…（共 103 字符）

口令散列算法：全部为 Argon2id
明文口令行（必须为空）: []
唯一标识约束：4 个不同标识 / 4 行

VERDICT = PASS
EXIT=0
```

```text
$ curl.exe -s -i -X POST http://127.0.0.1:8000/api/auth/login -H "Content-Type: application/json" -d '{"identifier":"admin","password":"admin-demo-pass"}'
（正确口令）
HTTP/1.1 200 OK
subject=e1 role=admin entry=admin
permissions=admin,read:all_orders,read:finance,read:own,write:refund,write:ticket
access_token 长度=273 refresh_token 长度=275
```

```text
--- 错口令（账号 admin 存在）---
HTTP/1.1 401 Unauthorized
{"error":{"code":"invalid_credentials","message":"账号或口令不正确","detail":{}},"request_id":"08b9afc0cc8c4d60"}

--- 不存在的账号（no-such-user-xyz）---
HTTP/1.1 401 Unauthorized
{"error":{"code":"invalid_credentials","message":"账号或口令不正确","detail":{}},"request_id":"6f0e984259684df9"}
```

> ✅ **两者逐字相同**（除 request_id）—— 防枚举生效，4.17 通过。
> ⚠️ 取证脚本第一版的判据写成 `startswith("$argon2")`，而实际值是 `argon2$argon2id$…`
> （外层还有 `argon2$` 前缀），于是**把 4 行好数据全报成异常**（`VERDICT = FAIL`）。
> 修正为 `"argon2id" in hash` 后 PASS —— 这是「判据写错会把好数据报成坏数据」的现场，
> 记录在此以免后人重踩。

### E2 · 构建 / 类型 / 离线用例

```text
$ npm run build
   ▲ Next.js 15.5.27
   Creating an optimized production build ...
 ✓ Compiled successfully in 11.2s
   Linting and checking validity of types ...
   Generating static pages (10/10)
 ✓ Generating static pages (10/10)

Route (app)                                 Size  First Load JS
┌ ○ /                                    3.78 kB         128 kB
├ ○ /_not-found                            993 B         104 kB
├ ○ /admin                               3.91 kB         128 kB
├ ƒ /api/kb                                123 B         103 kB
├ ○ /citation                               3 kB         109 kB
├ ○ /internal                            5.05 kB         129 kB
└ ○ /login                               3.31 kB         118 kB
+ First Load JS shared by all             103 kB
BUILD_EXIT=0
```

> ✅ **5 个业务路由的 URL 一个都没变**（路由组 `(auth)` / `(app)` 不参与 URL）—— 4.1 / 4.13 通过。

```text
$ npm run typecheck
> tsc --noEmit
TYPECHECK_EXIT=0
```

```text
$ npm test
ℹ tests 150
ℹ pass 150
ℹ fail 0
ℹ skipped 0
ℹ duration_ms 507.2666
```

> ✅ 119（变更前）→ **150**（+31：`health.test.ts` 10 条 + `shell.test.ts` 21 条）—— 4.3 通过。

### E3 · 探活：断连 vs 正常（T14 / T15）

```text
########## T14：指向空闲端口 8999（模拟后端未连上）##########
$ NEXT_PUBLIC_API_BASE=http://127.0.0.1:8999 npx tsx scripts/probe-evidence.ts
探活取证：当前 apiBase() = http://127.0.0.1:8999
state    = unreachable
reported = ""
summary  = 后端没有响应：页面拿不到 /health（没有 HTTP 状态码）
payload = null（没读到 body）
detail  =
探活失败（TypeError: fetch failed）。这个症状**先别怀疑令牌**：令牌无效会得到 401 —— 那是一个能读到的 HTTP 响应，而不是一句 `Failed to fetch`。按下面顺序查这三件事：① 后端在不在：另开一个终端跑 `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`；② 地址对不对：当前前端来源 （当前页面来源） → 后端 http://127.0.0.1:8999（跨源时后端必须放通这个来源）；③ 是不是**跨源被浏览器拦掉**：打开 DevTools → Console，若有一行 CORS 提示，就在后端 .env 里加 `API_CORS_ORIGINS=（当前页面来源）` 并重启后端；另外看 Network 面板：请求若是 200 却仍报错，那就是 CORS（服务端没问题）。
EXIT=1
```

```text
########## T15：指向真后端 8000 ##########
$ NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000 npx tsx scripts/probe-evidence.ts
探活取证：当前 apiBase() = http://127.0.0.1:8000
state    = ok
reported = "ok"
summary  = 后端正常（env=dev version=0.1.0）
payload.status = ok
  component rate_limit = ok
  component observability = ok
  component cache = ok
  component postgres = ok
  component redis = ok
EXIT=0
```

> ✅ 4.14 / 4.15 通过：**同一个判据函数**在两种现场给出相反的结论（`unreachable` vs `ok`）。
>
> ⚠️ 过程中撞到一个**环境事实**（不是代码问题）：第一次跑 T15 时后端进程已经停了
> （`ECONNREFUSED`，8000 端口无监听），于是「正常态」也报 `unreachable`。
> 重启后端（`uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`）后复跑即 `ok`。
> **这恰好反证了判据是有效的**：它没有把「后端没起」误报成「正常」。

```text
=== 产物 CSS 里的关键规则（从 http://127.0.0.1:3100/_next/static/css/app/layout.css 抓取）===
  table.data        : True
  .cell-ellipsis    : True
  .cell-actions     : True
  .sidenav          : True
  .app-shell        : True
  .auth-shell       : True
  .health-banner    : True
  浅色底 #f6f7f4    : True
  旧深色底 #0b1020  : False
```

> ✅ 4.12 通过（省略号/操作列规则确实进了产物），且**旧深色底已从产物中消失**。

### E4 · 路由可达性与外壳分离（T13）

```text
$ （npm run dev 起在 3000）
/ -> HTTP 200
/login -> HTTP 200
/internal -> HTTP 200
/admin -> HTTP 200
/citation -> HTTP 200

=== /login 的 HTML ===
  含 sidenav      : False
  含 auth-shell   : True
  含 app-shell    : False
  含 开发模式     : False
  含 演示身份     : False
  含 登录按钮     : True

=== / 的 HTML ===
  含 app-shell    : True
  含 sidenav      : True
  含 ShopSage     : True
  含 topnav       : False
```

> ✅ 4.6 / 4.13 通过：登录页**根本没有侧边栏节点**（不是渲染后隐藏），
> 主界面有侧边栏且**没有**旧的 `topnav`。

```text
$ npx tsx scripts/sidebar-evidence.ts
后端 apiBase() = http://127.0.0.1:8000

PASS  客户（13800000002）
      role=customer  subject=c2  entry=customer
      权限点（2）= read:own, write:ticket
      侧边栏可见入口 = [customer]

PASS  普通员工（employee）
      role=internal_employee  subject=e2  entry=internal
      权限点（4）= read:all_orders, read:own, write:refund, write:ticket
      侧边栏可见入口 = [customer, internal]

PASS  管理员（admin）
      role=admin  subject=e1  entry=admin
      权限点（6）= admin, read:all_orders, read:finance, read:own, write:refund, write:ticket
      侧边栏可见入口 = [customer, internal, admin]

PASS  (app)/layout.tsx 挂了 <SideNav/>
EXIT=0
```

> ✅ 4.7 / 4.8 通过：**真令牌 → `GET /api/auth/me` → 真权限点 → 入口过滤** 整条链在真实后端上跑通，
> 三种角色看到三种入口集合。

### E5 · 静态判据（grep）

```text
=== 4.4：globals.css / layout.tsx 里的 data-theme ===
globals.css:11 / 13 / 17   ← 均为**注释**（说明为什么删掉 data-theme）
layout.tsx:16              ← 均为**注释**
（代码中无 data-theme 属性；shell.test.ts 去注释后断言「无匹配」）

=== 4.5：登录页旧内容 ===
login/page.tsx:8 / 9 / 14 / 15   ← 均为**注释**（「删掉了什么」的说明表）
（代码中无匹配）

=== 4.11：admin 的 className="table" ===
admin/page.tsx:311   ← **注释**（说明修的是什么）
admin/page.tsx:316   ← 实际代码：<table className="data">

=== TopNav 残留 ===
layout.tsx:17   ← **注释**
（无 <TopNav 或 import TopNav 的代码引用；组件文件已删除）
```

> ⚠️ **这些 grep 命中的全部是注释**，不是代码。本仓的模块注释**刻意**会引用被删掉的旧写法
> （说明「原来是什么、为什么改」），所以纯 `grep` 会把「已经改对了」误判成「没改」。
> 这正是 `tests/shell.test.ts` 里那个 `stripComments()` 辅助函数存在的理由
> —— 用例断言前先剥掉注释，否则连改对了都判不过（第一版就是这么误报的）。

### E6 · 三条主路径回归 + 越权对照（T18）

```text
$ uv run python -m scripts.evidence_paths
=== 路径 1：对客问答（customer）===
HTTP 200 flow_state=delivered route=data
answer: 我这边看不到物流轨迹，只能看到订单状态，所以暂时没法告诉你「到哪了」。

你名下有两条记录：一单已退款，一单已送达（金额一万二千多，支付宝付款）。如果这两单都不是你在问的，或者你想查具体某一单，麻烦把订单号发我，我再帮你核对。

物流详情这次我这边确实查不到，抱歉。

=== 路径 2：对内查询（employee）===
HTTP 200 flow_state=delivered route=analysis
answer: 结论先行：本次查询共返回 5 行状态汇总，合计 30 笔订单；delivered 19 笔（63.3%）为最高柱，其余五个状态各 2–3 笔，差异很小。

【柱状图】（1 个 █ = 1 笔；数据源：本次查询 5 行汇总）

delivered  ███████████████████  19  (63.3%)
refunded   ███                   3  (10.0%)
chart_spec: 有（bar）

=== 路径 3：管理台（admin 列账号）===
HTTP 200 账号数=4

=== 越权对照：customer 调管理台（预期 404）===
HTTP 404

=== 汇总 ===
PASS  对客问答                 HTTP 200
PASS  对内查询                 HTTP 200
PASS  管理台列账号               HTTP 200
PASS  越权被拒（404）            HTTP 404
EXIT=0
```

> ✅ 4.18 通过：三条主路径都返回**真实业务回答**（对客走了行级过滤、对内出了柱状图 spec），
> 且越权对照仍按后端口径 404。
>
> ⚠️ 取证脚本必须写成 Python 文件而不是 PowerShell 一行命令：**中文请求体经 PowerShell 传递会变乱码**
> （实测 `我的订单到哪了？` 到后端变成 `???????`，后端如实回答「问题内容为乱码」）。
> 那是 shell 编码问题、不是后端问题 —— 见 `scripts/evidence_paths.py` 的模块注释。

### E7 · 已知未验证项（诚实标注）

| # | 未验证的事 | 为什么 | 影响 |
|---|---|---|---|
| E7.1 | **浏览器里的实际渲染**（横幅出现在顶部、侧边栏底部布局、表格省略号的实际视觉效果） | 本仓没有 Playwright，Node 里没有 React/DOM；`shell.test.ts` 只做源码级结构断言 | 4.6–4.12 的「接线」已验证，但「看起来对不对」需人工看一眼 |
| E7.2 | `tests/live`（真链路套件，10 条） | 需前后端同时按固定配置在跑；本次用手工 HTTP 回归（E6）替代 | 见附录 A10 的既定取舍 |
| E7.3 | 色弱场景下「按钮（青绿）」与「成功徽章（绿）」的可区分性 | 需要色觉对比度工具 | 见 §8 遗留 L4 |
| E7.4 | 窄屏（≤860px）下侧边栏变顶部条、表格横向滚动的实际观感 | 同上（需浏览器） | 代码里已加 media query 与 `scroll-x`，未目视确认 |

---

## 8. 遗留与后续

> 本次**不做**但已发现的问题，一律记到 `issues/` 并在此链接，不许塞进本次范围。

- **L1**：`lib/session.ts` 的"三把钥匙互不覆盖"（同一浏览器可同时以客户与管理员身份登录）在**界面**上已无入口（2.1.3 删了登录态卡片）。存储能力仍在（2.2.4），但没有 UI 管理它。是否需要"切换身份"界面 → **待定**，属于后续变更。
- **L2**：删除登录页"开发模式"后，手粘令牌只能走 DevTools 的 `localStorage.setItem`（见 3.3）。若排障频率高，应补一个**独立的** `/dev/token` 页面（不进主侧边栏）→ 属于后续变更。
- **L3**：`web/public/diag.html` 是另一套独立的深浅色样式（`color-scheme: light dark`），本次未纳入统一主题 → 若要求"全站浅色"需一并处理，**当前需求未提**。
- **L4**：`--accent` 改成青绿后与 `--ok` 绿色系接近（见 3.1.4 的取舍），色弱用户下"按钮"与"成功徽章"的可区分性**未做实测**（需要色觉对比度工具）→ 若后续做无障碍验收，这里要复测。
- **L5**：`change_own_password`（`app/security/accounts.py:492`）**没有 HTTP 出口**，用户无法自助改密 → 属后续变更（2.2.3）。
- **L6**（开发中发现的**环境**问题，与代码无关）：本次开发期间，pwsh 一度对整个工作区（连 `docs/`、`web/`）写入被拒，
  而 `write` 工具正常。按 `diagnose-windows-sandbox-acl` 技能跑了一次诊断，
  结论是 `NOT_THIS_CLASS`（`FIXED=0`，**文件权限本身没有问题**，未做任何 ACL 改动），
  之后文件策略切到完全访问，写入恢复正常。报告留在 `E:\Desktop\DeepSeekHarness\ShopSage-acl-recovery\`（只读追溯用，可随时删除）。
- **L7**（开发中发现的**判据陷阱**）：`hook-wiring.test.ts` 硬编码了 `src/app/page.tsx` 路径，
  页面移入路由组后该文件在 import 期就 `ENOENT`，**整组用例一条都没跑**（`npm test` 报 `fail 1` 而不是逐条失败）。
  已修为 `(app)/page.tsx`。**教训**：源码级用例里的硬编码路径是脆的，移动文件时它是静默失效点。

---

## 附：假设与风险复述（含两轮澄清中"按推荐项"决定的条目）

| # | 决定 | 来源 | 风险 |
|---|---|---|---|
| A1 | 登录页只留账号+口令，②③④界面全删 | 澄清 Q1 选 A | 排障少一个界面入口（L2）；已给替代手段 |
| A2 | 统一外壳 + 左侧边栏 + 底部个人信息/权限状态；`/login` 独立布局 | 澄清 Q2 选 A | 需移动 4 个页面文件（T7） |
| A3 | 固定浅色护眼主题，移除深色分支与主题脚本 | 澄清 Q3 选 A | 失去深色偏好（3.2.2 已记为取舍） |
| A4 | 所有页面自动探活一次，失败顶部横幅，保留「重新探活」 | 澄清 Q4 选 A | 每次刷新多一次 `/health`（成本极低，见 3.2.4） |
| A5 | 管理台表格：补齐样式 + 长文本省略号 + 悬浮全文 + 窄屏横向滚动 | 澄清 Q5 选 A | 长文本默认看不全（需悬浮），已记为取舍 |
| A6 | 需求 2「账号密码落库」现状已满足，**本次不新增后端功能**，只在自测取证 | 澄清 Q6 选 A | **本次不加强安全策略**（无口令复杂度/锁定），若需要是另一次变更 |
| A7 | 权限状态优先 `/me`，失败回退本地并**标注是回退值** | 澄清 Q7 选 A | 多一次请求；回退值可能与后端不一致（已用界面标注缓解） |
| A8 | 用路由组 `(auth)` / `(app)` 做布局隔离 | 澄清 Q8 选 A | 需移动文件；URL 不变（已验 4.13） |
| A9 | 侧边栏：按权限过滤入口 + 底部个人信息/权限状态 + 退出登录 | 澄清 Q9 选 A | 无 |
| A10 | 自测范围：新增离线用例 + 全量 `npm test` + `typecheck` + `build` | 澄清 Q10 选 A | **不跑 `tests/live`**（需前后端同时在跑，见 §6 T13/T18 用手工替代） |

### 风险清单

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | 移动页面到路由组时漏改相对 import | 构建失败 | T7 后立刻 `npm run build`（4.1），不通过不往下走 |
| R2 | 探活判据只看 `response.ok` | `/health` 恒 200 → Postgres 挂了也显示"正常" | 判据抽成纯函数（2.1.9），用例**必须含 200+`status:"down"`**（4.10） |
| R3 | `SideNav` 里读 `localStorage` 造成 hydration 不一致 | 控制台警告 / 布局抖动 | 沿用 `TopNav` 已定的口径：`ready` 之前不渲染结论（`TopNav.tsx:41-47` 的同一手法） |
| R4 | 自动探活在 SSR 期执行 | 构建期报错 | 探活放在 `useEffect` 内（客户端），不在模块顶层调用 |
| R5 | 删除 `TopNav.tsx` 后仍有引用 | 构建失败 | T10 用全仓 grep 确认无引用后再删；`npm run build` 兜底 |
| R6 | 需求 2 被误认为"本次要写后端" | 范围蔓延 | 2.2.1 + A6 已显式排除；第 4 步只取证不改码 |
