# Next.js 前端实操手册（两个入口 / 流式渲染 / 溯源可视化 / 图表适配）

> 面向开发与运维的**操作指南**：第 16 步（web）怎么一步步做出来、每步怎么确认做对了、出问题怎么分诊。
> 设计原理与取舍见 [`docs/modules/16.web.md`](../modules/16.web.md)（8 段模板）；
> 本文只讲「怎么做、怎么验证」，格式与 [`9.api-runbook.md`](9.api-runbook.md) /
> [`15.mcp-runbook.md`](15.mcp-runbook.md) / [`17.docker-ops.md`](17.docker-ops.md) 保持一致。

> ✅ **状态（已执行，本文与实现同步）**：`web/` 下的 5 个页面/路由 + 4 个组件 + 6 个 `lib/` 模块 +
> 5 个离线测试文件 + 1 个真链路测试文件 + 1 个启动器均已落地，并在
> **Windows 开发机（Node v24.16.0 / npm 11.13.0）+ Next 15.5.27 + React 19.1.0 + echarts 5.6.0**、
> 后端 `uv run uvicorn app.main:app --port 8000`（VMware-Linux 上的 PostgreSQL 16.15 / pgvector + 本地
> `BAAI/bge-m3` / `BAAI/bge-reranker-base`）上跑通：
>
> | 范围 | 命令 | 实测结果 |
> |---|---|---|
> | 构建（验收第 1 条的判据） | `npm run build` | **✓ Compiled successfully in 22.0s**；9/9 静态页生成；6 条路由（`/`、`/internal`、`/login`、`/citation`、`/api/kb`、`/_not-found`） |
> | 类型检查 | `npm run typecheck` | **无输出（0 错）** |
> | 本步离线用例 | `npm test` | **65 passed / 0 failed**（5 个文件；0.22s） |
> | 本步真链路用例 | `node tests/run-live.mjs`（前端已在跑时加 `SHOPSAGE_SKIP_WEB=1`） | **9 passed / 0 failed**；两次实测：**41.9s**（后端刚热身）与 **81.9s**（后端刚重启，含模型冷加载） |
> | 页面可访问性 | `Invoke-WebRequest http://127.0.0.1:3000{/,\/internal,\/login,\/citation}` | 四条路由都 **HTTP 200**、title 为 `ShopSage · 智能助手` |
> | `/api/kb` 安全面 | 真链路用例里的一条 | 正常名 200；`../.env` → **400**；不带 `name` → 200 列清单 |
> | Python 全量回归 | `uv run pytest -q -m "not llm_live"` | **1617 passed, 50 skipped, 13 deselected, 6 errors**（140s） |
> | Python 全量回归（**本步之前**的基线） | 同上 | **1613 passed, 50 skipped, 13 deselected, 6 errors**（见 [`15.mcp-runbook.md`](15.mcp-runbook.md) 状态表） |
>
> ✅ 增量可逐项对上：`+4` 条用例里，3 条来自本步新增的占位符守卫
> （`tests/test_agents_data.py`：示例不许有尖括号 / 占位符会重试 / 一直给占位符会降级，
> 另有一条 4 条断言的识别器用例也在同一个 patch 里），
> 其余改动是**修既有用例的一个判据**（`tests/cache/test_cache_contracts.py` 换调用名，见 §10.1 坑 4）——
> `1617 − 1613 = 4` 与「新增 4 条、修 1 条判据」完全吻合。
>
> ⚠️ 6 个 error 是 `tests/test_config.py` 里用 `tmp_path` 的老问题（沙箱 `PermissionError: [WinError 5]`），
> **改动之前就是这个数**。

> 🎯 **本步的边界（先读，避免做重复功）**：
> 第 16 步交付的是**演示层**：两个入口（对客 / 对内）、流式渲染、溯源展示、图表渲染、角色分流。
> **不做的七件事**：① 不改后端接口契约（`app/api/` 一行未动 —— 前端按 `9.api-runbook.md` §4.1
> 的帧契约照抄）；② 不做登录接口（本仓没有它，演示用 `scripts/dev_token.py` 签令牌，见 §4.1）；
> ③ 不做 SSR 数据预取（会话与令牌都在 `localStorage`，页面必须客户端渲染）；④ 不做国际化 / 主题切换的
> 持久化后端（主题只写 `localStorage`）；⑤ 不做端到端浏览器自动化（没有 Playwright 依赖，
> 真链路证据用 Node 直接跑**同一份**客户端代码，见 §7.4）；⑥ 不做生产部署（`web/Dockerfile` 属第 19 步，
> `16.web.md` §6 第 5 条留到那时）；⑦ 不把本步挂进 CI（属第 2 步的 `16.infra.md` 第 19 步）。

> 🧪 **执行方式：用 npm 管前端依赖，用 `node --test` 跑测试**（不引 jest / vitest，理由见 §7.3）。
> ⚠️ 本机 npm 全局缓存在受限环境下不可写，涉及 `npx`/`npm` 的命令前面都先设一次
> `$env:npm_config_cache="$PWD\.npm-cache"`（与 [`5.llm-runbook.md`](5.llm-runbook.md) §1.1 同一类坑）。
> ⚠️ `npm install` 会因 **postinstall 脚本起子进程被沙箱拒绝**（`spawn EPERM`）而整包回滚，
> 必须加 `--ignore-scripts` —— 见 §10.1 坑 1，这不是「本机坏了」，是**必须知道的手法**。

> 🖥 **执行位置**：本步**全在 Windows 开发机**完成。
> 前端（Next）跑在 Windows 的 **3000** 端口；后端（FastAPI）跑在 Windows 的 **8000** 端口；
> 依赖栈（PostgreSQL/Redis/Prometheus/Grafana）在 VMware-Linux 上 —— ⚠️ **Linux 上的 Grafana 也占了 3000**，
> 但那是**另一台机器**的 3000，与本机前端不冲突（`http://127.0.0.1:3000` 永远指 Windows 自己）。
> ⚠️ 有**一条与第 14/15 步相同的前置条件顺序**：跑 Python 全量回归会把向量库清空，所以
> 「真溯源」用例要在**重灌语料之后**跑 —— 见 §1.1 第 ④ 条与 §10.1 坑 6。

---

## 0. 30 秒速查

| 我想… | 去哪 | 命令 / 做法 |
|---|---|---|
| **把前端跑起来** | Windows | **§1.4（`npm run dev` → http://127.0.0.1:3000）** |
| **签一个能粘进浏览器的令牌** | Windows | **§4.1（`uv run python -m scripts.dev_token`）** |
| **看对客页的流式与溯源** | Windows | **§5（http://127.0.0.1:3000 → 问「7天无理由退货的运费由谁承担？」）** |
| **看对内页的表格与图表** | Windows | **§5.3（/internal → 问「按订单状态统计订单数量」）** |
| 只跑前端用例（不连后端） | Windows | **§7.2（`cd web; npm test`）** |
| **连真后端跑一遍端到端** | Windows | **§7.4（`node tests/run-live.mjs`）** |
| 构建通过（验收第 1 条） | Windows | `cd web; npm run build` |
| 「构建报 `spawn EPERM`」 | Windows | §10.1 坑 2（沙箱下 `next build/dev` 必须放宽文件访问） |
| 「`npm install` 一半回滚」 | Windows | §10.1 坑 1（`--ignore-scripts`） |
| 「页面能开，但一问就白屏 / 什么都不发生」 | Windows | §10.2 第 1 条（先看浏览器 Network/Console，再看后端日志） |
| 「WebSocket 连不上，控制台一行握手失败」 | Windows | §10.1 坑 7（子协议少写了 `shopsage.v1`） |
| 「检索回 0 条 → 回答没有来源」 | Windows | §10.1 坑 6（向量库被全量回归清空了） |
| 「图表出不来」 | Windows | §10.1 坑 5（Text-to-SQL 把绑定参数写成占位符） |
| 「页面昨天还好，今天 500 且报 `React Client Manifest`」 | Windows | §10.1 坑 10（`next build` 与 `next dev` 同时在跑，`.next` 被覆盖） |
| 回填验收证据 | Windows | §11 → `docs/modules/16.web.md` §8 |

