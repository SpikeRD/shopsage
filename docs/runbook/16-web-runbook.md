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
> | 本步离线用例 | `npm test` | **102 passed / 0 failed**（8 个文件；0.26s；含 7 条源码级接线断言，见 §10.1 坑 17） |
> | 本步真链路用例 | `node tests/run-live.mjs`（前端已在跑时加 `SHOPSAGE_SKIP_WEB=1`） | **10 passed / 0 failed**；三次实测 **41.9s / 81.9s / 143.2s** —— 差距全在「真溯源」那一条（第一次检索要把 bge-m3 + bge-reranker 拉进内存，实测该条 2.5s ~ 109.7s） |
> | **浏览器跨源 + WebSocket upgrade** | `uv run python -m scripts.check_web_cors` | **五项全 ✅、退出码 0**（后端可达 / 前端可达 / 放通来源 / 预检 / **WS 升级 101**） |
> | **HTTP 通但 WS 死**的定位（坑 14） | 三条对照：直连 8000 / 直连 localhost / 经 3000 | `8000` → open（54ms）；`localhost:8000` → open（27ms）；**经前端 3000 → 8s 超时** |
> | **真令牌过前端判定**（坑 13 的收尾验证） | 真签两个令牌 → 跑 `web/src/lib/auth.ts` 的同一份代码 | `typ=access` → `isAccessToken=true`；`typ=refresh` → `false`（修复前**两个都是 false**） |
> | **连接生命周期**（坑 15/16/17） | `npm test` 的 `transport` / `hook-wiring` 两组 | 僵尸连接不污染状态（3 条）/ 握手与回帧看门狗（4 条）/ 重连接线（7 条） |
> | 网卡来源（`next dev` 的 Network 那一行） | `check_web_cors --web http://192.168.1.13:3000` | ③ 明确报「不在白名单」并给出要写进 `.env` 的那一行（WS 侧却能升级 101） |
> | Python 侧新增的 CORS 用例 | `uv run pytest -q tests/api/test_cors.py` | **11 passed**（简单请求 / 白名单外来源 / 预检不吃配额 / 错误响应带头 / Expose-Headers） |
> | 页面可访问性 | `Invoke-WebRequest http://127.0.0.1:3000{/,\/internal,\/login,\/citation}` | 四条路由都 **HTTP 200**、title 为 `ShopSage · 智能助手` |
> | `/api/kb` 安全面 | 真链路用例里的一条 | 正常名 200；`../.env` → **400**；不带 `name` → 200 列清单 |
> | Python 全量回归 | `uv run pytest -q -m "not llm_live"` | **1628 passed, 50 skipped, 13 deselected, 6 errors**（150s） |
> | Python 全量回归（**本步之前**的基线） | 同上 | **1613 passed, 50 skipped, 13 deselected, 6 errors**（见 [`15.mcp-runbook.md`](15.mcp-runbook.md) 状态表） |
>
> 🔧 **本步对后端的改动（四处，都是实测逼出来的）与对前端的收口（三处）**：
>
> | # | 位置 | 改了什么 | 坑 |
> |---|---|---|---|
> | ① | `app/api/config.py` + `app/api/main.py` | 新增 **CORS 中间件与来源白名单**（`API_CORS_ORIGINS`，必须是最外层） | 11 |
> | ② | `app/tools/retriever.py` | 新增异步入口 `retrieve_docs_async`（同步入口在事件循环里显式拒绝） | 4 |
> | ③ | `app/agents/rag.py` | 改调异步入口（否则检索抛错被兜底吞成「没有来源」） | 4 |
> | ④ | `app/agents/data.py` | few-shot 绑定参数改成真实值 + 新增占位符守卫 | 5 |
> | ⑤ | `web/src/lib/auth.ts` | 令牌类型声明读 `typ`（原来读 `type` → **所有真令牌被拒**） | 13 |
> | ⑥ | `web/src/lib/config.ts` | `resolvedApiBase()` 纠正「base 指到前端自己」并说明 | 14 |
> | ⑦ | `web/src/lib/stream.ts` + `useChatStream.ts` + `page.tsx` | 世代守卫 / 看门狗 / 排队与重连接线 | 15 / 16 / 17 |
>
> ⚠️ 6 个 error 是 `tests/test_config.py` 里用 `tmp_path` 的老问题（沙箱 `PermissionError: [WinError 5]`），
> **改动之前就是这个数**。

> ⚠️ **本步还留下一条硬约束**（值得单列，因为我踩了两次）：
> **改文件只用编辑工具，绝不用 PowerShell 回写**。
> 本轮两次用 `Set-Content -Encoding utf8` 回写 markdown，把整篇中文变成了 `鍓嶇` 这类乱码
> （UTF-8 字节被当 GBK 解释的双重编码），最后只能 `git checkout --` 回滚重做。
> 判据：`node -e "const t=require('fs').readFileSync(p,'utf8'); console.log((t.match(/\uFFFD/g)||[]).length)"`
> 或肉眼扫一眼有没有 `鍓/锛/銆` —— 有就是坏了，**不要试图修**，直接从版本库重做。

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
| **一条命令自检「两个进程 + 跨源 + WS 升级」** | Windows | **§1.5（`uv run python -m scripts.check_web_cors`）** |
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
| 「HTTP 好好的，一进对客页就说 WebSocket 连接失败」 | Windows | §10.1 坑 14（base 指到了前端自己；自检 ⑤ 一次定位） |
| 「照 `next dev` 的 Network 地址打开：页面能开、却一直显示连不上，发消息也没反应」 | Windows | **§10.1 坑 18**（该来源不在 `API_CORS_ORIGINS`；自检加 `--web <你实际打开的地址>` 一次定位。**首选 `http://127.0.0.1:3000` 打开**） |
| 「一直显示正在连接…、输入框点不动（点新会话就好了）」 | Windows | §10.1 坑 16 / 17（看门狗 + 重连接线；页面现在显示 `正在连接… Ns`） |
| 「秒数一直涨、消息也上屏了但**永远等不到回答**，刷新也没用」 | Windows | **§10.1 坑 20**（握手成功但 phase 没离开 `connecting` → `send` 永远排队；真 bug，已修） |
| 「**等 LLM 回复时退出登录** → 之后再也登不进去、一直显示「登录中」，且**换账号也一样**」 | Windows | **§10.1 坑 21**（后端事件循环被死连接的忙等占满：`/health` 也会永久超时，`py-spy` 见 `seq` 天文数字；真 bug，已修，issue 007） |
| 「聊得好好的，忽然弹一条 WebSocket 连接失败」 | Windows | §10.1 坑 15（僵尸连接；看后端日志里同一端口的两条 `accepted`） |
| 「令牌明明是对的，登录页却说它是刷新令牌」 | Windows | §10.1 坑 13（`typ` 被读成 `type`） |
| 「一切请求都 `Failed to fetch`，而服务端日志是 200」 | Windows | §10.1 坑 11（缺 CORS）**别重签令牌** |
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
   ⚠️ 而「令牌是什么形状」也**必须以后端为准**：类型声明的键名是 `typ`，
   第一版前端按 `type` 读，于是**所有真令牌都被拒**（§10.1 坑 13）。

另一条（本步最值钱的认知）：**「流式」与「图表」在后端是两条不同的出口，前端必须选一条**。
WebSocket 的 `answer` 帧只带一个布尔 `chart`（`app/api/chat.py` 刻意不把 spec 塞进帧），
`chart_spec` 只出现在 HTTP 响应里。所以对客页走 WS（要首字快、要可中断），
对内页走 HTTP（要拿到图表本体）—— 这不是偷懒，是**契约决定的**（§2.3）。

再一条（坑 15/16/17 换来的）：**连接的「状态」比连接的「结果」更难写对**。
`WebSocket` 只给 open / message / error / close 四个事件，而现实里还会出现
「什么都不给」（握手被吞）、「给了但来自一条已经没人要的连接」（僵尸连接）、
「我以为它在连、其实它永远连不上」三种情况。
**所以连接层必须自己长出三样东西**：世代号（谁说的算）、看门狗（多久算死）、
以及「界面不许因为连接状态而永久禁用」——三样缺一，症状都会是「用户被卡住且不知道原因」。

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
Test-NetConnection 192.168.1.6 -Port 5432 -InformationLevel Quiet   # True
Test-NetConnection 192.168.1.6 -Port 6379 -InformationLevel Quiet   # True

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
│  ├─ stream.ts            # ★reducer（纯函数）+ 连接层（世代守卫 / 看门狗 / 主动关闭语义）
│  ├─ chart.ts             # ★`chart_spec` → ECharts option / 表格视图（纯函数）
│  ├─ api.ts               # HTTP 客户端：一套错误信封解析 + 令牌注入
│  ├─ auth.ts              # 登录态、角色→权限表（**界面边界**）、会话 id、令牌类型（`typ`！见坑 13）
│  ├─ diagnostics.ts       # ★「探活失败」的提示语（把「先别怀疑令牌」钉进代码，见 §10.1 坑 11）
│  └─ config.ts            # 唯一一处读 `NEXT_PUBLIC_*` 的地方 + **地址纠正**（base 指到前端自己时改用它，见坑 14）
├─ src/components/
│  ├─ useChatStream.ts     # 把 WS 连接与消息列表缝起来（hook）：排队 / 重连 / 发送守卫（坑 16/17）
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
   ├─ transport.test.ts    # 子协议 / 假 socket / 错误信封 / 僵尸连接 / 看门狗（坑 15/16）
   ├─ hook-wiring.test.ts  # ★源码级接线断言：依赖数组 / onOpen 排队 / running 不含 connecting（坑 17）
   ├─ chart.test.ts        # 图表适配（含 table / 空数据 / 截断警告）
   ├─ citations.test.ts    # 出处串解析（含**实测形状** `docs/kb/退货政策.md #<hex>`）
   ├─ auth.test.ts         # 角色分流 / 令牌过期 / 会话 id 字符集 / **`typ` 类型判定**（坑 13）
   ├─ diagnostics.test.ts  # 「探活失败」提示语（先别怀疑令牌 / 三条动作 / 可粘贴的 .env 行）
   ├─ config.test.ts       # 地址推导与**纠正**（base 指到前端自己时必须纠正并说出来，坑 14）
   ├─ live/live.test.ts    # 真链路（真后端 + 真前端；缺令牌时整组 skip）
   └─ run-live.mjs         # 真链路启动器（签令牌 + 起前端 + 跑用例）
```

⚠️ **`hook-wiring.test.ts` 这一层为什么必须存在**（坑 17 的教训）：
它读 `useChatStream.ts` / `page.tsx` 的**源码文本**，断言接线形状
（依赖数组里有没有 `connectionKey`、`onOpen` 里有没有 flush `pendingRef`、
`running` 里有没有混进 `connecting`……）。这类 bug **躲过所有运行期证据**：
纯逻辑单测看不见 React 接线、类型检查不管依赖数组、真链路在 Node 里根本没有 React。
局限（别误读）：它挡得住「有人把依赖删了」，挡不住「依赖写了但逻辑顺序错」。

仓库根新增的四个脚本（都是**给本步用的工具**，不是业务代码）：

| 文件 | 作用 | 为什么值得进仓库 |
|---|---|---|
| `scripts/dev_token.py` | 签发演示用 JWT（`--role` / `--user-id` / `--kind refresh` / `--json`） | 本仓没有登录接口，而浏览器要真令牌才能连后端；同时它是「401 分支怎么试」的唯一入口（§4.1） |
| `scripts/check_web_cors.py` | 一条命令自检「两个进程 + 跨源 + 预检 + **WebSocket upgrade**」，退出码可当门禁 | 前端 `Failed to fetch` 有三种成因、`HTTP 通但 WS 死` 另有一种，判断它们要同时看三处；这条命令把三处问完并给出修法（§1.5 / §10.1 坑 11 / 坑 14） |
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
#    - Network:      http://192.168.1.13:3000
#    ✓ Ready in 4.7s

# ② 生产模式（构建 + 起；验收「构建通过」用这条）
npm run build      # ✓ Compiled successfully in 22.0s（9/9 静态页）
npm start          # http://localhost:3000

# ③ 指向别的后端（默认 http://127.0.0.1:8000）
$env:NEXT_PUBLIC_API_BASE = "http://192.168.1.13:8000"; npm run dev
```

