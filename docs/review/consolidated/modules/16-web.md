# 模块 16 · web（Next.js 前端）

## 目的

三个入口（对客 `/`、对内 `/internal`、管理台 `/admin`）+ 登录页，把后端的流式问答、订单/退款、管理台能力做成可演示的界面。

## 关键结论

1. **技术栈**：Next **15.5.27**（App Router，`output: "standalone"`）、React **19.1.0**、Tailwind **4**、ECharts **5.5.1**、TypeScript 5；测试用 **Node 内置 `node --test`**（不是 jest/vitest）—— `web/package.json`。
2. **路由表**（`web/src/app/`）：

   | 路径 | 文件 | 说明 |
   |---|---|---|
   | `/login` | `(auth)/login/page.tsx` | 账号 + 口令 |
   | `/` | `(app)/page.tsx` | 对客问答（流式 + 溯源 + 图表） |
   | `/internal` | `(app)/internal/page.tsx` | 对内查询 + 报表 |
   | `/internal/refunds` | `(app)/internal/refunds/page.tsx` | 员工退款审批台（变更 003） |
   | `/orders` | `(app)/orders/page.tsx` | 客户订单页 + 申请退款（变更 003） |
   | `/admin` | `(app)/admin/page.tsx` | 管理台 |
   | `/citation` | `(app)/citation/page.tsx` | 出处原文查看 |
   | `/api/kb` | `app/api/kb/route.ts` | Next 侧只读代理：`GET /api/kb?name=退货政策.md` |

3. **鉴权是"前端只渲染、后端才判"**：`web/src/lib/auth.ts:61 ROLE_PERMISSIONS` 是**后端表的副本**，只决定菜单显隐；任何真正的授权判断都在后端（同文件 :57-59 注释）。
   ⚠️ 入口判断有两个函数：`canUseInternalEntry`（能不能进对内页，= 有 `read:all_orders` **或** `approve:refund`）与 `canUseInternal`（能不能查全站，= 有 `read:all_orders`）。**不许混用**（`web/src/lib/auth.ts:107-128`）。
4. **流式走 WebSocket 直连后端**，不经 Next 路由层（Next 的 `rewrites` 不代理 WS 升级）—— `web/next.config.ts` 注释第 2 条。因此 `NEXT_PUBLIC_API_BASE` 必须是**构建期**变量。
5. **帧与关闭码契约**：5 类帧 `skeleton/node/answer/done/error`；关闭码 `4401/4429/4400`（`web/src/lib/contracts.ts:22-35`）。子协议必须是 `shopsage.v1` + `bearer.<token>` 两个（浏览器不能给 WS 自定义头）。
6. **token 刷新**：`ensureFreshToken()` / `withFreshToken()`（`web/src/lib/session.ts:231/332`），提前 60 秒续期（`REFRESH_LEEWAY_SECONDS=60`）。
7. **口令策略前端也要有**（`issues/004` 教训）：`web/src/lib/password.ts:33 MIN_PASSWORD_LENGTH=12`，与后端 `app/security/accounts.py:95` 同值。**只放后端会导致"前端不发请求也不说原因"**。

## 实际用法

```bash
cd web
npm install --ignore-scripts     # ⚠️ 必须带 --ignore-scripts
npm run dev                      # http://127.0.0.1:3000
npm test                         # 离线用例（不连后端）
npm run typecheck
npm run build
node tests/run-live.mjs          # 真链路（需后端在跑）
```

```ts
// web/src/lib/stream.ts:356 —— 打开流
const handle = openChatStream({ token, message, sessionId, onFrame, onClose });
```

## 待办 / 已知边界

- **启动方式已核实**：生产容器用 `node server.js`（`web/Dockerfile:59 CMD ["node","server.js"]`），依赖 `next.config.ts` 的 `output: "standalone"` 产出 `.next/standalone`；`web/package.json` 的 `npm start`（`next start`）**不是**容器用的那条命令。开发期仍用 `npm run dev`。
- `issues/004/005/006/007` 全部在前端侧有对应修复与取证脚本（`web/scripts/*-evidence.mjs`），但这些脚本会**真改演示库**，只能对开发库跑。
- 页面打开地址必须是 `http://127.0.0.1:3000` 或 `http://localhost:3000`；`next dev` 打印的 `Network: <网卡IP>:3000` **不在跨源白名单**（`README.md:166-172`）。

## 来源

- `docs/modules/16.web.md`（170 行）、`docs/runbook/16-web-runbook.md`（1458 行）
- `docs/changes/002`、`issues/004/005/006/007`
- 代码：`web/src/**`、`web/package.json`、`web/next.config.ts`；测试：`web/tests/*.test.ts`（14 个）