三条铁律：

1. **前端不做业务判断**。`web/` 里出现「这条数据他能不能看」「这句话能不能答」的判断就是越界 ——
   它只做四件事：**分流入口 → 调接口 → 渲染帧 → 展示出处**（§6）。
   角色表只是**界面边界**（不显示点不动的按钮），真正的拦截在后端
   （对客行级过滤、对内 RBAC，`app/security/`）。
2. **契约只有一份，且来自后端**。`web/src/lib/contracts.ts` 是 `app/api/schemas.py` 的**镜像**：
   `flow_state` / 帧 `event` / 关闭码 / 子协议名的字面量逐字抄过来，
   所有来自网络的数据都先过 `parseFrame()` / `parseChatResponse()`
   （在组件里 `as Xxx` 只是让编译器闭嘴，运行期该崩还是崩）。
3. **令牌、`user_id`、`thread_id` 都不是前端能造的东西**。令牌来自服务端签发；
   `thread_id` 一律用响应里回传的那个；请求体里**永不**出现 `user_id` / `role`
   （后端 `ChatRequest` 是 `extra="forbid"`，多写一个键就是 422）。

另一条（本步最值钱的认知）：**「流式」与「图表」在后端是两条不同的出口，前端必须选一条**。
WebSocket 的 `answer` 帧只带一个布尔 `chart`（`app/api/chat.py` 刻意不把 spec 塞进帧），
`chart_spec` 只出现在 HTTP 响应里。所以对客页走 WS（要首字快、要可中断），
对内页走 HTTP（要拿到图表本体）—— 这不是偷懒，是**契约决定的**（§2.3）。

---

## 1. 环境与前提

### 1.0 本步的链路（两个入口 + 一条流式 + 一条报表）

```
       浏览器（同一份构建产物，按 localStorage 里的令牌分流）
   ┌──────────────┴───────────────┐
   │                              │
   ▼ 对客 /（WS 流式）              ▼ 对内 /internal（HTTP 报表）
  openChatStream()                postChat()
   │  Sec-WebSocket-Protocol:      │  POST /api/chat
   │  shopsage.v1, bearer.<JWT>    │  Authorization: Bearer <JWT>
   └──────────────┬───────────────┘
                  ▼
        Next 前端（3000，只做展示）
                  │   同源调用 /api/kb（读语料原文，白名单 + 限大小）
                  ▼
   FastAPI（8000，第 9 步）：鉴权 → 限流 → 转交图 → 格式化
                  ▼
   第 8 步的图（guardrail → router → data / rag / analysis → synthesis）
```

**每个环节「前端能保证什么 / 保证不了什么」**：

| 前端环节 | 能保证 | 保证不了（要靠别的环节） |
|---|---|---|
| 角色分流 | 客户看不到「对内查询」入口 | 客户**绕过前端**去调接口 → 行级过滤 + RBAC（`app/security/`） |
| 子协议带令牌 | 令牌不落在 URL / access log 里 | 令牌本身的安全性 → 短 `exp` + 刷新令牌（第 4 步） |
| 帧解析 `parseFrame` | 未知 event / 非法 JSON 有明确报错，不白屏 | 帧**内容**的合规性 → 后端 `FRAME_SAFE_KEYS` 白名单 |
| 出处展示 `/citation` | 出处能解析、能打开原文 | 「这条政策是不是最新」→ 语料版本（`rag_documents`） |
| 图表适配 `toEChartsOption` | spec → 图的映射不含业务规则（缺值不补 0、点数不扩） | 「该不该画」→ `analysis` 节点（形状不合适时置空 spec） |
| 错误信封解析 | 401 → 去登录、429 → 退避、其余 → 展示 | 「为什么被拒」→ 后端 `error.code`（前端不猜） |

### 1.1 前置条件（第 9 步的地基 + 本步的运行时）

```powershell
# ── 执行位置：Windows 开发机，仓库根 ──
$env:UV_CACHE_DIR = "$PWD\.uv-cache"          # 本机 uv 全局缓存不可写
$env:npm_config_cache = "$PWD\.npm-cache"     # 本机 npm 全局缓存不可写

# ① 后端在跑（本步的一切都依赖它）
Invoke-RestMethod http://127.0.0.1:8000/health | Select-Object status, env, version
# 实测：status=ok env=dev version=0.1.0
# ⚠️ 起它的命令：uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
#    **不要加 --reload**：本机实测 --reload 起不来（watchfiles 要起子进程）；
#    代价是「改了后端代码必须手动重启」，见 §10.1 坑 8。

# ② 依赖栈可达（前端本身不需要，但「真溯源」与「对内图表」需要）
Test-NetConnection 192.168.167.130 -Port 5432 -InformationLevel Quiet   # True
Test-NetConnection 192.168.167.130 -Port 6379 -InformationLevel Quiet   # True

# ③ 前端依赖已装（`--ignore-scripts` 是必须的，见 §10.1 坑 1）
cd web
npm install --ignore-scripts --no-audit --no-fund
node -e "console.log('next', require('./node_modules/next/package.json').version)"
# 实测：next 15.5.27

# ④ 语料已灌进 pgvector（**真溯源用例与演示场景的前置条件**）
cd ..
uv run python -m scripts.probe_rag_sources
# 实测：rag_chunks 总数=61 活跃=61（8 份文档）
# ⚠️ 输出 0 时先跑：uv run python -m scripts.load_knowledge_base（约 56~82s）
#    跑完 Python 全量回归之后**必然**是 0 —— 见 §10.1 坑 6。
```

### 1.2 本步的文件清单与职责