> ⚠️ **`NEXT_PUBLIC_API_BASE` 是构建期变量**（`NEXT_PUBLIC_*` 会被字面替换进客户端包），
> 所以「改了地址要重启 `npm run dev`（生产模式要重新 `npm run build`）」——
> 与后端 `.env` 的「重启进程就生效」不同，这一点很容易被误解成「配置没生效」。
> 唯一读它的地方是 `src/lib/config.ts`（前端也守「配置取值只有一处」）。

### 1.5 一条命令自检「两个进程 + 跨源 + WebSocket upgrade」（本步最常用的一条）

```powershell
# ── 执行位置：仓库根 ──
uv run python -m scripts.check_web_cors
```

实测输出（两个进程都正常时）：

```text
① 后端可达（http://127.0.0.1:8000）
  ✅ HTTP 200 status=ok env=dev
② 前端可达（http://127.0.0.1:3000）
  ✅ HTTP 200（15366 字节）
③ 后端放通前端来源吗（浏览器唯一真正在意的一件事）
  ✅ Access-Control-Allow-Origin = http://127.0.0.1:3000
  ✅ Access-Control-Expose-Headers = X-Request-Id
④ 预检（OPTIONS /api/chat，**不带令牌**）
  ✅ HTTP 200（预检不该要求鉴权）
⑤ WebSocket 流式端点**真的能 upgrade** 吗（对客页的主链路）
  ✅ ws://127.0.0.1:8000/api/chat/stream → HTTP/1.1 101 Switching Protocols（upgrade 成功）

结论：两个进程都在、浏览器可以跨源读这个 API、WebSocket 能 upgrade —— 打开前端页面按 runbook §5 试。
退出码 = 0
```

五项各挡一类问题，其中 ⑤ 是**坑 14 的判据**（「HTTP 通但 WS 连不上」时它一次就能定位）：

| 项 | 挡的问题 |
|---|---|
| ① 后端可达 | 进程没起 / 端口被占 / 地址写错 |
| ② 前端可达 | `next dev` 没起（或还在首次编译） |
| ③ 放通来源 | **CORS 白名单**（坑 11）；认得出「这是网卡地址」（坑 14 附带那条） |
| ④ 预检 | CORS 中间件位置（是不是最外层）；来源不在白名单时会明确说「根因在 ③」而不是误导 |
| ⑤ WebSocket upgrade | **地址指错**（坑 14）：`ws:` 连不上而 HTTP 正常 ⇒ 先怀疑地址 |

> 🎯 ⑤ 的用法：它直接用**裸 socket** 发一次 WebSocket 握手（不打令牌），
> 所以结果分得清：「101 = upgrade 通了」「401/403 = 通了但被鉴权拒（也说明通）」
> 「404 = 这个地址上没有 WS 路由（URL 指错地方）」「超时 = 那个端口上没人接 upgrade（最典型的是指到了前端自己的 3000）」。

为什么值得有这条命令：前端的 `Failed to fetch` 有**三种**成因（进程没起 / 跨源被拦 / 地址写错），
而判断它需要同时看三处（两个终端 + 后端配置）。这条命令把三件事一次问完，
**每一项不通过时都打印出「怎么办」**，退出码可直接当门禁用。
⚠️ 它不需要令牌，也不需要语料 —— 只回答连通性与跨源（§10.1 坑 11 就是它要挡的那类问题）。

⚠️ **`next dev` 是「按路由现编译」的**：重启后第一次请求 `/` 实测要 **8.1s**（`/internal` 11.7s）。
所以这个自检的默认单项超时是 **20 秒**（`--timeout` 可调）—— 设小了会把「正在编译」
报成「前端没起」，那是一个**自查工具自己给出的假信号**，比没有工具更糟。

不通过时它长这样（前端没起 + 跨源没配）：

```text
② 前端可达（http://127.0.0.1:3000）
  ❌ 连不上：timed out
     → 起前端：cd web; npm run dev
③ 后端放通前端来源吗（浏览器唯一真正在意的一件事）
  ❌ Access-Control-Allow-Origin = （空）
     → 在后端 .env 里加一行：API_CORS_ORIGINS=http://127.0.0.1:3000 然后**重启后端**
```

---

## 2. 三个入口：我该敲哪条命令

### 2.0 先看这里：变更 001 起「怎么登录」变了（**最常踩的一条**）

| | 变更 001 之前 | 变更 001 之后 | **变更 002 之后** |
|---|---|---|---|
| 登录方式 | **粘贴令牌**（`scripts/dev_token.py` 现场签） | **账号 + 口令**（`POST /api/auth/login`） | 同左（未变） |
| 口令从哪来 | —— | `uv run python -m scripts.seed` 灌的演示账号；口令在 `scripts/seed.py` 的 `DEMO_ACCOUNTS` 里（**刻意不打印到终端**） | 同左 |
| 进哪个界面 | 前端按角色**猜** | **服务端返回 `entry`** | 同左 |
| 30 分钟后 | 重新签、重新粘 | **自动续期**（`web/src/lib/session.ts`） | 同左 |
| 粘贴令牌 | 唯一方式 | `/login` 的「开发模式」折叠区（**保留**：排障与真链路用例还用它） | ⚠️ **界面已删除** —— 改用下面的 DevTools 方式（见「变更 002 的排障入口」） |
| 页面外壳 | 顶部导航 | 顶部导航（按角色显示入口） | **左侧边栏**（按权限显示入口 + 底部个人信息/权限状态） |
| 主题 | 深色（`data-theme` 是死代码） | 同左 | **单一浅色护眼** |
| 探活 | 登录页一个**手动按钮** | 同左 | **每次打开/刷新自动探活**；失败顶部横幅 |

```powershell
# 最短路径（三个演示账号，口令见 scripts/seed.py::DEMO_ACCOUNTS）
uv run python -m scripts.seed                 # 灌账号（幂等；连跑三次结果一致）
# 打开 http://127.0.0.1:3000/login
#   admin          → /admin      （管理台：开账号 / 停用启用 / 改角色 / 重置口令）
#   13800000002    → /           （对客）
#   employee       → /internal   （对内）
```

#### 变更 002 的排障入口：手粘令牌（原「开发模式」已删）

变更 002 按「登录页只管登录账户」把粘贴令牌的**界面**删了，但**存储层与 `dev_token` 都没动**。
排障时要手粘一个令牌，用 DevTools 直接写 `localStorage`：

```javascript
// ① 先签一个（在仓库根）
//    uv run python -m scripts.dev_token --account admin
// ② 浏览器打开 http://127.0.0.1:3000，F12 → Console，粘贴：
localStorage.setItem("shopsage.token.admin", "<上面签出来的令牌>");
// ③ 刷新页面即生效（侧边栏底部会显示身份与权限状态）
//    key 的形状见 web/src/lib/auth.ts::tokenKey：shopsage.token.{customer|internal|admin}
//    ⚠️ 这种方式没有刷新令牌：30 分钟后要重新粘（与变更 001 的开发模式一致）
```

> 为什么用 `localStorage` 而不是改 URL：令牌放查询串会进 access log、浏览器历史与 Referer，
> 与 `app/api/schemas.py` 里 `WS_TOKEN_PREFIX` 走子协议而非查询串是同一条理由。

⚠️ **三条最容易误判的**（都写进了 `/login` 的提示语里）：

1. **「账号不存在」与「口令错误」返回同一句话** —— 那是**防枚举**，不是提示没写清楚。
   别去后端找「为什么不说清是哪个错了」。
2. **登录被 429** 是**限流**（`auth.login` 是 10 次/5 分钟，全项目最严的一条）。
   它是防暴力破解的，等一会儿即可 —— 别去查令牌或后端。
3. **旧格式令牌一定被拒**：变更 001 之前签的令牌（没有 `aid`/`ver`）在
   `/login` 的开发模式里会被**提前标红**（「旧格式（会被拒）」），
   而不是让你拿着它去发消息、再收到一个不明所以的 401。

### 2.1 一张表看清

| 目标 | 命令 | 期望 |
|---|---|---|
| 看页面 | `npm run dev` → 浏览器开 `http://127.0.0.1:3000` | 顶栏按登录态显示「对客问答 / 对内查询 / 管理台 / 登录」 |
| 看真链路的**自动证据** | `node tests/run-live.mjs` | 10 passed（会自动签令牌；前端没起时它会起） |
| 看**离线**证据 | `npm test` | **119 passed**（不连任何后端） |

> ⚠️ **变更 001 实测：`node tests/run-live.mjs` 在本机会挂住不退出。**
> 它用 `spawn(..., stdio: ["ignore","pipe","pipe"])` 跑 `uv run python -m scripts.dev_token --json`，
> 而受限环境下子进程管道不可用时，它的 `capture()` **既不 error 也不 close** ⇒ 永久挂住
> （该文件注释记的是「会 EPERM 直接失败」，那是另一个变体）。
> **绕法（不改任何文件）**：直接用 `node --test tests/live/live.test.ts`，
> 令牌从环境变量传（`live.test.ts` 本来就支持这条路）：
>
> ```powershell
> $env:SHOPSAGE_TOKEN_CUSTOMER = (uv run python -m scripts.dev_token --account 13800000002 --json | ConvertFrom-Json).token
> $env:SHOPSAGE_TOKEN_INTERNAL = (uv run python -m scripts.dev_token --account employee --json | ConvertFrom-Json).token
> $env:SHOPSAGE_TOKEN_EXPIRED  = (uv run python -m scripts.dev_token --account 13800000002 --ttl -5 --json | ConvertFrom-Json).token
> node --test --test-isolation=none tests/live/live.test.ts
> ```

### 2.2 三个「入口」是什么，以及为什么不是三套前端

`16.web.md` §4 第 1 条要的是「入口靠角色切换，而非多套前端」。本步的落法是：

* **同一个构建产物**里的三条路由（`/`、`/internal`、`/admin`，变更 001 加了第三条）；
* 分流依据是**服务端返回的 `entry`**（`accounts.entry_for_role`，判据是权限点）；
  前端 `defaultEntry()` 只在「手粘令牌、服务端没给 entry」时兜底；
* 三把钥匙分开放（`shopsage.token.customer` / `.internal` / `.admin`），
  所以**同一个浏览器可以同时扮演客户与管理员** —— 登录管理台**不会**把对客页踢下线。

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

### 3.6 第 9 步的 API 为第 16 步补了唯一一处东西：CORS 中间件

这是**本步对后端的第一处契约级改动**，也是最容易漏的一处：前端与 API 不同源，而 CORS 不是「优化」，
是**浏览器读不读得到响应**的前提（§10.1 坑 11 是实测记录）。

```text
        browser (127.0.0.1:3000)                API (127.0.0.1:8000)
             │  GET /health   Origin: http://127.0.0.1:3000
             └──────────────────────────────────────────────▶
                                                           处理，回 200
             ◀──────────────────────────────────────────────┘
        ⚠️ 响应里**没有** Access-Control-Allow-Origin
           → 浏览器拒绝把响应交给页面 → fetch 抛 TypeError: Failed to fetch
           → 页面上看起来跟「后端没起」一模一样（服务端日志里却是 200 OK）
```

落地两处（`app/api/config.py::cors_origins` + `app/api/main.py` 的中间件），三个决定：

| 决定 | 取值 | 理由 |
|---|---|---|
| 中间件**位置** | 最后加 = **最外层** | ① 预检（OPTIONS）带不了 `Authorization`，必须在鉴权/限流之前被答掉；② 401/429/422 也要带上 CORS 头，否则前端连「为什么失败」都读不到 |
| 白名单 | 显式来源列表，**不写 `*`** | 白名单可审计（`4.security.md` 的「表白名单」同一纪律）；配置校验会拒绝 `*` 与漏协议的值 |
| `allow_credentials` | `False` | 令牌走 `Authorization` 头，不靠 Cookie；开放 credentials 是另一个量级的风险 |

⚠️ 三点必须记牢：

1. **CORS 不是鉴权**：它只约束浏览器，`curl` / Node 不受影响 ——
   所以**真链路用例永远测不出这个坑**（§7.5 专门讲这件事，并补了服务端侧的断言）；
2. **`localhost` 与 `127.0.0.1` 是两个不同的来源**，开发期默认两个都放通
   （只配一个的症状是「换个地址打开就坏」）；
3. **来源不带末尾斜杠**（浏览器发的 `Origin` 就是没有斜杠的），代码里做了归一化；
4. **⚠️ 网卡地址（`next dev` 打印的 `Network` 那一行）默认不在白名单里**：
   照它打开页面时，HTTP 侧会失败（没有 `Access-Control-Allow-Origin`、预检 400），
   而 **WebSocket 侧却能升级成功**（WS 不受 CORS 约束）——
   两个相反的结论叠起来 = 「界面能开、流式也能连、但一问就 `Failed to fetch`」（坑 14 附带那条）。

生产形态（第 19 步）：**同源部署**最省事（反代把页面与 `/api` 放同一个域，CORS 直接不需要），
或者显式列域名；所以 `ENV=prod` 且未配置时，白名单是**空**（宁可浏览器拒绝，也不要默认放开）。

---

## 4. 身份：令牌从哪来，怎么进前端

### 4.0 变更 001 起：**优先用账号登录**（这一节其余部分是排障用的备选路径）

```powershell
# ── 执行位置：仓库根 ──
uv run python -m scripts.seed        # 灌演示账号（幂等）
# 然后浏览器打开 http://127.0.0.1:3000/login，填 admin / admin-demo-pass
# （口令见 scripts/seed.py::DEMO_ACCOUNTS —— 刻意不打印到终端）

# 同一件事的命令行版本（脚本/自动化用）：
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/auth/login `
  -ContentType 'application/json' -Body '{"identifier":"admin","password":"admin-demo-pass"}'
```

登录响应里 `entry` 是**服务端算好的入口**（`admin` / `internal` / `customer`）——
前端拿它跳转，不自己推。三个入口各存一把钥匙，互不覆盖（`web/src/lib/session.ts`）。

### 4.1 备选：签一个令牌（**变更 001 之后按账号签，需要先 seed**）

```powershell
# ── 执行位置：仓库根 ──
uv run python -m scripts.dev_token --account employee
# 实测输出（变更 001 之后）：
#   账号      = employee（account_id=…）
#   角色      = internal_employee（权限点：read:own, read:all_orders, write:ticket, write:refund）
#   主体      = e2（thread_id 前缀按它派生：c=客户 / e=员工）
#   入口      = internal
#   类型/有效期 = access / 1800s（世代号 ver=…）
#   自检      = parse_token → subject=e2 role=internal_employee type=access ver=…
#
#   令牌（粘到 http://localhost:3000/login 的「开发模式」）：
#   eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9….

# 按角色取「第一个启用账号」来签（老命令 `--role x --user-id 7` 仍能跑，user-id 被忽略）
uv run python -m scripts.dev_token --role internal_employee

# ⚠️ 令牌 payload 的**真实形状**（变更 001 起；别凭记忆写解析代码）：
#   {"sub": "e2", "stype": "employee", "sid": 2, "aid": <account_id>, "ver": <世代号>,
#    "role": "internal_employee", "typ": "access", "iat": …, "exp": …, "jti": "…"}
#          ↑ 主体标识串（c=客户 / e=员工）      ↑ 类型声明的键名是 **typ**（不是 `type`，见 §10.1 坑 13）
#   ⚠️ 变更 001 之前是 {"sub": "7", ...} —— 那种**旧格式一定被后端拒**（缺 aid/ver），
#      而 `/login` 的开发模式会提前把它标红（「旧格式（会被拒）」）。

# 机器可读（给真链路启动器用）
uv run python -m scripts.dev_token --account 13800000002 --json

# 故意签一个刷新令牌：用来试「401 分支」与「前端提示令牌类型不对」
uv run python -m scripts.dev_token --kind refresh

# 故意签一个已过期的：用来试 4401（WebSocket 关闭码）
uv run python -m scripts.dev_token --account 13800000002 --ttl -5
```

> ⏱ **默认只活 30 分钟**（`DEFAULT_TTL_SECONDS`）。「签一次、想半天、回来再试」是很常见的顺序，
> 所以登录页会显示「剩余 N 分钟」徽章 —— 看到负数/接近 0 先重签，别去怀疑别的（§10.1 坑 13）。

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

# ── 起完先自检一次（§1.5）──
uv run python -m scripts.check_web_cors    # 五项全绿再往下走
```

> 💡 页面顶部那排徽章可以立刻告诉你「连接建立到哪一步了」：
> `正在连接… Ns` / `已连接` / `连接已断开`。
> ⚠️ `正在连接… Ns` 的秒数若一直涨，说明握手没回来 —— 8 秒后会自动重连一次，
> 再不行就点旁边的「重连」（坑 16/17）。**这时消息也可以照发**：会排队到连接建立后自动发出。

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
node tests/run-live.mjs  # 真链路 10 条（需要后端在跑；「真溯源」那条连问两次）
```

> 📌 上表的条数是**第 16 步交付时**的实测值（保留作历史证据）。
> 变更 001~003 之后用例不断增补，当前实测值见 §7.2 与 [`issues/`](../issues/)。
> 它们之间**不是回归关系** —— 条数变了不代表坏，看 `fail` 才代表坏。

> ⚠️ 看页面时留意顶部那排状态徽章（坑 16/17 的出口）：
> `正在连接… Ns`（含**秒数**与「重连」按钮）/ `已连接` / `连接已断开`。
> 「N 秒还在连接」本身就是一个可观察的事实 —— 不用再靠感觉判断是不是卡住了。

---

## 6. 四个验收口径怎么测

### 6.1 验收第 1 条：两个入口可用 + 流式正常

* **入口**：`GET /` 与 `GET /internal` 都 200（§11 的自查命令里有四条路由的实测）；
* **流式**：真链路用例「真流式」断言三件事 ——
  ① 帧序号从 1 **连续递增**（丢帧只可能来自本地逻辑，`seq` 就是为它准备的）；
  ② 节点帧里出现过 `router`；③ 服务端自报 `first_frame_ms < 1000`（B2）。

### 6.2 验收第 2 条：RAG 回答带溯源

> ✅ **2026-10-17 判据已加严**（issue 009）：原来是「闭环」四步，但**第一步就错了** ——
> 「`citations` 非空」不等于「用户拿到了回答」。语义缓存命中时 `citations` 照常非空，
> 而回答是「抱歉，这个请求我无法处理」，于是这条验收在**完全坏掉的路径上全绿**。
> 现在的判据是「**答出来了**」+ 原闭环，两步缺一不可。

```text
① 用户真的拿到了回答（新加，是第一判据）
   flow_state == "delivered" 且 blocked == false
   且 回答正文里没有「无法处理 / 被合规护栏拦下」这类拦截话术
        ↑ 比断言 flow_state 更贴近用户看到的东西：话术对不上就是没答出来
② 溯源闭环（原来的四步，一条都不能少）
   citations 非空
     → parseCitation 能解析出文档名与 chunk_id（形状 ^[0-9a-f]{6,}$）
     → GET /api/kb?name=<文档名> 真的返回 200 且原文非空（点开不是 404）
```

⚠️ **必须连问两次**（`web/tests/live/live.test.ts` 的「真溯源」现在是 `for (attempt of [1, 2])`）：

第 2 次必然走**语义缓存**，而 issue 009 正是**只在第 2 次起发作**（第 1 次正常）。
只问一次的用例永远测不到那条路 —— 这就是它当初全绿的原因。

**「去掉就红」已验证**：把 `guardrail.py` 的缓存补事实那一段去掉、重启后端再跑，
这条用例立刻红，报错直接点名：

```text
AssertionError: 第 1 次提问没有答完（flow_state=blocked，blocked=true）：
  回答=抱歉，这个请求我无法处理。… —— 若这是第 2 次，先怀疑语义缓存那条路（issue 009）
```

### 6.2.1 缓存这一层怎么单独验（issue 010 的两个判据）

「空结果不写缓存」与「灌库清缓存」都不是前端能看到的，所以它们各有自己的验法：

```powershell
# ① 空结果不进缓存：问一句语料里真没有的，然后看该问法有没有进缓存
uv run python -m scripts.cache_report --sample 20
#    期望：查不到该问法的新条目（synthesis 的日志会打 `不写缓存：…0 条出处`）
#    反向判据（更要紧）：**有出处的答案必须照常写** —— 否则 E1 的收益被这条修法清零