```
web/
├─ package.json            # 7 条脚本：dev / build / start / lint / typecheck / test / test:live
├─ next.config.ts          # 只放「构建期」决策（本步实际只开了 reactStrictMode）
├─ tsconfig.json           # allowImportingTsExtensions（见 §10.1 坑 3）
├─ src/lib/
│  ├─ contracts.ts         # ★镜像 `app/api/schemas.py`：帧/信封/关闭码/子协议 + parse*
│  ├─ stream.ts            # ★reducer（纯函数）+ 连接层（薄壳）；两半分开是为了可离线测
│  ├─ chart.ts             # ★`chart_spec` → ECharts option / 表格视图（纯函数）
│  ├─ api.ts               # HTTP 客户端：一套错误信封解析 + 令牌注入
│  ├─ auth.ts              # 登录态、角色→权限表（**界面边界**）、会话 id、令牌解码
│  └─ config.ts            # 唯一一处读 `NEXT_PUBLIC_*` 的地方（apiBase / streamUrl）
├─ src/components/
│  ├─ useChatStream.ts     # 把 WS 连接与消息列表缝起来（hook）
│  ├─ MessageList.tsx      # 气泡 + 出处（纯展示）
│  ├─ StreamStatus.tsx     # 骨架语 / 走过的节点 / 首帧耗时
│  ├─ Composer.tsx         # 输入框（长度上限与后端一致 + Ctrl/⌘+Enter）
│  └─ EChart.tsx           # ECharts 的最小 React 包装（动态 import + dispose）
├─ src/app/
│  ├─ layout.tsx           # 顶栏 / 主题脚本 / `lang="zh-CN"`
│  ├─ globals.css          # CSS 变量主题（不引 tailwindcss-animate，理由见文件注释）
│  ├─ page.tsx             # 对客入口（`/`，WS 流式）
│  ├─ internal/page.tsx    # 对内入口（`/internal`，HTTP 报表 + 图表）
│  ├─ login/page.tsx       # 登录 / 切角色（粘贴令牌）
│  ├─ citation/page.tsx    # 出处页（`/citation?q=...`，解析出处 + 读原文）
│  └─ api/kb/route.ts      # 只读语料路由（白名单 + 路径校验 + 大小上限）
└─ tests/
   ├─ stream.test.ts       # 帧时序（最值钱的一组）
   ├─ transport.test.ts    # 子协议 / 假 socket / 错误信封
   ├─ chart.test.ts        # 图表适配（含 table / 空数据 / 截断警告）
   ├─ citations.test.ts    # 出处串解析（含**实测形状** `docs/kb/退货政策.md #<hex>`）
   ├─ auth.test.ts         # 角色分流 / 令牌过期 / 会话 id 字符集
   ├─ live/live.test.ts    # 真链路（真后端 + 真前端；缺令牌时整组 skip）
   └─ run-live.mjs         # 真链路启动器（签令牌 + 起前端 + 跑用例）
```

仓库根新增的两个脚本（都是**给本步用的工具**，不是业务代码）：

| 文件 | 作用 | 为什么值得进仓库 |
|---|---|---|
| `scripts/dev_token.py` | 签发演示用 JWT（`--role` / `--user-id` / `--kind refresh` / `--json`） | 本仓没有登录接口，而浏览器要真令牌才能连后端；同时它是「401 分支怎么试」的唯一入口（§4.1） |
| `scripts/probe_rag_sources.py` | 打印 `rag_chunks` 的来源文档与片段 id 形状 | 前端的出处解析必须知道**真实形状**（`docs/kb/退货政策.md #c00bcbd…`），靠猜会写出错一个目录名的解析器（§4.3） |
| `scripts/probe_internal_query.py` | 用内部角色问 5 种「报表句式」，看哪一个出图 | 「出不出图」取决于 Text-to-SQL 的写法，人工筛句式比在页面上反复试快得多（§6.4） |

### 1.3 数据契约一览（前端要用到的四个出口）

前端一共只用四个出口，**一个都不多**（`16.web.md` §2 要求「调后端 API」）：

| 出口 | 谁在调 | 关键字段 | 前端拿它做什么 |
|---|---|---|---|
| `WS /api/chat/stream` | 对客页 | `skeleton` / `node` / `answer` / `done` / `error` 五类帧 | 打字机式进度 + 回答 + 出处 + 审批闸门 |
| `POST /api/chat` | 对内页 | `chart_spec` / `answer` / `debug` | 报表表格 + 图表 + SQL 回显 |
| `POST /api/chat/approve` | 对客页（挂起时） | `flow_state` / `answer` | 「批准 / 拒绝」两个按钮 |
| `GET /health` | 登录页探活 | `status` / `components` | 「后端在不在」的一句话 |

外加**一条自己的路由**：`GET /api/kb?name=<文档名>`（Next 侧，只读语料原文）——
它不是后端接口，是为「出处能点开」补的最后一公里（§4.3）。

### 1.4 两条起前端的命令

```powershell
# ── 执行位置：web/ ──
# ① 开发模式（改前端即生效，本步日常用这条）
npm run dev
# 实测：
#    ▲ Next.js 15.5.27
#    - Local:        http://localhost:3000
#    - Network:      http://192.168.167.156:3000
#    ✓ Ready in 4.7s

# ② 生产模式（构建 + 起；验收「构建通过」用这条）
npm run build      # ✓ Compiled successfully in 22.0s（9/9 静态页）
npm start          # http://localhost:3000

# ③ 指向别的后端（默认 http://127.0.0.1:8000）
$env:NEXT_PUBLIC_API_BASE = "http://192.168.167.156:8000"; npm run dev
```

> ⚠️ **`NEXT_PUBLIC_API_BASE` 是构建期变量**（`NEXT_PUBLIC_*` 会被字面替换进客户端包），
> 所以「改了地址要重启 `npm run dev`（生产模式要重新 `npm run build`）」——
> 与后端 `.env` 的「重启进程就生效」不同，这一点很容易被误解成「配置没生效」。
> 唯一读它的地方是 `src/lib/config.ts`（前端也守「配置取值只有一处」）。

---

## 2. 三个入口：我该敲哪条命令

### 2.1 一张表看清

| 目标 | 命令 | 期望 |
|---|---|---|
| 看页面 | `npm run dev` → 浏览器开 `http://127.0.0.1:3000` | 顶栏 + 「对客问答」/「对内查询」/「登录 / 切角色」 |
| 看真链路的**自动证据** | `node tests/run-live.mjs` | 9 passed（会自动签令牌；前端没起时它会起） |
| 看**离线**证据 | `npm test` | 65 passed（不连任何后端） |

### 2.2 两个「入口」是什么，以及为什么不是两套前端

`16.web.md` §4 第 1 条要的是「两个入口靠角色切换，而非两套前端」。本步的落法是：

* **同一个构建产物**里的两条路由（`/` 与 `/internal`）；
* 分流依据是**令牌里的 `role`**（`src/lib/auth.ts::canUseInternal`，判据与后端
  `Actor.internal` 一致：能不能读全站订单）；
* 两把钥匙分开放（`shopsage.token.customer` / `shopsage.token.internal`），
  所以**同一个浏览器可以同时扮演客户与员工** —— 这正是验收第 3 条要能演示的东西。

### 2.3 为什么对客走 WS、对内走 HTTP（**契约决定的，不是偏好**）

```text
WS 的 answer 帧（app/api/chat.py::_stream_turn）：
    {"text": ..., "done": true, "degraded": ..., "citations": [...], "chart": true/false}
                                                                  ↑ 只有布尔，没有 spec

HTTP 的响应信封（app/api/chat.py::build_response）：
    {"answer": ..., "chart_spec": {...}, "citations": [...], "debug": {...}}
                      ↑ 图表本体在这里
```

所以：

| 页面 | 要的东西 | 走哪条 | 代价 |
|---|---|---|---|
| `/` 对客 | 首字快、逐段进度、可中断 | `WS /api/chat/stream` | 拿不到 `chart_spec`（对客也不需要图表） |
| `/internal` 对内 | 表格 + 图表 + 可审计的 spec | `POST /api/chat` | 没有流式（要等整轮跑完） |

**要「图表 + 流式」同时成立，必须改后端帧契约**（在 `answer` 帧里带 spec，或新增一类帧）——
那超出第 16 步的边界。所以这里把它记成一条**接线点**（§9），而不是偷偷在前端拼一个假的。

---

## 3. 实现：五个决定怎么落成代码

### 3.1 契约镜像：`src/lib/contracts.ts`

镜像的含义是**逐字抄 + 只抄一份**：