# ② 灌库清缓存：改一份 kb 文档 → 灌库 → 看版本号变大
uv run python -m scripts.load_knowledge_base --source "docs/kb/退货政策.md"
#    期望：INFO app.cache.corpus: 知识库版本 → N（by=ingest:docs/kb/退货政策.md, …）
uv run python -m scripts.load_knowledge_base      # 内容没变再灌一次
#    期望：documents 8 / unchanged 8，且**版本不动**（不白清缓存）
```

⚠️ 这两条的失败**都没有运行期表现**（程序一切正常，只是缓存悄悄不对），
所以它们必须靠上面这两条命令 / 日志来判，别指望「页面看着正常」能证明什么。

### 6.3 验收第 3 条：角色权限在前端正确分流

* 离线：`tests/auth.test.ts` 断言 `canUseInternal` 的四个角色 + 演示身份卡片里的 role 都在权限表里；
* 真链路：客户令牌问「查全站所有订单」→ 前端不崩、后端如实拦住或降级（`flow_state` 非空、有回答）；
* **后端兜底**这一半由第 4/9 步的用例保证（本步不动它）。

### 6.4 验收第 4 条：对内报表可出图表（以及「出不来」怎么判）

```powershell
# 人工筛句式：5 种问法一次性跑完，看哪些出图
uv run python -m scripts.probe_internal_query
```

⚠️ **前提：后端必须已经在跑**。脚本对着真后端发请求（它依赖 lifespan 装配出的图）。
后端没起时脚本会打印一句可照做的提示并以退出码 1 结束（**不是** `JSONDecodeError`，
那个报错不指向真正原因 —— 2026-10-15 已改，见 `issues/` 第 1 条）。

**✅ 判定口径：看「失败原因能不能解释」，不是「必须 5/5」。**

首次修复绑定参数坑之后实测过 5/5 全出图：

| 问法 | 结果 |
|---|---|
| 各个订单状态的订单数量，用柱状图 | ✅ `bar` / 5 点 / `x=status` / `series=order_count` |
| 按订单状态统计订单数量 | ✅ 同上 |
| 统计每种订单状态各有多少笔订单，画成柱状图 | ✅ 同上 |
| 最近 30 天每天的订单金额趋势，用折线图 | ✅ `bar` / 5 点 / `x=day` / `series=amount`（模型选了柱状图，不是折线） |
| 销售额最高的 10 个商品，用柱状图 | ✅ `bar` / 10 点 / `x=name` / `series=product_id` |

⚠️ **但这条链路是非确定性的**：`analysis` / `synthesis` 节点要求 LLM **严格输出 JSON**
（`"4. 只输出 JSON，不要解释。"`），模型偶尔给不出来就重试 2 次后放弃 → **这一次没图**。
**同一句话重跑，结果可能不同**。2026-10-15 迁移验收时复测 5 句 × 2 轮：

| 问法 | 出图率 |
|---|---|
| 各个订单状态的订单数量，用柱状图 | 2/2 |
| 按订单状态统计订单数量 | 1/2 |
| 统计每种订单状态各有多少笔订单，画成柱状图 | 2/2 |
| 最近 30 天每天的订单金额趋势，用折线图 | 0/2 |
| 销售额最高的 10 个商品，用柱状图 | 1/2 |

失败原因只有两类，**都不是前端问题**：

* `analysis 节点的 LLM 连续 2 次未给出可解析的 JSON 对象（not_json）` → 模型输出格式不稳；
* `没有可分析的数据行` → 那次 `data` 节点没查出数据（Text-to-SQL 的写法波动）。

> 📌 **所以别把单次「少于 5 句」当成回归失败**。要确认「出图功能本身没坏」，
> 只需要**至少有一句稳定出图** + 失败项的原因是上面两类之一。
> 想追「模型为什么给不出 JSON」属于第 7 步 `analysis` 节点的 prompt/解析健壮性，
> 与第 16 步的前端链路无关（已在 `issues/` 第 1 条记录）。

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

### 7.1 四层证据，各自回答不同的问题

| 层 | 文件 | 条数 | 回答的问题 |
|---|---|---|---|
| **纯逻辑**（离线） | `tests/stream.test.ts`（15）/ `citations`（9）/ `chart`（11）/ `auth`（20）/ `transport`（23）/ `diagnostics`（6）/ `config`（10） | **94** | 「帧序列意味着什么」「出处怎么拆」「spec 怎么画」「角色怎么分流」「子协议发了什么」「探活失败怎么说」「令牌类型怎么认」「配置指错会怎样」「僵尸连接会不会污染状态」「卡住的连接会不会自己超时」 |
| **接线断言**（离线，读源码） | `tests/hook-wiring.test.ts` | **7** | 「兜底真的被触发吗」：依赖数组有没有漏项 / `onOpen` 有没有 flush 排队消息 / `running` 里有没有混进 `connecting`（**坑 17 唯一能抓到的一层**） |
| **真链路**（Node 打真服务） | `tests/live/live.test.ts` | **10** | 「我们抄的契约是真的吗」：真 WS 帧序列 / 真出处 / 真 `chart_spec` / 4401 / `/api/kb` 越界 / **跨源头** |
| **服务端跨源** | `tests/api/test_cors.py` | **11** | 「浏览器读得到响应吗」：允许来源 / 白名单外 / 预检 / 预检不吃配额 / 错误响应带头 |
| **构建** | `npm run build` | — | 「能不能出产物」：9/9 静态页、6 条路由、0 类型错 |

> ⚠️ 四层里**没有一层能替代浏览器**（§7.5 / §7.6）——
> 「页面真的渲染出来、按钮真的能点」这件事，本步仍然需要人眼（§5.2 / §5.3 就是那个清单）。

### 7.2 离线用例：一条命令，不连任何后端

```powershell
cd web
npm test
# 实测：tests 102 / pass 102 / fail 0（0.26s）
```

### 7.3 用例的三条纪律（沿用前几步的口径）

1. **不测实现细节，测契约**：断言的是「发出的子协议是什么」「缺值时图上是不是 `null`」
   「过期令牌会不会被清掉」，而不是「函数被调了几次」；
   ⚠️ 唯一的例外是 `hook-wiring.test.ts`：它**故意**测源码形状 ——
   因为「依赖数组漏项」这类 bug 没有任何运行期表现（坑 17），
   而那条纪律的本意是「别测实现细节」，不是「宁可漏掉一整类 bug」；
2. **零依赖优先**：102 条离线用例不连库、不连模型、不起浏览器，跑完 0.26s ——
   所以它们可以被反复跑（真链路那 10 条要 42~143s，且有模型成本）；
3. **两种证据都要**：离线用例用的是**抄来的** fixture，抄错了会「过了但真链路还是坏的」；
   所以真链路用例拿真数据过一遍**同一个**函数（§7.4 / §7.6）。

### 7.4 为什么真链路用例用**同一份客户端代码**，以及为什么要重试

* **同一份代码**：真链路用例直接 `import { openChatStream } from "../../src/lib/stream.ts"` ——
  浏览器里跑的就是它。另写一个「测试专用 WebSocket 客户端」看起来很干净，
  但它会因为写得跟真客户端不一样而给出**虚假的安全感**；
* **重试**：`chart_spec` 那条用例最多试 3 次。理由写在用例注释里：
  出图链路上有一段非确定性（Text-to-SQL），而这条用例要断言的是**前端适配层**，
  不是「模型今天稳不稳」 —— 把模型的随机失败算成前端用例的红，最后会让人无视这组用例，
  那比没有用例更糟。

### 7.5 「真链路全绿」为什么不能证明浏览器能打开页面（本步最贵的一课）

真链路用例跑在 **Node** 里，而 **Node 不受 CORS 约束**：Node 的 `fetch` 会把你带的
`Origin` 头原样发出去、把响应原样读回来，哪怕响应里一个 CORS 头都没有。

实测代价（第 16 步）：后端当时**一个 CORS 中间件都没有**，而
`node tests/run-live.mjs` **9 条全绿**；同一时刻浏览器打开页面只得到一句
`Failed to fetch`（服务端日志里全是 200）。也就是说：

| 证据 | 能证明 | **不能**证明 |
|---|---|---|
| `npm test`（离线） | 前端逻辑对 | 真后端、真浏览器 |
| `node tests/run-live.mjs` | 真后端 + 真前端的**服务端侧**契约对 | **浏览器跨源这一层**（Node 不看 CORS） |
| `uv run python -m scripts.check_web_cors` | 浏览器读响应所依赖的那个头在 | 页面渲染（需要人眼或 Playwright） |

**处置**：把「跨源」这件事用**三处**钉住（缺一处就会再退回到「Node 绿 / 浏览器红」）：

1. `tests/api/test_cors.py`（服务端，11 条）：拿 `Origin` 打进去看响应头，含预检与错误响应；
2. `web/tests/live` 里的「浏览器跨源这一层」：对**真服务**做同样的断言（Node 能验到「头在不在」）；
3. `scripts/check_web_cors.py`：给人用的一条命令，附修法。

⚠️ 剩下的那一格（**页面真的渲染出来了吗**）本步**没有**自动化覆盖 ——
没有 Playwright 依赖，也没有做（§8 的接线点）。所以 `16.web.md` §8 的验收第 1 条里
「两个入口可用」这一半，最终仍需要**人眼在浏览器里确认一次**（§5.2 / §5.3 就是那个清单）。

### 7.6 为什么必须拿**真令牌**过一遍前端判定（坑 13 的教训）
坑 13（§10.1）的成因值得单独讲一遍，因为它是**测试方法本身的漏洞**，不是某一行代码写错：

```text
实现：读 payload.type          ← 错（后端写的是 payload.typ）
测试：fakeJwt({ type: "access" })  ← 按同一个错理解造的 fixture
结果：14 条用例全绿，真令牌 100% 被拒
```

> **手写 fixture 只能证明「实现与我对契约的理解一致」，不能证明「我的理解对」。**

所以本步的证据链里必须有一格是**真来源**：

| 数据 | 来源 | 谁在用 |
|---|---|---|
| 帧形状 / 信封形状 | 真后端（真链路用例） | `parseFrame` / `parseChatResponse` |
| **令牌形状** | **后端真签**（`scripts/dev_token.py`） | `decodeJwtPayload` / `tokenKind`（坑 13 的收尾验证） |
| 出处串形状 | 真库（`scripts/probe_rag_sources.py`）+ 真链路用例 | `parseCitation` |
| `chart_spec` 形状 | 真后端（真链路用例） | `toEChartsOption` / `toTableView` |

⚠️ 反过来说：`tests/auth.test.ts` 里那些 `fakeJwt` 用例**不是没价值** ——
它们覆盖的是「过期 / 坏段 / 类型混用 / 存储清理」这些**分支**，跑 0.2s、可穷举。
两类数据的**分工**才是重点：fixture 管分支，真数据管形状。

### 7.7 哪些 bug 是「四层证据都看不见」的（坑 17 补上的第五层）

坑 15/16/17 三个 bug 连起来看，能得到一张**「什么证据能抓到什么」**的对照表 ——
比任何一条用例都值钱：

| bug | 纯逻辑单测 | 类型检查 / 构建 | 真链路（Node） | 浏览器手工 | 抓它的是 |
|---|---|---|---|---|---|
| 帧序列写错（§3.2） | ✅ 抓到 | — | ✅ 抓到 | — | `stream.test.ts` |
| 令牌读错键名（坑 13） | ❌ fixture 与实现同错 | — | ❌ 真链路没测 | ✅ 能复现 | **真令牌过一遍**（§7.6） |
| 缺 CORS（坑 11） | ❌ | ❌ | ❌ Node 不看 CORS | ✅ 能复现 | **服务端侧断言 + 自检**（§7.5） |
| 僵尸连接（坑 15） | ✅ 抓到 | — | ❌ | ✅ | `transport.test.ts` |
| 卡在 connecting（坑 16） | ✅ 抓到（看门狗） | — | ❌ | ✅ | `transport.test.ts` |
| **重连是哑的（坑 17）** | ❌ 逻辑没错 | ❌ 不是类型错 | ❌ Node 里没有 React | ✅ 能复现 | **源码级接线断言**（`hook-wiring.test.ts`） |
| **缓存命中被判定成编造（issue 009）** | ❌ 逻辑没错 | ❌ | **❌ 当时的真链路只断言 citations → 全绿** | ✅ 能复现 | **真链路「连问两次 + 断言 delivered」**（§6.2，已改） |
| **灌库不清缓存（issue 010）** | ❌ 逻辑没错 | ❌ | ❌ | ❌ 页面看着完全正常 | **版本号断言 + `rebuild` 后 `cache_report`**（§6.2.1） |

> ⚠️ 最后两行是**2026-10-17 补的**，它们的共同点是「**所有旧证据都显示正常**」：
> issue 009 让真链路全绿而浏览器全红；issue 010 让页面看起来一切正常、
> 只是答案过时。这两类恰恰是最难靠「跑一遍看看」发现的。

结论（写下来免得下次重犯）：

> **「逻辑对」≠「接线对」**。依赖数组、事件注册、`onOpen` 里的 flush 都属于**接线**，
> 它们没有类型、没有运行期表现、也不会让任何逻辑单测变红 ——
> 一旦写错，表现是「你加的兜底**从来没被触发**」，而所有证据都显示一切正常。

⚠️ **还有一条更贵的**（issue 009 换来的）：

> **「断言的东西在」≠「用户拿到了东西」**。第 16 步原来把「有 citations」当作
> 「溯源可用」，而缓存命中时 citations 照常非空、回答却是拦截话术 ——
> 于是验收在一条 100% 不可用的路径上全绿。
> **验收要断言「用户得到了什么」，而不是「某个中间产物存在」。**

⚠️ 第五层（源码级断言）的**边界**：它挡的是「有人把依赖删了 / 忘了加」，
挡不住「依赖写了但逻辑顺序错」。真正的运行期覆盖要浏览器（§8 的 Playwright 接线点）。

### 7.8 收尾后确认演示库状态

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
| 生产部署 | `npm run build` 产物 + `npm start` | ✅ **已落地**：`web/Dockerfile`（多阶段 + `output: "standalone"`）+ 生产 compose 的 `web` 服务；部署步骤见 [`17.docker-ops.md`](17.docker-ops.md) §3.4、§5.2（`NEXT_PUBLIC_API_BASE` 是**构建期**变量） |
| 端到端浏览器自动化 | 无（真链路用 Node 跑客户端代码；接线用源码级断言） | 需要截图/点击回归时加 Playwright（届时把那 10 条真链路用例平移过去，并补上「点一下按钮」这类**只有浏览器能验**的动作） |
| CI | 未挂 | 第 2 步的 `16.infra.md`：`npm ci --ignore-scripts && npm run typecheck && npm test && npm run build` |

---

## 9. 故障速查（按症状）

### 9.1 前端侧

| 症状 | 先看哪里 | 多半是 |
|---|---|---|
| **一直「正在连接… Ns」、输入框点不动（点「新会话」就好了、刷新又卡）** | 顶部徽章的秒数 + 「重连」按钮 | **重连是哑的**（effect 依赖漏项）—— §10.1 坑 17；现在会显示秒数、消息排队、8s 后自动重连 |
| **提问后一直转圈，没有回答也没有报错** | 后端是不是刚被重启/在冷加载 | 连接被静默切断，或一轮超过 180s（坑 16 的等回帧看门狗） |
| **一切请求都 `Failed to fetch`，而服务端日志是 200** | `uv run python -m scripts.check_web_cors` | **跨源被拦**（后端没配 `API_CORS_ORIGINS`）—— §10.1 坑 11。**别去重签令牌** |
| **HTTP 那半正常，但一进对客页就提示「WebSocket 连接失败」** | 页面上那行「后端 … / 流式 …」+ 自检 ⑤ | **地址指到了前端自己**（`NEXT_PUBLIC_API_BASE` 是构建期变量，改了要重启 dev）—— §10.1 坑 14 |
| **聊天能用，但时不时弹「WebSocket 连接失败」（URL 还是对的）** | 后端日志里同一端口有没有两条 `accepted` | **被抛弃的旧连接**在报错（StrictMode 双挂载 / 热更新）—— §10.1 坑 15，已由世代守卫修掉 |
| **登录页说「这是刷新令牌」，而令牌明明是刚签的** | 粘贴后看那排徽章：`typ=` / `剩余 N 分钟` | ① 声明键名读错（**已修**，坑 13）；② 真的过期了（默认只活 30 分钟，看「剩余」徽章） |
| 页面能开，一问就「什么都不发生」 | 浏览器 Console + Network | WS 握手失败（子协议）/ 后端没起 / `NEXT_PUBLIC_API_BASE` 指错（§10.1 坑 7） |
| 用 `http://<本机 IP>:3000` 打开时 HTTP 全红、但流式却能连 | `check_web_cors --web http://<本机 IP>:3000` | 网卡来源不在白名单（坑 14 附带那条）—— 加进 `API_CORS_ORIGINS` 或改用 127.0.0.1 |
| 顶栏是亮的但正文一直「正在检查登录状态…」 | Console 有没有报错 | `localStorage` 不可用（隐私模式）→ 退化为内存存储，本会话可用 |
| 一进 `/` 就提示「需要先登录」 | `/login` 里的令牌状态徽章（含「剩余」） | 令牌过期 / 复制时缺了一段（这与上面的 `Failed to fetch` **是两件事**） |
| 回答出来了但没有「来源」 | 先看后端日志有没有 `retrieve_knowledge` 的 tool_error | 向量库空（跑过全量回归）或问法没走知识库（§6.5） |
| **同一句话第 1 次答得好好的、第 2 次起变成「抱歉，这个请求我无法处理」，而下面的「来源」还在** | `uv run python -m scripts.cache_report --sample 20`，看该问法有没有 `hit` 记录 | **语义缓存命中被判成编造**（issue 009，已修）。修复前的判据：`flow_state=blocked` 但 `citations` 非空 —— 光看「有没有出处」看不出来 |
| **知识库明明灌好了，某些问法仍答「没有找到相关知识库片段」** | 换一句没问过的说法再问一次 | 缓存里躺着一条「检索回 0 条」时的旧答案（issue 010）。换措辞能答上就说明语料是好的；灌库会自动清缓存（见坑 6） |
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