```ts
export type FlowState = "delivered" | "blocked" | "approval_required";   // app/api/schemas.py
export type FrameEvent = "skeleton" | "node" | "answer" | "done" | "error";
export const WS_SUBPROTOCOL = "shopsage.v1";
export const WS_TOKEN_PREFIX = "bearer.";
export const WS_CLOSE_UNAUTHORIZED = 4401;   // 4401 / 4429 / 4400
```

三条纪律：

1. **所有来自网络的数据都先过 `parseFrame()` / `parseChatResponse()`**：
   它们做「形状不对就抛 `ContractError`（带原始值）」。组件里写 `as Frame` 会让编译器闭嘴，
   但运行期 `frame.data.xxx` 该崩还是崩，而且崩在渲染里 = 白屏；
2. **未知 `event` 要报错**，不要忽略：后端加了新帧而前端没跟上时，
   「忽略」表现为「渲染少了一段」，而「报错」能立刻说出是哪个 event（§7.1 有用例）；
3. **镜像是有代价的**：后端契约变了前端要跟着改。这是刻意的取舍 ——
   本步要的是「一眼看出前后端是不是同一份」，而不是现在就上 `openapi-typescript` 代码生成（第 19 步之后再说）。

### 3.2 流式：reducer 与连接层分开（`src/lib/stream.ts`）

```text
reduceStream(state, frame)   ← 纯函数：帧序列 → 界面状态（零依赖、可穷举）
        ▲
        │  喂帧
openChatStream({token, onState})  ← 薄壳：拼子协议 / 连 / 把帧喂给 reducer / 推状态
```

为什么拆开（**本步最值钱的一条工程决定**）：「骨架 → 节点 → 回答 → 收尾」这条时序必须能被**离线**测。
揉在一起就只能靠「起浏览器 + 真后端 + 真模型」去测，红的时候你分不清是协议错了还是环境不对。
拆开之后 `web/tests/stream.test.ts` 直接喂一串帧进去断言状态（0.2s 跑完 15 条）。

三处与后端对齐的细节：

| 细节 | 为什么 |
|---|---|
| `skeleton` 可能来**两条**（accept 时一条、路由确定后一条） | 界面取最后一条；同文案不重复追加（避免刷一串一样的话） |
| `node` 帧的 `delta` **只取 `route`**，其余不落状态 | `delta` 是后端 `FRAME_SAFE_KEYS` 白名单裁剪过的；前端再抄一份判断，等于把「哪些字段能出网」搬到了前端 |
| `error` 帧**不结束这一轮** | 后端对「这条消息不合法」只回一条错误帧、连接继续用（`app/api/chat.py` 的消息循环）；把它当成 failed 会让用户「改一句话再发」也发不出去 |

### 3.3 图表适配：spec → option 的**唯一**一处（`src/lib/chart.ts`）

`16.web.md` §5 第 2 条要的是「后端返回结构化 spec，前端渲染，换绘图库不改后端」。
本步的落法：`toEChartsOption(spec)` 是唯一知道 ECharts 长什么样**唯一的**地方。

spec 里三件事前端**不许自作主张**：

| 后端已决定 | 前端不许 | 理由 |
|---|---|---|
| `meta.truncated_points`（点数已截断） | 「顺手把剩下的画上」 | 图表不是数据导出通道（`app/tools/chart.py` 的模块注释） |
| 空值是 `null`（**不补 0**） | 填 0 | NULL 与 0 是两件事；折线断开才是诚实的画法（`connectNulls: false`） |
| `meta.warnings`（含空数据 / 空值数） | 吞掉不显示 | 那是后端刻意的「有声音」设计，前端吞掉等于让用户以为数据是全的 |

`type: "table"` 在这条路径上**抛错**（而不是静默画成柱状图），由页面改走 HTML 表格 ——
不然「表格」这个类型的语义就在两个地方不一样了。

### 3.4 角色分流：界面边界，不是安全边界（`src/lib/auth.ts`）

前端只做一件事：**决定渲染哪个界面**。判据是令牌 payload 里的 `role`
（⚠️ `decodeJwtPayload()` **不验签** —— 浏览器里没有密钥，也不该有）：

| 前端用 `role` 做什么 | 前端**不**用它做什么 |
|---|---|
| 决定「默认进 / 还是 /internal」 | 决定「这行数据能不能返回给这个人」 |
| 决定「显示不显示对内入口的按钮」 | 决定「这次查询要不要加行级过滤」 |
| 决定演示身份卡片的提示文案 | 决定「有没有 `read:finance` 权限」（那在后端 RBAC） |

**为什么敢用它渲染**：界面显示错了的后果是「看到自己点不动的按钮」；
真正的越权（看到别人的订单）在后端被行级过滤 + RBAC 拦住（第 4/9 步已覆盖）。
反过来说：**把这一层当安全边界用是错的**（§10.1 坑 9）。

### 3.5 出处能点开：为什么要一条自己的 `/api/kb` 路由

`16.web.md` §4 第 3 条要求「展示『来源文档 + 片段』，可点击跳转」。而后端的 `citations`
只给出处串（`docs/kb/退货政策.md #c00bcbd2da0b2ebe6`），**不返回片段正文**
（第 9 步契约里没有「按出处取原文」的接口）。差的那一步就是这条路由：

```
回答底部的出处串 ──parseCitation──▶ /citation?q=...（解析出文档名）
                                      │
                                      ▼
                        GET /api/kb?name=退货政策.md（Next 侧只读 docs/kb/）
```

三条安全口径（缺一不可）：**只允许一层文件名**（无路径分隔符、无 `..`、不以点开头）、
**扩展名白名单** `.md/.txt/.html`、**大小上限 256KB**，并且解析之后再校验一次真实路径仍在
`docs/kb` 之下（真链路用例里有一条 `../.env` → 400，§7.4）。

---

## 4. 身份：令牌从哪来，怎么进前端

### 4.1 签一个令牌（本仓没有登录接口）

```powershell
# ── 执行位置：仓库根 ──
uv run python -m scripts.dev_token --role internal_employee --user-id 7
# 实测输出：
#   角色      = internal_employee（权限点：read:own, read:all_orders, write:ticket, write:refund）
#   user_id   = 7（行级过滤的依据：只能查自己的数据）
#   类型/有效期 = access / 1800s
#   自检      = parse_token → user_id=7 role=internal_employee type=access
#
#   令牌（粘到 http://localhost:3000/login）：
#   eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9….

# 机器可读（给真链路启动器用）
uv run python -m scripts.dev_token --json --role customer --user-id 2

# 故意签一个刷新令牌：用来试「401 分支」与「前端提示令牌类型不对」
uv run python -m scripts.dev_token --kind refresh

# 故意签一个已过期的：用来试 4401（WebSocket 关闭码）
uv run python -m scripts.dev_token --ttl -5
```

**为什么不做登录接口**：`16.web.md` §6 第 4 条写的是「接 JWT 登录」，但本仓第 9 步只交付了
「令牌怎么被验」（`app/security/auth.py::parse_token`）——签发工具一直只被测试与演示脚本使用。
所以这一步交付的是**真实链路上最短的那一段**（签发 → 粘贴 → 后续请求带 `Authorization`），
而不是自造一个不需要密码的登录端点。

⚠️ 三条纪律：

1. 脚本**只在 `ENV=dev` 下有意义**（生产环境拿不到令牌是对的）；
2. 后端进程与脚本必须**同一份 `JWT_SECRET`**（没配时用开发默认密钥，所以两边都"没配"也能对上；
   一边配了一边没配，症状是「粘进去一堆 401」）；