### 10.1 本步实测撞到的 15 个坑

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

✅ **2026-10-17 起：灌库会自动清语义缓存**（issue 010）。`load_knowledge_base` 现在会给
`ingest_document` / `ingest_path` 传 `on_change=`，只要有文档真的变了就推进
`corpus_revision`，全批旧缓存条目按 `fingerprint` 立即失效。判据是日志里那行：

```text
INFO app.cache.corpus: 知识库版本 → 10（by=ingest:docs/kb/退货政策.md, document_version=2）
```

⚠️ 两条配套认知：

1. **只有「真的变了」才推进**（`report.doc_changed`）——同一份语料重灌一遍，
   版本不动（`documents 8 / unchanged 8`，revision 不变），不会把缓存白清一遍；
2. **灌库解决不了「检索当时回 0 条」被缓存下来的那一半** —— 那一半在 2026-10-17 也修了：
   `synthesis` 现在**不缓存空结果**（判据：这一轮走了知识库分支 且 `citations == []`）。
   日志里会看到 `不写缓存：本轮走了知识库但有 0 条出处`。
   所以「灌完库还答不上」现在只剩两种可能：语料没真灌进去（跑 §1.1 第 ④ 条确认），
   或那条旧缓存是**修好之前**写进去的（跑一次 `uv run python -m scripts.cache_report --purge-all --yes`
   清掉，或按 issue 010 的判据先看清是哪一条）。
   最省事的自检仍然是：**换一句没问过的说法再问一次** —— 换措辞能答上，
   就说明语料是好的，卡在缓存这一层。

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

**坑 11：浏览器里一切请求都 `Failed to fetch`，而服务端日志里全是 200（**后端当时没有 CORS**）**

这是本步**唯一一个「Node 侧全绿、浏览器侧全红」**的坑，也是整套手册里最值得记住的一条。

```text
页面（登录 页的探活）：
  连不上后端（http://127.0.0.1:8000）：Failed to fetch

后端日志（同一时刻）：
  INFO: 127.0.0.1:5xxxx - "GET /health HTTP/1.1" 200 OK
```

**成因**：前端在 `http://127.0.0.1:3000`、API 在 `http://127.0.0.1:8000` ——
**端口不同就是不同源**。第 9 步的 API 从来没有跨源调用方（演示脚本与测试都是同进程 / Node），
所以 `app/api/main.py` 里一个 CORS 中间件都没注册。浏览器发出请求、也**拿到了 200 响应**，
但因为响应里没有 `Access-Control-Allow-Origin`，它**拒绝把响应交给页面** ——
页面侧看到的就是一句没有任何状态码的 `TypeError: Failed to fetch`。

> **判据（先记这一条，比记代码有用）**：
> `fetch` 抛 `TypeError`（拿不到任何状态码）= **响应没被读到**，三种可能：
> ① 服务没起 / 地址写错；② **跨源被拦**；③ 网络/证书。
> **「令牌无效」不在其中** —— 令牌无效会得到 **401**，那是一个能读到的正常响应。
> 所以看到 `Failed to fetch` 时，**别去重签令牌**（那是这个坑最常见的错误反应）。

**修法（已在仓库里）**：

1. `app/api/config.py` 新增 `cors_origins()`：读 `API_CORS_ORIGINS`（逗号分隔）；
   开发期默认 `http://localhost:3000` + `http://127.0.0.1:3000`（两个都列是有原因的，见下）；
   `ENV=prod` 且未配置时为**空**（不允许任何跨源）；
2. `app/api/main.py` 加 `CORSMiddleware`，并且**必须最后加 = 最外层**（见 §3.6）；
3. 登录页的探活失败提示语改成 `lib/diagnostics.ts::probeFailureHint()` ——
   第一句就是「先别怀疑令牌」，并给出三条可执行动作（起后端 / 查地址 / 查 CORS）。

⚠️ **两个容易配错的细节**：

* `localhost` 与 `127.0.0.1` 在浏览器眼里是**不同的来源**，只配一个的症状是
  「用 localhost 打开能用、换 127.0.0.1 就失败」（反之亦然）—— 这种「换个地址就坏」最容易被归因成别的东西；
* 来源**不加末尾斜杠**：浏览器发的 `Origin` 头没有斜杠，配成 `http://host:3000/` 永远匹配不上。
  代码里做了 `rstrip("/")`，配置校验也会拒绝漏协议 / 写 `*` 的值（fail-fast，与限流表达式同一口径）。

⚠️ **这一层不是鉴权**（写在这里免得有人拿它当门）：CORS 只约束**浏览器**，
`curl` / Node 完全不受它约束 —— 真正的门是 `Authorization: Bearer <JWT>`。

**为什么这组用例必须写在服务端**：本步的真链路用例是 **Node 直接 fetch**，
它**永远看不到 CORS 的效果**。所以「真链路 9 条全绿」不能证明浏览器能打开页面。
现在三处都钉住了：`tests/api/test_cors.py`（11 条，含预检、错误响应、白名单）、
`web/tests/live` 里的「浏览器跨源这一层」（拿 `Origin` 打真服务）、
以及 `scripts/check_web_cors.py`（一条命令的现场自检，见 §1.5）。

**坑 12：前端与后端都得**由人起**，前端页面会把「后端没起」显示成「令牌无效」**

本机没有把这两个进程做成服务（`2.infra.md` 的容器化到第 19 步才做）。所以：

* 关掉终端 / 会话结束 → 进程就没了 → 页面报 `Failed to fetch`；
* 这时候用户的第一反应往往是「那我去重新签一个令牌」—— **令牌是好的**，
  重签一万次也没用（令牌只在服务端活着时才可能验通过）。

**判据**：`Invoke-RestMethod http://127.0.0.1:8000/health` 都连不上，
说明是进程/网络问题，与令牌无关（§10.2 第 1 条）。
**修法**：两个终端分别起后端与前端（§5.1），或用 §1.5 的一条命令自检「两个进程 + 跨源」都正常。

**坑 13：令牌类型声明的键名是 `typ`，前端读的是 `type` —— **所有真令牌都被拒**

这是本步第二类「Node 侧全绿、浏览器全红」的坑（坑 11 是跨源，这一条是**契约读错键名**），
而且它的报错**看起来非常具体、非常可信**，所以更难查：

```text
登录页（粘贴一个刚签出来的、完全正确的访问令牌）：
  这是刷新令牌（`type=refresh`），后端会当 401 处理 —— 用 --kind access 重新签发一个。

而实际：
  payload = {"sub": "7", "role": "internal_employee", "typ": "access", ...}
                                                      ↑ 后端写的键名是 typ
  后端 parse_token(同一个令牌) → Principal(user_id=7, role='internal_employee', token_type='access')  ✅
```

**成因**：后端 `app/security/auth.py::_create_token` 写的是 `{"typ": token_type}`，
`parse_token` 也读 `payload.get("typ")`；而前端第一版读的是 **`type`**（OAuth 里常见的命名）。
于是前端**永远**读不到类型 → 所有令牌都被判成「不是访问令牌」→ 登录页一律拒绝。
**后端的提示语是对的、令牌也是对的** —— 错的是前端那一行读取，
但症状（「令牌有问题」）把注意力全引到了令牌上。

⚠️ **为什么它躲过了整套测试**：`web/tests/auth.test.ts` 里的 `fakeJwt(...)` 是**手写的形状** ——
而它是按 `{"type": "access"}` 造的，也就是说**测试与实现犯了同一个错**，
于是 14 条用例全绿，真令牌却被拒。这条教训比 bug 本身值钱：

> **手写 fixture 只能证明「实现与我对契约的理解一致」，不能证明「我的理解对」。**
> 所以真链路用例必须拿**后端真签的**令牌过一遍前端的判定（§7.8）。

**修法（已落地，三处）**：

1. `web/src/lib/auth.ts::decodeJwtPayload` **两个键名都认**（`payload.typ ?? payload.type`），
   并新增 `tokenKind()` 返回三态（`access` / `refresh` / **`unknown`**）；
2. 登录页把「声明是 refresh」与「没有类型声明」**分开报**（第一版混成一句「这是刷新令牌」——
   正是这句话把「键名读错」伪装成了「令牌签错」）；
3. 登录页新增「剩余 N 分钟」徽章：本仓演示令牌默认只活 **30 分钟**，
   而「签一次、想半天、回来再试」是常态 —— 不显示剩余时间的话，过期也会被读成「令牌类型不对」。

**离线用例**（`web/tests/auth.test.ts`，新增 6 条）：`typ` 必须被认 / `type` 也认 /
三态区分 / `typ` 优先 / 刷新令牌被拒 / **类型缺失同样被拒**（与后端 `parse_token` 的判据一致：
后端对缺失类型也报「收到 (缺失)」）。

**真令牌验证**（这次修复的收尾动作，见 §7.8）：拿 `scripts/dev_token.py` 真签的两个令牌，
过一遍前端页面用的**同一份**判定代码：

```text
✅ access（--kind 默认）   payload 键 = sub,role,typ,iat,exp,jti   payload.typ = access
                          前端解出 = {"sub":"2","role":"internal_employee","exp":…,"tokenType":"access"}
                          isAccessToken=true  tokenKind=access
✅ refresh（--kind refresh）payload.typ = refresh   → isAccessToken=false  tokenKind=refresh
```

**坑 14：HTTP 那半是好的、WebSocket 那半一定死（`NEXT_PUBLIC_API_BASE` 指到了前端自己）**

症状（实测报告）：**对客页能与 AI 对话**，但一进页面就提示
「WebSocket 连接失败：先确认后端在跑…」。而 HTTP 侧完全正常 —— 这正是它难查的地方。

三条对照实验（把「后端问题」与「地址问题」分开，Node 的 WebSocket 直接给结论）：

```text
① ws://127.0.0.1:8000/api/chat/stream   → open，子协议=shopsage.v1（54ms）   ← 后端完全正常
② ws://localhost:8000/api/chat/stream   → open（27ms）
③ ws://127.0.0.1:3000/api/chat/stream   → error 事件（8s 超时）              ← 页面上的症状
```

**成因（一条容易被忽略的不对称）**：HTTP 侧有**两个**出口，而 WebSocket 只有**一个**：

| 出口 | 谁在答 | base 指错时会怎样 |
|---|---|---|
| `/api/kb`（出处原文） | **Next 自己**（同源） | **永远正常** ← 「半通」错觉的来源 |
| `/api/chat`（对内报表） | 后端（走 `NEXT_PUBLIC_API_BASE`） | 打到 Next 自己 → 404/500，像「后端坏了」 |
| `WS /api/chat/stream`（对客流式） | 后端（走同一个 base） | **一定失败** —— Next 的 dev server **不代理 WebSocket upgrade** |

所以「HTTP 看着好、WS 死」= 十有八九是**地址**问题，不是后端问题。
⚠️ 附带一条：`NEXT_PUBLIC_API_BASE` 是**构建期**变量 —— 设错之后即使改 `.env` 也没用，
必须**带着正确的值重启 `npm run dev`**（或重新 `npm run build`）。

**修法（两层，缺一不可）**：

1. **代码层：能纠正的自动纠正，且必须说出来**（`web/src/lib/config.ts::resolvedApiBase`）：
   `NEXT_PUBLIC_API_BASE` 指到**页面自己**时，自动改用 `http://127.0.0.1:8000`；
   页面上同时显示「后端 … → 实际用 …」并弹一条配置提示
   —— 这是**有声音的降级**，不是静默兜底（静默兜底会让「配置到底生效没有」永远说不清）。
   判据：`isSelfReferentialBase()`（`new URL(base).origin === new URL(pageOrigin).origin`）。
2. **提示层：`socket_error` 的话术要带 URL 与可执行动作**。
   第一版只有一句「先确认后端在跑」，而实测那次后端**完全正常** ——
   方向错了，排查时间就全花在没问题的东西上。现在它打印**实际试过的 URL**、
   指向 `scripts/check_web_cors.py`、并在 URL 指向前端自己时直接给出重启命令。

**为什么不让 Next 去代理 WebSocket**（一个被否掉的方案）：Next 的 App Router **不支持**
在 route handler / rewrite 里代理 WebSocket upgrade（那要自建 server）。也就是说
「页面同源转发 WS」这条路在本仓的形态下走不通 —— 把它写在这里，免得下一个人再试一遍。

**顺带实测到的一个相关坑（网卡地址）**：`next dev` 会打印一行
`Network: http://192.168.1.13:3000`，照着它打开页面时：
HTTP 侧**会失败**（那个来源不在白名单里 → 没有 `Access-Control-Allow-Origin`），
而 WebSocket 侧**却能升级成功**（WS 不受 CORS 约束）。
两个方向相反的结论叠在一起 = 「界面能开、流式也能连、但一问就 Failed to fetch」。
处置：要么把该来源写进 `API_CORS_ORIGINS`，要么就用 `http://127.0.0.1:3000` 打开。
`check_web_cors.py` 现在会**认出来这是网卡地址**并直接说这两条（§1.5）。

**坑 15：聊天能用，但时不时弹「WebSocket 连接失败」—— 报错来自一条**已经没人要**的旧连接**

症状（实测报告）：**对客页一切正常、回答也能出来**，但页面上时不时出现
「WebSocket 连接失败：ws://127.0.0.1:8000/api/chat/stream（子协议 shopsage.v1）…」。
注意 URL 是**对的**、后端也**是好的** —— 这条报错与「连不上」无关。

两条现场证据：

```text
后端日志：同一个客户端端口出现两条 /api/chat/stream accepted（其中一条被强行关掉）
          INFO: 127.0.0.1:52269 - "WebSocket /api/chat/stream" [accepted]
          INFO: 127.0.0.1:52270 - "WebSocket /api/chat/stream" [accepted]
          Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)
          ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。

Node 实测（建连后立刻 close）：
          ① 立刻 close(1000)          → error → close(1006)     ← 没有 open！
          ② 等 300ms 再 close(1000)   → open → close(4401)
```

两条证据拼起来就是成因：

1. **React 开发模式（StrictMode）会把 effect 跑两遍**：第一条连接被 cleanup `close()` 掉，
   第二条接管（热更新 Fast Refresh 同理）。所以后端看到「同一端口两条连接」；
2. **在 `CONNECTING` 阶段 `close()` 会得到一个 `error`**（浏览器还没握手就收到关闭请求），
   以 `close(1006)` 收尾 —— 事件序列是 `error → close(1006)`，**看起来完全像连接失败**；
3. 旧连接的事件仍然会到达它的监听器，而第一版**照单全收**：
   `push()` 里直接 `options.onState(...)` —— 于是**一条已死连接的错误覆盖了正在工作的那条连接的状态**，
   用户看到的就是「聊得好好的忽然报连接失败」。

**修法（两处，各治一半）**：

1. **世代守卫**（`activeGeneration`）：每次建连领一个号，`push()` 与 `open/close/error` 回调
   先问一句「我还是当前那条连接吗」，不是就**直接扔掉**。
   这条治的是「旧连接不许改状态」，而且**连回调也要挡**（`onClose` 会改调用方的状态）；
2. **主动关闭不算失败**（`intentionalClose`）：`close()` 之后到达的 1006 只作为**信息**记下来
   （`closeCode` 保留），不再渲染成「连接失败」。这条治的是「我们主动收线不该报错」——
   不然每次 StrictMode 双挂载、每次热更新、每次切页都会在界面上留一句假报错。

**用例**（`web/tests/transport.test.ts`，新增 3 条）：旧连接再抛 `error` 不许污染当前状态 /
旧连接的 `error` 不进当前状态 / **主动 close 之后的 1006 不算失败**。
两条守卫都做了「去掉就红」的验证 —— 否则它们只是装饰。

⚠️ 这条坑的通用教训：**WebSocket 的生命周期不是「一条连接一个组件」**。
React 的双挂载、热更新、切页都会产生「被抛弃但仍会发事件的连接」，
而它们的 `error`/`close` 与真实故障**长得一模一样**。
凡是共享一份状态的地方（我这里就是 hook 里的 `stream` state），都要有世代号。

**坑 18：照 `next dev` 打印的 Network 地址打开页面 → 页面能开、WS 也连得上，但「连不上」且发消息没反应**

坑 14 附带那条（网卡地址不在白名单）的**实测复发记录**（第二次踩，值得单列成一条完整的坑）：

```text
现象（用户原话）：对客入口进入页面后一直无法连接，也无法正常与助手对话。
                 —— 而且「两个进程都已经起了、真链路 10 条全绿」之后**依然如此**。

三条现场证据（缺一条就会误判）：
  ① 后端日志：只有 WebSocket accepted，**一条 POST /api/chat 都没有**
  ② 前端日志：⚠ Cross origin request detected from 192.168.1.13 to /_next/* resource
              （说明浏览器实际是用**网卡地址**打开的页面）
  ③ 自检（唯一能一次定位的一条）：
     uv run python -m scripts.check_web_cors --web http://192.168.1.13:3000
     ③ ❌ Access-Control-Allow-Origin = （空）   ④ ❌ 预检 HTTP 400   ⑤ ✅ WS 101
```

**为什么症状是「既连不上、又没反应」**（两条相反的结论叠在一起）：

| 出口 | 受不受 CORS 约束 | 网卡地址打开时 |
|---|---|---|
| `WS /api/chat/stream` | **不受**（WS 不跨源） | ✅ 能升级 —— 所以前端**看着像连上了** |
| `GET /health`（登录页探活） | 受 | ❌ 读不到响应 → 页面显示「连不上后端」 |
| `POST /api/chat`、`/api/chat/approve` | 受 | ❌ 一律失败 → 发消息后没有任何反应 |