3. 令牌里**不带任何权限声明** —— 权限一律由后端按 `role` 查表（`app/security/rbac.py`），
   令牌只回答「你是谁」。

### 4.2 令牌怎么进浏览器

```text
登录页（/login）
  ① 粘贴令牌 → 前端只解码 payload（看 sub / role / exp / type，**不验签**）
  ② 校验三件事：能解出三段？ type=access？ 没过期？   ← 三层都过了才写存储
  ③ 写入 localStorage：shopsage.token.customer / shopsage.token.internal（**按入口分开放**）
        ▼
后续页面：readSession(entry) → 过期 / 类型不对 / 坏令牌 → 视为未登录并**顺手清掉**
```

⚠️ 已知取舍（写在这里而不是藏着）：**令牌在 `localStorage`，因此可被同源 JS 读到**。
演示/开发阶段可以接受；生产形态应当换成 httpOnly Cookie + BFF 代理（前端只持有会话 Cookie），
那是第 19 步的部署话题（§9 接线点）。本步因此**不把令牌写进 URL**、也**不放进任何日志**。

### 4.3 Cookie 不是唯一选项：出处串本身也要「防跳错」

出处串的**真实形状**（实测，来自 `rag_chunks`）：

```text
docs/kb/退货政策.md #c00bcbd2da0b2ebe6
└── 目录前缀（后端存的是相对仓库根的路径）   └── chunk_id 是 16 位 hex，不是 "c12"
```

`parseCitation()` 因此做三件事：拆出文档 → 取 basename（**正反斜杠都处理**）→ 页码/片段各自成字段。
解析失败时**保留原文**（绝不返回空文档名）：出处解析不出来是展示层的小问题，
让用户看不到来源是大问题（§7.2 有用例）。

---

## 5. 手工试（最常用的三步）

### 5.1 起两个进程

```powershell
# 终端 1（仓库根）：后端
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

# 终端 2（web/）：前端
npm run dev
```

### 5.2 对客页：流式 + 溯源 + 审批

1. 浏览器开 `http://127.0.0.1:3000/login` → 粘贴客户令牌 → 保存到「对客入口」→ 点「打开对客页」；
2. 问 **「7天无理由退货的运费由谁承担？」**（这一句实测稳定走知识库并带出处，§6.5）：
   * 先出现虚线框「正在理解你的问题…」+ 跳动的小点（**首帧**）；
   * 节点徽章依次出现（`guardrail_input` → `router` → `rag` → `synthesis` → `guardrail_output`）；
   * 回答落定后，底部出现「来源 3 条」→ 点开看到 `退货政策.md` + `#<片段 id>` → 点击进 `/citation` 读到原文；
   * 状态条右侧显示「首帧 xxx ms（服务端自报）」。
3. 问 **「帮我申请一笔退款」** → 出现黄色面板「需要人工确认」+ 「批准并继续 / 拒绝」两个按钮：
   * 点「批准并继续」→ 新增一条 assistant 气泡（内容来自 `POST /api/chat/approve`）；
   * 直接点「拒绝」→ 气泡显示「按未执行处理」的话术（`flow_state=blocked`）。

### 5.3 对内页：表格 + 图表 + SQL

1. `/login` → 粘贴**内部角色**令牌（`--role internal_employee --user-id 7`）→ 保存到「对内入口」；
2. 打开 `http://127.0.0.1:3000/internal`；
3. 点建议按钮 **「按订单状态统计订单数量」**（或直接问这一句）→
   出现柱状图 + 「显示数据表」+ 「查看 spec（JSON）」两个开关；
4. 想让 SQL 也显示出来：后端 `.env` 加 `API_DEBUG_PAYLOAD=1` 并**重启后端**，
   再问一次 → 页面底部出现「本轮执行的 SQL」。

### 5.4 只跑本模块的用例

```powershell
cd web
npm test                 # 离线 65 条（0.2s）
npm run typecheck        # 0 错
npm run build            # 构建通过
node tests/run-live.mjs  # 真链路 9 条（需要后端在跑）
```

---

## 6. 四个验收口径怎么测

### 6.1 验收第 1 条：两个入口可用 + 流式正常

* **入口**：`GET /` 与 `GET /internal` 都 200（§11 的自查命令里有四条路由的实测）；
* **流式**：真链路用例「真流式」断言三件事 ——
  ① 帧序号从 1 **连续递增**（丢帧只可能来自本地逻辑，`seq` 就是为它准备的）；
  ② 节点帧里出现过 `router`；③ 服务端自报 `first_frame_ms < 1000`（B2）。

### 6.2 验收第 2 条：RAG 回答带溯源

判据写成「**闭环**」而不是「有串就行」：

```text
answer 帧的 citations 非空
   → parseCitation 能解析出文档名与 chunk_id（形状 ^[0-9a-f]{6,}$）
   → GET /api/kb?name=<文档名> 真的返回 200 且原文非空（点开不是 404）
```

### 6.3 验收第 3 条：角色权限在前端正确分流

* 离线：`tests/auth.test.ts` 断言 `canUseInternal` 的四个角色 + 演示身份卡片里的 role 都在权限表里；
* 真链路：客户令牌问「查全站所有订单」→ 前端不崩、后端如实拦住或降级（`flow_state` 非空、有回答）；
* **后端兜底**这一半由第 4/9 步的用例保证（本步不动它）。

### 6.4 验收第 4 条：对内报表可出图表（以及「出不来」怎么判）

```powershell
# 人工筛句式：5 种问法一次性跑完，看哪些出图
uv run python -m scripts.probe_internal_query
```

实测（修复绑定参数坑之后）：

| 问法 | 结果 |
|---|---|
| 各个订单状态的订单数量，用柱状图 | ✅ `bar` / 5 点 / `x=status` / `series=order_count` |
| 按订单状态统计订单数量 | ✅ 同上 |
| 统计每种订单状态各有多少笔订单，画成柱状图 | ✅ 同上 |
| 最近 30 天每天的订单金额趋势，用折线图 | ✅ `bar` / 5 点 / `x=day` / `series=amount`（模型选了柱状图，不是折线） |
| 销售额最高的 10 个商品，用柱状图 | ✅ `bar` / 10 点 / `x=name` / `series=product_id` |

⚠️ 出图的**真正前提**是「`data` 节点真的查回了行」。修 §10.1 坑 5 之前，上面五句**一句都出不来**，
而界面上的表现只是「没有图表」—— 所以排查顺序要**先看后端日志里有没有 `sql_query` 的 tool_error**，
再看前端（§10.2）。

### 6.5 一句「问法」能让用例红：知识库问题的非确定性

同一件事（溯源）两种问法的实测差异：

| 问法 | 实测 |
|---|---|
| 7天无理由退货的运费由谁承担？ | `route=rag`、3 条出处（**稳定**，用它做用例） |
| 退货政策是怎么规定的？ | 有时也走 `rag`，但检索回 0 条 → 无出处（**不稳定**） |

这不是前端的问题：它落在「问法 → 路由 → 检索 → 合成」这条链上（属第 7/10/14 步）。
真链路用例因此固定用**稳定的那一句**，并在注释里写明为什么 ——
用例要钉的是前端链路，不是模型的随机性（§7.4）。

---

## 7. 测试覆盖（本步的验收口径）

### 7.1 三层证据，各自回答不同的问题