⚠️ 还有一个更隐蔽的后果：`OPTIONS` 预检不是 200 而是 **400**（来源不合格时预检本来就该失败），
所以「跨源被拦」在浏览器里表现为**两种不同的错**（探活是 `Failed to fetch`、预检是 400）——
别把它们当成两个问题。

**修法（`.env`，然后必须重启后端）**：

```powershell
# .env —— ⚠️⚠️ 这一项一旦设置就【完全替代】开发期默认值（默认只有 localhost:3000 与 127.0.0.1:3000），
#      不是「追加」。所以三个来源都要列上，少一个就又变成「换个地址打开就坏」。
API_CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000,http://192.168.1.13:3000
# 然后重启后端（.env 是启动时读的，坑 8 同一类顺序问题）
```

**两条纪律（这条坑真正的教训）**：

1. **网卡地址是 DHCP 给的，会变**。本机实测被 `next dev` 打印过 `.156`，后来变成 `.141` ——
   `.env` 里写死的那一行早晚会失效，而症状与这次**一模一样**（用户只会说「又连不上了」）。
   ⚠️ 所以**首选 `http://127.0.0.1:3000` 或 `http://localhost:3000` 打开页面**（默认就在白名单里）；
   只有真的要给「局域网里另一台设备」访问时才需要配网卡地址。
   IP 变了怎么查：`check_web_cors --web http://<新IP>:3000`，它会直接给出该写进 `.env` 的那一行。
2. **「真链路全绿」不等于「浏览器能用」**。这次的顺序很值得记住：
   我先跑完 `node tests/run-live.mjs` **10 passed**，而用户的浏览器**依然不能用** ——
   因为真链路是 Node 直接 fetch（**永远看不到 CORS 的效果**，§7.5 / 坑 11）。
   判据只有一个：**`check_web_cors --web <你实际打开的那个地址>`**，来源必须**逐字**对上
   （`localhost` / `127.0.0.1` / 网卡地址是**三个**不同来源）。

**坑 19：页面长期开着时，后端被重启 / 前端热更新 → 徽章卡在「正在连接… Ns」，发消息无反应**

坑 15/16/17 的**配套现象**，第三次报「一直无法连接」时实测定位到的那一层：

```text
用户现象：访问 http://127.0.0.1:3000（**来源本来就是对的**）：
          徽章「正在连接… Ns」秒数一直涨；发消息没有任何反应。
Console：  stream.ts:529 WebSocket connection to 'ws://127.0.0.1:8000/api/chat/stream' failed:
           WebSocket is closed before the connection is established.
```

**`stream.ts:529` 就是 `socket.close(1000)` 那一行** —— 也就是说，那句报错是**客户端自己**
主动收线造成的（握手看门狗 8s 超时，或 React 双挂载 / 热更新的 cleanup），
**不是服务端拒绝**。判据三件事可以同时确认：

| 判据 | 实测 |
|---|---|
| 控制台行号是否落在 `close()` 上 | ✅ `stream.ts:529` = `socket.close(1000)` |
| 后端日志有没有收到这次握手 | ✅ `WebSocket /api/chat/stream [accepted]` + `connection open`（**accept 发生在鉴权之前**，所以有日志只代表「到了后端」） |
| 裸 socket 自己完成一次握手 | ✅ `HTTP/1.1 101` + `Sec-WebSocket-Protocol: shopsage.v1`（服务端完全正常） |

**成因（三条叠在一起，各占一部分）**：

1. **本仓后端没有 `--reload`**（坑 8），所以每次改后端代码都得**手动重启** ——
   而重启会让**所有已建立的 WS 连接一起断**（浏览器侧就是一次 1006）；
2. **React 开发模式的双挂载 + 热更新**会在 `CONNECTING` 阶段就 `close()` 掉一条连接（坑 15），
   那条连接必然在控制台留下「closed before the connection is established」——
   **它是噪音，不是根因**；
3. 真正让人卡住的是**界面状态**：自动重连「最多一次」由 `attemptsRef` 计数，
   而 `reconnect()`（以及 `send()` 在 `failed` 分支里）会把它**重置为 1** ——
   于是「重连→失败→点重连」这个循环里，徽章会反复回到「正在连接… Ns」，
   用户看到的就是「秒数一直涨、什么都发不出去」。

**修法（按代价从低到高）**：

1. **硬刷新页面**：`Ctrl+F5`。多数情况到此为止（页面里那条坏连接与坏状态一起没了）；
2. **还不行就关掉这个标签页重开**：热更新有可能让 React 树停在坏状态，`Ctrl+F5` 未必救得回来；
   这也是「为什么开发者自己那边永远是好的」—— 开发者的页面是刚打开的；
3. 重新登录（本仓演示令牌**只活 30 分钟**，坑 13 那条徽章会显示剩余分钟数）；


**坑 20：握手成功了，但 `phase` 永远停在 `connecting` → 页面**一条消息都发不出去**（真 bug，已修）**

坑 19 是「环境 + 噪音」，这一条是**前端的真逻辑缺陷**：它让对客页在**后端完全健康**时
100% 不可用，而且**刷新页面修不好** —— 这正是与坑 19 的关键区别（坑 19 刷新/重开标签页就能好）。

```text
用户现象：徽章「正在连接… Ns」秒数一直涨（9s / 17s / 31s…），发消息永远没有回复。
Console：  stream.ts:529 … WebSocket is closed before the connection is established.
后端日志：WebSocket /api/chat/stream [accepted] + connection open —— **服务端一切正常**
```

**定位手法（这一步值得记）**：HTTP 侧全绿、裸 WS 也能 `101` 时，**别再用 curl 猜** ——
用**真实浏览器**跑一次（`--headless=new` + CDP 驱动；本仓没有 Playwright，脚本见 §10.4）。
实测拿到的页面侧时间线（临时给源码插 `window.__csdbg`）把死锁钉死了：

```text
hook effect run  connectionKey=0
openChatStream  gen=1  → push connecting      ← StrictMode 第一次挂载
openChatStream  gen=2  → push connecting      ← 第二次挂载（真正生效的那条）
error  gen=1 current=false                    ← 旧连接被 cleanup 收线（**就是 Console 那句噪音**）
close  gen=1 intentionalClose=true
open   gen=2 current=true   → onOpen()
6s 后探针：gen=2 readyState=1 protocol=shopsage.v1 phase=connecting   ← **open 了，phase 没动**
```

**根因是一条自锁**（三件事互相依赖，缺一都不会发生）：

```text
① useChatStream.send() 判 `phase === "connecting"` → 把消息**排队**（pendingRef），不发送；
② 那条排队只在 `open` 事件里冲一次 —— 而 open 早在用户点发送**之前**就发生了，队列当时是空的；
③ 后端不会主动发帧；而 `phase` 唯一能离开 `connecting` 的路径是**收到一帧**。
```

于是：**没发送 → 没有帧 → 状态不动 → 更不发送**。`frame_timeout` 看门狗（180s）也救不了它 ——
它只在 `phase === "streaming"` 时计时，而这个页面**永远进不到 streaming**。

**修法**：`openChatStream` 的 `open` 监听里，握手成功就把 phase 推出 `connecting`
（`web/src/lib/stream.ts`）：

```ts
push({ ...state, phase: "idle" });   // open 之后「可发」，send 不再把消息排进黑洞
```

**为什么以前没测出来**：`transport.test.ts` 的假 socket **从来不发 `open`**（看门狗默认关掉，
见 `NO_WATCHDOG` 的注释），所以「open 之后会发生什么」这一格一直是空的；而真链路用例跑在 Node 里，
没有 React、也没有「先排队再 open」这个时序。**这就是必须用真浏览器跑一次的判据。**

**防回归（两层，都已加）**：

* 运行期：`transport.test.ts` ★「握手成功后 phase 必须离开 connecting」——真开一条连接、
  发一次 `open` 事件、断言 phase 与推给调用方的状态；
* 源码级：`hook-wiring.test.ts` ★「握手成功的 open 里必须推进 phase」——挡住有人把这一行删掉。

**判据（下次一眼分清是坑 19 还是坑 20）**：

| | 坑 19 | 坑 20 |
|---|---|---|
| 刷新页面（`Ctrl+F5`）后 | **好了** | **照样卡住**（刷新不改变代码里的自锁） |
| 后端日志 | 有 `accepted`，之后可能没帧 | 有 `accepted` + `connection open`，**永远没有帧**（客户端根本没发） |
| 输入框 | 卡住 / 点不动 | **能打字、能点发送，消息也上屏**，就是永远等不到回答 |

**坑 21：「等 LLM 回复时退出登录」→ 之后**再也登不进去**，一直显示「登录中…」（真 bug，已修，issue 007）**

坑 20 是**前端**的自锁；这一条是**后端**的事件循环被一条死连接占满 —— 两者的现象
（「卡住、等不到东西」）很像，但判据完全在**服务端**这一侧。

```text
用户现象：在等 LLM 回复的过程中点「退出登录」；
          之后在 /login 点「登录」→ 按钮永远停在「登录中…」，不进也不报错。
          （换账号、换浏览器、curl 直连都一样）
后端现象：GET /health **永久超时**；后端进程 CPU 打满一个核（实测 5 秒增量 5.38s）
```

**根因**：`WS /api/chat/stream` 的消息循环**只接住了两个断连异常中的一个**。
Starlette 按两种语义抛两个**互不继承**的异常：

| 异常 | 语义 | 触发场合 |
|---|---|---|
| `WebSocketDisconnect`（`Exception`） | 「收到对端的断开消息」 | 客户端发了 close 帧 |
| `WebSocketDisconnected`（`RuntimeError`） | **「这条连接已经不可用」** | 连接状态已离开 CONNECTED 后的任何 `receive_*` / `send_*` |

`WebSocketDisconnected` 滑进了兜底的 `except Exception`（那里对「坏内容」的处理是
**回一条错误帧然后 continue 继续服务**），于是：连接已死 → `receive` 立刻抛（**不 await、
不让出事件循环**）→ `_send_frame` 又被吞掉 → `continue` → **纯 CPU 忙等**，
单线程事件循环被占满，**所有**请求（含登录）永远得不到调度。

⚠️ **为什么偏偏是「等 LLM 的时候」退出才炸**：图不会因客户端断开而停下（刻意的设计）。
刚断开时消息循环正阻塞在 `receive_json` 的**真** I/O 上 → 会正常收到 `websocket.disconnect`
→ 走 `WebSocketDisconnect` 分支、正常退出。只有「**图还在跑**（没有人在收帧）时断开，
等图画完再发帧」才会撞上那个已死的连接。所以「回答已经出来了再退出」**不**触发。

**判据（一眼分清是不是这一条）**：

* 页面**没有任何错误提示**（不是 401、不是 `Failed to fetch`）→ 请求根本没被处理；
* `py-spy dump --pid <后端进程>` 的栈停在 `_send_frame → send_json → starlette.websockets.send`，
  且**局部变量 `seq` 是个天文数字**（实测 `31338568` —— 那个计数器只在一轮问答里递增，
  正常是一位数，涨到千万说明循环空转了几千万次）；
* ⚠️ **`asyncio.wait_for` / 看门狗都压不住它**（实测套 2 秒超时仍然永久挂住）：
  忙等里没有任何让出点，定时器回调永远没机会执行。所以「给外层加超时」不是解药。

**修法**：`app/api/chat.py` 把 `WebSocketDisconnected` 与 `WebSocketDisconnect` **同等对待**
（连接没了就结束消息循环并返回），兜底分支只留给「这一帧内容不合法」。