| 层 | 文件 | 条数 | 回答的问题 |
|---|---|---|---|
| **纯逻辑**（离线） | `tests/stream.test.ts`（15）/ `citations`（9）/ `chart`（11）/ `auth`（14）/ `transport`（16） | **65** | 「帧序列意味着什么」「出处怎么拆」「spec 怎么画」「角色怎么分流」「子协议发了什么」 |
| **真链路** | `tests/live/live.test.ts` | **9** | 「我们抄的契约是真的吗」：真 WS 帧序列 / 真出处 / 真 `chart_spec` / 4401 / `/api/kb` 越界拦截 |
| **构建** | `npm run build` | — | 「能不能出产物」：9/9 静态页、6 条路由、0 类型错 |

### 7.2 离线用例：一条命令，不连任何后端

```powershell
cd web
npm test
# 实测：tests 65 / pass 65 / fail 0 / duration_ms 218
```

### 7.3 用例的三条纪律（沿用前几步的口径）

1. **不测实现细节，测契约**：断言的是「发出的子协议是什么」「缺值时图上是不是 `null`」
   「过期令牌会不会被清掉」，而不是「函数被调了几次」；
2. **零依赖优先**：65 条离线用例不连库、不连模型、不起浏览器，跑完 0.2s ——
   所以它们可以被反复跑（真链路那 9 条要 42s，且有模型成本）；
3. **两种证据都要**：离线用例用的是**抄来的** fixture，抄错了会「过了但真链路还是坏的」；
   所以真链路用例拿真数据过一遍**同一个**函数（§7.4）。

### 7.4 为什么真链路用例用**同一份客户端代码**，以及为什么要重试

* **同一份代码**：真链路用例直接 `import { openChatStream } from "../../src/lib/stream.ts"` ——
  浏览器里跑的就是它。另写一个「测试专用 WebSocket 客户端」看起来很干净，
  但它会因为写得跟真客户端不一样而给出**虚假的安全感**；
* **重试**：`chart_spec` 那条用例最多试 3 次。理由写在用例注释里：
  出图链路上有一段非确定性（Text-to-SQL），而这条用例要断言的是**前端适配层**，
  不是「模型今天稳不稳」 —— 把模型的随机失败算成前端用例的红，最后会让人无视这组用例，
  那比没有用例更糟。

### 7.5 收尾后确认演示库状态

```powershell
uv run python -m scripts.probe_rag_sources   # 实测：61 活跃片段 / 8 份文档
uv run python -m scripts.db_check            # users 8 / orders 30 / products 20 …
```

---

## 8. 与后续步骤的接线点（本步留好的接缝）

| 接缝 | 现在什么样 | 下一步怎么接 |
|---|---|---|
| 逐 token 增量帧 | `answer` 帧是整段（后端 `done: true` 注释已声明） | 第 11 步在 `answer` 前插入增量帧；前端按 `event` 分支即可（`reduceStream` 加一个 case） |
| 图表 + 流式同时要 | 对内页走 HTTP（拿 spec），对客页走 WS（拿流式） | 改后端帧契约（`answer` 帧带 spec 或新增帧），前端换 `useChatStream` 的收尾分支 |
| 登录 | 粘令牌（`scripts/dev_token.py`） | 生产：httpOnly Cookie + BFF 代理；本步的 `lib/api.ts` 是唯一改注入点的地方 |
| 生产部署 | `npm run build` 产物 + `npm start` | 第 19 步写 `web/Dockerfile`（多阶段 + `output: "standalone"`）+ 接生产 compose |
| 端到端浏览器自动化 | 无（真链路用 Node 跑客户端代码） | 需要截图/点击回归时加 Playwright（届时把那 9 条真链路用例平移过去） |
| CI | 未挂 | 第 2 步的 `16.infra.md`：`npm ci --ignore-scripts && npm run typecheck && npm test && npm run build` |

---

## 9. 故障速查（按症状）

### 9.1 前端侧

| 症状 | 先看哪里 | 多半是 |
|---|---|---|
| 页面能开，一问就「什么都不发生」 | 浏览器 Console + Network | WS 握手失败（子协议）/ 后端没起 / `NEXT_PUBLIC_API_BASE` 指错（§10.1 坑 7） |
| 顶栏是亮的但正文一直「正在检查登录状态…」 | Console 有没有报错 | `localStorage` 不可用（隐私模式）→ 退化为内存存储，本会话可用 |
| 一进 `/` 就提示「需要先登录」 | `/login` 里的令牌状态徽章 | 令牌过期 / 是刷新令牌 / 复制时缺了一段 |
| 回答出来了但没有「来源」 | 先看后端日志有没有 `retrieve_knowledge` 的 tool_error | 向量库空（跑过全量回归）或问法没走知识库（§6.5） |
| 图表区域空着 | 后端日志里 `sql_query` 的 tool_error | 绑定参数占位符（§10.1 坑 5）；或这一轮确实没有 `chart_spec`（页面会有一句说明） |
| 点出处页 404 | `/api/kb?name=...` 的响应体 | 出处解析出的文档名不在 `docs/kb/` 里（页面上会列出可用清单） |

### 9.2 后端侧（症状出现在前端，根因在后端）

| 症状 | 命令 | 说明 |
|---|---|---|
| 所有请求 401 | `uv run python -m scripts.dev_token --json` 后 `parse_token` 自检 | 两边 `JWT_SECRET` 不一致，或令牌过期 |
| 429 / 4429 | `curl http://127.0.0.1:8000/metrics \| findstr ratelimit` | 限流额度（`RATE_LIMIT_CHAT*`）；WS 那条是 `RATE_LIMIT_CHAT_STREAM` |
| 回答是「查不到相关资料」 | 后端日志里搜 `rag 完成` / `图级兜底` | 检索回 0（语料空）或检索抛错（§10.1 坑 4） |
| 回答被拦下 | 日志里搜 `guardrail 拦下请求` | 输出侧护栏拦的（正常行为，前端会显示「已被拦下」） |

---

## 10. 坑与分诊

### 10.1 本步实测撞到的 10 个坑

**坑 1：`npm install` 报 `spawn EPERM` 并整包回滚（postinstall 脚本起子进程被拒）**

```
npm error syscall spawn
npm error errno -4048
npm error Error: spawn EPERM
```

`sharp` / `unrs-resolver` 的 postinstall 要起子进程；本机受限环境下被拒 → npm **回滚整个 install**
（`node_modules` 只剩一个 `.package-lock.json`）。**修法：`npm install --ignore-scripts`**
（Next/Tailwind 的开发与构建都不需要这两个 postinstall 的产物；`sharp` 只在 `next/image` 优化时用，
本步不用 `next/image`）。
⚠️ 同类坑还有一处：`npx create-next-app` 会在 `%APPDATA%\create-next-app-nodejs\` 下写配置，
沙箱下 `mkdir` 被拒（`EPERM ... create-next-app-nodejs\Config`）。本步的处置是
**改用手写脚手架**（`package.json` / `tsconfig.json` / `next.config.ts` / `globals.css` / `layout.tsx`
逐个人写），这一步反而让第 3 节的六个决定写得比生成器更贴本仓契约。

**坑 2：`next build` / `next dev` 报 `spawn EPERM`（**用了带管道的子进程**）**

```powershell
node node_modules/next/dist/bin/next build
# → Error: spawn EPERM   （编译已经开始，失败在 lint/type-check 的 worker 上）
```

Next 的构建/开发会 fork worker（带 `pipe` stdio）。本机在受限文件访问模式下**没有**带管道的子进程能力
（Python 侧同一现象的记录见 [`15.mcp-runbook.md`](15.mcp-runbook.md) §10.1）。
处置：跑 `npm run build` / `npm run dev` 时**放宽文件访问**（本地终端里直接跑没有这个问题；
本会话是通过沙箱的放宽机制跑的）。**判据**：错误里出现 `errno: -4048` + `syscall: 'spawn'` 就是它，
不要浪费时间去查代码。

**坑 3：测试跑不起来 —— `node --test` 默认 fork 子进程；`tsx`/`esbuild` 也要起子进程**

```text
✖ tests/stream.test.ts  Error: spawn EPERM
  at TestContext.<anonymous> (node:internal/test_runner/runner:454:19)