**防回归**：`tests/api/test_chat_stream_disconnect.py`（4 条；摘掉修复即 2 条红）。
⚠️ 那两条判据的设计本身就是这个 issue 的认知：**不能用超时**（压不住忙等）、
**哨兵异常必须继承 `BaseException`**（否则被被测代码自己的 `except Exception` 吞掉）、
**结构断言必须用 AST 而不是字符串匹配**（注释里出现过那个字符串，会骗过纯文本断言）。

**真浏览器复刻**：`node scripts/logout-during-llm-evidence.mjs`（需前后端都在跑）。
⚠️ 复刻要踩准窗口：`_stream_turn` 是「先跑完图、再一次性补发节点帧」，
所以「正在检索知识库…」这类文案出现时图**已经跑完**了 —— 那时退出踩不到故障，
真正的窗口只有**首帧骨架到 answer 帧之间**。

### 10.2 排障顺序（三条路，别乱）

1. **先确认两个进程各自在跑哪一版、连的是谁**：
   `Invoke-RestMethod http://127.0.0.1:8000/health`（后端在不在、连的哪个库）
   + 页面顶栏那行 `后端 http://127.0.0.1:8000 · 身份 user_id=… · 会话 …`（前端连的是谁）。
   本步 90% 的「假红」都在这一步被排除（改了后端没重启、`NEXT_PUBLIC_API_BASE` 指错、令牌过期）。
2. **再看后端日志里的 tool_error**：`data`/`rag` 的失败在日志里是**结构化一行**
   （带 `tool`、`error_type`、`args`）；前端看到的「没有来源 / 没有图表」几乎都对应这里的一行。
3. **最后才怀疑前端**：此时用 `npm test`（离线）与浏览器 Console 夹逼 ——
   离线绿 + Console 报错 = 环境/契约问题；离线红 = 前端逻辑问题。

### 10.3 坑的分布（为什么这些坑大半不是前端的问题）

| 坑 | 表面症状在前端？ | 根因所在 |
|---|---|---|
| 1 / 2 / 3（工具链） | 是（`npm install`/`build`/`test` 失败） | 本机受限环境（子进程、缓存目录） |
| 4 / 5（检索 / 出图） | **是**（「没有来源」「没有图表」） | **后端**：异步节点调同步检索、few-shot 教坏了模型 |
| 6（语料被清空） | 是 | 测试顺序（全量回归会 `downgrade base`） |
| 7 / 8 / 10（子协议 / 没重启 / 两个 next 抢 `.next`） | 是 | 契约细节 + **操作顺序** |
| **11 / 12（跨源 / 进程没起）** | **是**（「连不上后端」「令牌全无效」） | **后端缺 CORS 中间件** + 两个进程都得人来起 |
| **13（`typ` 被读成 `type`）** | **是**（「这是刷新令牌」—— 而令牌完全正确） | **前端读错契约键名**（真令牌为准的验证能一次抓到，见 §7.6） |
| **14（base 指到前端自己）** | **是**（「HTTP 通、WebSocket 死」） | **HTTP 有两个出口而 WS 只有一个** + `NEXT_PUBLIC_API_BASE` 是构建期变量 |
| **18（照网卡地址打开页面）** | **是**（「一直无法连接」+「发消息没反应」——而同一条 WS 明明连上了） | `API_CORS_ORIGINS` 白名单里没有那个来源（**且设了它就替代默认值**）+ 网卡 IP 会变 |
| **15（被抛弃的旧连接报错）** | **是**（「聊得好好的忽然报连接失败」——URL 还是对的） | **React 双挂载 / 热更新**产生的僵尸连接 + `CONNECTING` 阶段 close 会得到 `error` |
| **20（握手成功但 phase 卡在 connecting）** | **是**（「秒数一直涨、发不出去」，而**后端日志一切正常**） | **前端自己的状态机**：`send` 判 `connecting` 就排队，而 phase 只能靠「收到帧」离开 `connecting` → 自锁（真 bug，已修 + 两层防回归） |
| 9（把前端当安全边界） | 不是（它是设计纪律） | 分层 |

这张表的用途只有一个：**下次看到「前端坏了」，先按 10.2 的三步确认根因在哪一层** ——
本步大半的「前端 bug」最后都改在后端、契约理解或操作顺序上。
其中四条最值得记住，因为它们的**症状都极像「后端/令牌坏了」，而真相都不是**：

* **坑 11**：`fetch` 抛 `TypeError`（**没有任何状态码**）= 响应没被读到 → 进程没起 / **跨源被拦** / 地址错；
  **不是令牌问题**（令牌无效会得到 401）；
* **坑 13**：报错说得**非常具体**（「这是刷新令牌」）时也要留个心眼 ——
  先看一眼页面上那排徽章里的 `typ=` 与「剩余 N 分钟」，再决定要不要重签；
* **坑 14**：**HTTP 通不代表地址对** —— `/api/kb` 是 Next 自己的路由，它永远通。
  判据是自检的 ⑤（WebSocket 能否 upgrade）：`ws:` 连不上而 HTTP 正常 ⇒ 先怀疑**地址**；
* **坑 15**：**能用不代表没有错** —— 报错可能来自一条已经没人要的连接。
  判据是后端日志里「同一个客户端端口出现两条 `accepted`」；
  修法是**世代号**（旧连接不许改状态），而不是去查后端。

### 10.4 用真浏览器复现（本仓没有 Playwright，靠 CDP）

离线用例覆盖不到「React + 真实握手时序」这一类问题（坑 20 就是这么漏掉的），
所以这类故障要**用真浏览器**跑一次。无头 Chrome + Node 自带 WebSocket 就够，不需要装任何依赖：

```powershell
# ① 起一个客户令牌并写进页面登录态 → ② 驱动 Chrome 打开对客页、点一条建议问题、回读徽章与帧
.\.venv\Scripts\python.exe -m scripts.dev_token --role customer --user-id 2 --ttl 7200 --json
node .appdata\cdp-repro.mjs
```

脚本做的事（`.appdata/cdp-repro.mjs`，走 `DevToolsActivePort` + CDP 协议）：
写 `localStorage` 的 `shopsage.token.customer` → 打开 `/` → 回读徽章与消息气泡 →
点一条建议问题 → 打印 WebSocket 生命周期与浏览器 console。

⚠️ 两条环境限制（都是本机沙箱的边界，**不是代码问题**）：

* Chrome 需要创建 mojo 命名管道，受限沙箱下会
  `FATAL:mojo\public\cpp\platform\platform_channel.cc: Check failed: 拒绝访问` —— 要在放宽的模式下跑；
* `next build` 在受限沙箱下会 `spawn EPERM`，此时以 `npm test` + 真浏览器实测为准。

---

## 11. 验收与证据回填

```powershell
# ① 离线用例（本模块，零依赖）
cd web; npm test
# 实测：tests 91 / pass 91 / fail 0（0.21s）

# ② 类型检查 + 构建（验收「构建通过」）
npm run typecheck      # 实测：无输出
npm run build          # 实测：✓ Compiled successfully in 22.0s；9/9 静态页

# ③ **先自检「两个进程 + 跨源 + WebSocket upgrade」**（浏览器能不能用的前提）
cd ..
uv run python -m scripts.check_web_cors
# 实测：五项全 ✅、退出码 0
# ⚠️ 如果你是照 `next dev` 打印的 Network 地址打开的页面，再跑一次带 --web 的那条：
uv run python -m scripts.check_web_cors --web http://192.168.1.13:3000
# 实测：③ 会明确说「这个来源不在白名单里」并给出要写进 .env 的那一行

# ④ 真链路（需要后端 + 语料；前端没起时启动器会自己起）
cd web; node tests/run-live.mjs
# 实测：tests 10 / pass 10 / fail 0（41.9s 热身态 / 81.9s 冷启动态）
# ⚠️ 2026-10-17 起「真溯源」那条**连问两次**且断言 `flow_state=delivered`
#    （原来只断言 citations，缓存命中时会全绿而用户拿到「无法处理」，见 issue 009）。

# ⑤ 四条路由可访问（前端在跑时）
foreach ($p in '/', '/internal', '/login', '/citation?q=%E9%80%80%E8%B4%A7%E6%94%BF%E7%AD%96.md') {
  $r = Invoke-WebRequest "http://127.0.0.1:3000$p" -UseBasicParsing
  "$p -> $($r.StatusCode)"
}
# 实测：四条都是 200

# ⑥ 跨源的服务端用例（改了 CORS 相关代码才需要，通常随全量回归一起跑）
cd ..; uv run pytest -q tests/api/test_cors.py
# 实测：11 passed

# ⑦ 语料与依赖栈状态（回填时一起记）
uv run python -m scripts.probe_rag_sources     # 实测：61 活跃片段 / 8 份文档
uv run python -m scripts.db_check              # 实测：users 8 / orders 30 / products 20 / order_items 65

# ⑧ Python 全量回归（改了后端四处，必须跑）
uv run pytest -q -m "not llm_live"
# 实测：1628 passed, 50 skipped, 13 deselected, 6 errors（150s）
```

回填到 [`docs/modules/16.web.md`](../modules/16.web.md) §8 的四条验收标准 + 一张证据表（§12 有模板）。

---

## 12. 六组自查命令（收尾必跑）

```powershell
# ── 执行位置：web/ ──
$env:npm_config_cache = "..\.npm-cache"

# ⓪ 「两个进程 + 跨源」一条命令（**本步最该先跑的一条**）
cd ..; uv run python -m scripts.check_web_cors; cd web
# 实测：四项全 ✅、退出码 0（含「后端放通前端来源」与「预检答得掉」）

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
# 实测：tests 102 / pass 102 / fail 0

# ⑤ 跨源这一层：前后端的「来源」必须对得上（坑 11 的判据）
#    ⚠️ 别只看「后端配了没有」：要确认**前端实际打开的地址**在后端白名单里
Select-String -Path '..\app\api\config.py' -Pattern 'DEV_CORS_ORIGINS' -Context 0,4 |
  ForEach-Object { $_.Context.PostContext }
# 实测：
#     "http://localhost:3000",
#     "http://127.0.0.1:3000",
#   两个都在（它们**不是**同一个来源；只配一个会「换个地址打开就坏」）

# ⑥ 令牌类型判定的**契约键名**（坑 13 的判据）：后端写 `typ`，前端必须两个都认
Select-String -Path '..\app\security\auth.py' -Pattern '"typ": token_type' | ForEach-Object { "后端：$($_.Line.Trim())" }
Select-String -Path 'src\lib\auth.ts' -Pattern 'payload.typ \?\? payload.type' | ForEach-Object { "前端：$($_.Line.Trim())" }
# 实测：
#   后端："typ": token_type,
#   前端：const typeValue = payload.typ ?? payload.type;

# ⑦ 连接层的三条兜底**都接线了吗**（坑 17 的判据）：源码级断言比人眼可靠
#    期望：⑧ 条全绿（依赖里有 connectionKey / onOpen 会 flush 排队 / running 不含 connecting …）
node --test --test-isolation=none tests/hook-wiring.test.ts 2>&1 | Select-String 'pass |fail '
# 实测：pass 7 / fail 0
```

> 🎯 ⑦ 是这份自查里的**新面孔**，也是坑 17 唯一能被自动抓到的地方：
> 它读源码，不跑代码 —— 因为「依赖数组漏项」这类 bug **没有任何运行期表现**（§7.7 的对照表）。

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