```

三件事一次性记住：

1. **`node --test --test-isolation=none`**：在**同一进程**里跑（本机唯一能跑通的方式）；
2. **不用 tsx**：它要起 esbuild service（子进程）。改用 Node 24 的**原生类型擦除**直接跑 `.ts`；
   代价两条：① 相对导入**必须带 `.ts` 扩展名**（`tsconfig` 里开 `allowImportingTsExtensions`）；
   ② 不支持**构造函数参数属性**（`constructor(readonly url: string)` 会抛
   `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX` —— 见 `tests/transport.test.ts` 的注释）；
3. **`npm test` 的 glob 只能是 `tests/*.test.ts`**：`tests/**/*.test.ts` 会把真链路的
   `tests/live/*.test.ts` 也收进来（前端没起时一片 ECONNREFUSED）。分两套命令是有意的（§7.1）。

**坑 4：异步图节点调用**同步**检索 → 检索抛错被兜底吞掉，用户只看到「没有来源」**

后端日志原文（第 16 步第一次问知识库问题时）：

```text
tool_error {"tool": "retrieve_knowledge", ..., "error_type": "RuntimeError",
            "details": {"error": "search_sync() 不能在事件循环内调用（如 FastAPI 路由）..."}}
执行在 rag 失败：... → 从断点重放
重放预算用尽（1 次），把失败写回 state 兜底：...
```

根因：`app/agents/rag.py::rag_node` 是 **async** 节点，却调用**同步**入口
`app/tools/retriever.retrieve_docs`，而后者的默认后端 `app/rag/retriever.hybrid_search`
内部是 `asyncio.run(...)`。异常被 `ToolNotAvailableError` 那一段兜底吞成 refusals，
于是**症状与「语料没灌」完全一样**。

修法（后端三处，均已在仓库里）：

1. `app/tools/retriever.py`：实现抽成 `_retrieve_docs`，对外给两个入口 ——
   同步的 `retrieve_docs()`（在事件循环里调用会**显式拒绝**并指向异步版）与
   `await retrieve_docs_async()`（整段走 `asyncio.to_thread`，审计/埋点上下文一并搬进线程）；
2. `app/agents/rag.py`：改调 `await retrieve_docs_async(...)`；
3. `tests/cache/test_cache_contracts.py::test_rag_node_checks_the_cache_before_retrieving`：
   它的判据是**源码顺序**（`cache.semantic_get` 要在检索调用之前），
   所以调用名一变就红。改判据时踩了一个小坑：裸的 `retrieve_docs(` 会命中**文件顶部的 import**
   （它在缓存闸之前）→ 断言以「顺序反了」的样子红掉，报错是 `assert 4928 < 189`。
   **判据必须带 `await `**。

**坑 5：Text-to-SQL 把绑定参数写成**占位符** → 查询查回 0 行 → 图表出不来**

后端日志原文：

```text
sql_query ... params: {"since": "<统计起始时间，如 2024-01-01>", "n": "<返回的状态种类上限>"}
DataError [SQLSTATE 22007]: invalid input syntax for type timestamp with time zone: "<统计起始时间，如 2024-01-01>"
analysis 无数据可分析：没有可分析的数据行...
```

两种形状都实测见过：`<值>` → `invalid input syntax for type bigint`；
`<统计起始时间，如 2024-01-01>` → `InvalidDatetimeFormat`。
**根因不在模型「笨」，在 few-shot 教坏了它**：`app/agents/data.py::_example_params` 第一版
生成的示例是 `绑定参数：{'uid': <值>, 'n': <值>}` —— 模型照抄了尖括号。

修法（后端两处）：

1. `_example_params` 改成**真实值**（`'uid': 2, 'n': 20, 'since': '2026-01-01'`）；
2. 新增 `placeholder_param_names()`：在 `plan_sql` 里**先于守卫**拦下占位符形状的绑定参数，
   回喂一句可照做的指示（「params 里必须是真实值…不知道具体值时改写查询去掉那个条件」），
   让它走正常的重写轮，而不是落到数据库层炸。

修完实测：§6.4 的五种问法**全部出图**（修之前五种全不出）。
用例：`tests/test_agents_data.py` 新增 3 条（示例不许有尖括号 / 占位符会重试 / 一直给占位符会降级）
+ 1 条识别器形状用例。

**坑 6：跑完 Python 全量回归，向量库空了 → 前端「没有来源」**

`tests/test_db_migrations.py::test_migration_round_trip_is_repeatable` 会 `downgrade base`，
把 `rag_chunks` 清空（实测：`rag_chunks 总数=0`）。现象是「昨天还好好的溯源今天没了」。
**修法**：`uv run python -m scripts.load_knowledge_base`（约 56~82s）之后**重启后端**
（后端进程里的检索单例已经建过，重启最干净）。与第 14/15 步是同一条前置条件顺序。

**坑 7：WebSocket 握手失败 —— 子协议少写了 `shopsage.v1`**

```ts
new WebSocket(url, ["bearer." + token])              // ❌ 少一个：服务端 accept 时挑不到，浏览器报握手失败
new WebSocket(url, ["shopsage.v1", "bearer." + token])  // ✅ 两个都要
```

原因在后端：`websocket.accept(subprotocol=WS_SUBPROTOCOL if subprotocols else None)` ——
服务端只在**客户端提出的列表里**挑一个回。所以客户端必须把它一起提出来。
离线用例（`tests/transport.test.ts`）直接断言 `protocols` 数组的两项，就是为了让这条永不回退。

**坑 8：改了后端代码，前端没变（`--reload` 起不来）**

本机 `uv run uvicorn ... --reload` 起不来（watchfiles 要起子进程），所以后端是**手动重启**的进程。
本步为了修坑 4/坑 5 改了后端代码 → 必须**重启后端**才生效。
判断「后端跑的是哪一版」最快的办法：看日志里有没有新加的日志行（例如 SQL 失败那条 tool_error 的字段）。
⚠️ 这类「改了没生效」的假红最费时间，所以 §10.2 的第 1 条就是「先确认两边进程的版本」。

**坑 9：把前端的角色表当成安全边界**

`src/lib/auth.ts` 里有一份 `ROLE_PERMISSIONS`（后端 `app/security/rbac.py` 的副本）。
它出现的**唯一**理由是「界面上不用为一次渲染去问后端」。一旦有人用它做授权判断
（比如「前端说他是 internal 就不用加行级过滤了」），就等于把 D1 的第二半交给了浏览器 ——
而浏览器完全在用户手里。**判据**：前端文件里出现 SQL / 行级过滤 / 脱敏 / 权限点判断
（除渲染分支之外）就是越界。

**坑 10：`next build` 与 `next dev` **同时**跑 → dev server 的 `.next` 被覆盖，页面变 500**

本步收尾时实测撞到（先跑着 `npm run dev` 验证页面，又另开一条跑 `npm run build` 验收）：

```text
GET / 500 in 9707ms
⨯ Error: Could not find the module "…\node_modules\next\dist\next-devtools\userspace\app\
  segment-explorer-node.js#SegmentViewNode" in the React Client Manifest.
⨯ [TypeError: __webpack_modules__[moduleId] is not a function] { page: '/' }
```

数据没坏、代码也没坏：**两者共用同一个 `.next` 目录**，`build` 会把它改写成一棵「生产产物树」，
正在跑的 dev server 增量状态随之错位。**修法**：停掉 dev → `Remove-Item -Recurse -Force web\.next` → 重新 `npm run dev`。

⚠️ 由此得出一条操作纪律：**验收构建时先停 dev**（或至少别指望 dev 还能用），
这与「改了后端代码要重启后端」（坑 8）是同一类「顺序错了会得到假红」的坑。

### 10.2 排障顺序（三条路，别乱）

1. **先确认两个进程各自在跑哪一版、连的是谁**：
   `Invoke-RestMethod http://127.0.0.1:8000/health`（后端在不在、连的哪个库）
   + 页面顶栏那行 `后端 http://127.0.0.1:8000 · 身份 user_id=… · 会话 …`（前端连的是谁）。
   本步 90% 的「假红」都在这一步被排除（改了后端没重启、`NEXT_PUBLIC_API_BASE` 指错、令牌过期）。
2. **再看后端日志里的 tool_error**：`data`/`rag` 的失败在日志里是**结构化一行**
   （带 `tool`、`error_type`、`args`）；前端看到的「没有来源 / 没有图表」几乎都对应这里的一行。
3. **最后才怀疑前端**：此时用 `npm test`（离线）与浏览器 Console 夹逼 ——
   离线绿 + Console 报错 = 环境/契约问题；离线红 = 前端逻辑问题。

### 10.3 坑的分布（为什么这十条里有一半不是前端的问题）

| 坑 | 表面症状在前端？ | 根因所在 |
|---|---|---|
| 1 / 2 / 3（工具链） | 是（`npm install`/`build`/`test` 失败） | 本机受限环境（子进程、缓存目录） |
| 4 / 5（检索 / 出图） | **是**（「没有来源」「没有图表」） | **后端**：异步节点调同步检索、few-shot 教坏了模型 |
| 6（语料被清空） | 是 | 测试顺序（全量回归会 `downgrade base`） |
| 7 / 8 / 10（子协议 / 没重启 / 两个 next 抢 `.next`） | 是 | 契约细节 + **操作顺序** |
| 9（把前端当安全边界） | 不是（它是设计纪律） | 分层 |

这张表的用途只有一个：**下次看到「前端坏了」，先按 10.2 的三步确认根因在哪一层** ——
本步一半的「前端 bug」最后都改在后端或操作顺序上。

---

## 11. 验收与证据回填

```powershell
# ① 离线用例（本模块，零依赖）
cd web; npm test
# 实测：tests 65 / pass 65 / fail 0（0.22s）

# ② 类型检查 + 构建（验收「构建通过」）
npm run typecheck      # 实测：无输出
npm run build          # 实测：✓ Compiled successfully in 22.0s；9/9 静态页

# ③ 真链路（需要后端 + 语料；前端没起时启动器会自己起）
node tests/run-live.mjs
# 实测：tests 9 / pass 9 / fail 0（41.9s 热身态 / 81.9s 冷启动态）

# ④ 四条路由可访问（前端在跑时）
foreach ($p in '/', '/internal', '/login', '/citation?q=%E9%80%80%E8%B4%A7%E6%94%BF%E7%AD%96.md') {
  $r = Invoke-WebRequest "http://127.0.0.1:3000$p" -UseBasicParsing
  "$p -> $($r.StatusCode)"
}
# 实测：四条都是 200

# ⑤ 语料与依赖栈状态（回填时一起记）
cd ..
uv run python -m scripts.probe_rag_sources     # 实测：61 活跃片段 / 8 份文档
uv run python -m scripts.db_check              # 实测：users 8 / orders 30 / products 20 / order_items 65

# ⑥ Python 全量回归（改了后端三处，必须跑）
uv run pytest -q -m "not llm_live"
# 实测：1617 passed, 50 skipped, 13 deselected, 6 errors（140s）
```

回填到 [`docs/modules/16.web.md`](../modules/16.web.md) §8 的四条验收标准 + 一张证据表（§12 有模板）。

---

## 12. 四组自查命令（收尾必跑）

```powershell
# ── 执行位置：web/ ──
$env:npm_config_cache = "..\.npm-cache"

# ① 前端不许有业务判断：SQL / ORM / 模型 SDK / prompt
#    ⚠️ 路径要**逐个列**（`src\**\*.ts` 这种写法在 PowerShell 里要求 -Recurse，直接写会「零匹配」——
#    零匹配会让这条自查静默失效，所以下面这行是实测能跑的版本）
Select-String -Path 'src\lib\*.ts','src\components\*.ts','src\components\*.tsx',
  'src\app\page.tsx','src\app\internal\page.tsx','src\app\login\page.tsx','src\app\citation\page.tsx',
  'src\app\api\kb\route.ts' -Pattern 'SELECT |INSERT |sqlalchemy|openai|prompt'
# 实测：**没有任何输出**（连注释里都没有这些词）

# ② 契约镜像不许漂移：前端常量必须与后端逐字一致
#    期望：七行都是 backend=True frontend=True
$py = '..\app\api\schemas.py'; $ts = 'src\lib\contracts.ts'
foreach ($pair in @('shopsage.v1','bearer.','4401','4429','4400','delivered','approval_required')) {
  "{0,-20} backend={1,-6} frontend={2}" -f $pair,
    (Select-String -Path $py -Pattern ([regex]::Escape($pair)) -Quiet),
    (Select-String -Path $ts -Pattern ([regex]::Escape($pair)) -Quiet)
}
# 实测：shopsage.v1 / bearer. / 4401 / 4429 / 4400 / delivered / approval_required 全部 True-True

# ③ 路由清单：两条页面 + 一条出处页 + 一条只读语料路由
Get-ChildItem -Recurse src\app -Include page.tsx,route.ts |
  ForEach-Object { $_.FullName.Replace("$PWD\src\app\", '') }
# 实测：
#   api\kb\route.ts
#   citation\page.tsx
#   internal\page.tsx
#   login\page.tsx
#   page.tsx

# ④ 交叉核对：离线用例数、真链路用例数、构建产物路径
npm test 2>&1 | Select-String 'tests |pass |fail '
# 实测：tests 65 / pass 65 / fail 0
```

---

## 13. 相关文档

* 设计原理与取舍：[`docs/modules/16.web.md`](../modules/16.web.md)（8 段模板）
* 后端的接口与帧契约：[`9.api-runbook.md`](9.api-runbook.md) §2（三条路由）
  / §4（帧时序原文）/ §7（错误信封）
* 溯源串的格式来源：`app/tools/retriever.py::RetrievedDoc.citation()`；
  前端的解析用例见 `web/tests/citations.test.ts`
* 图表 spec 的字段来源：`app/tools/chart.py::build_chart_spec`；
  前端的适配用例见 `web/tests/chart.test.ts`
* 角色与权限点：`app/security/rbac.py`（真相）/ `web/src/lib/auth.ts`（镜像，**只用于渲染**）
* 依赖栈与跨主机拓扑：[`2.infra-runbook.md`](2.infra-runbook.md) §1.1
* 改配置/代码后怎么重启：[`17.docker-ops.md`](17.docker-ops.md)
