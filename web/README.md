# ShopSage Web（第 16 步：演示层）

Next.js（App Router）+ TypeScript 前端，提供**两个入口**：

| 路由 | 入口 | 出口 | 关键能力 |
|---|---|---|---|
| `/` | 对客 | `WS /api/chat/stream` | 流式骨架 + 节点进度 + **溯源展示** + 人工审批（批准/拒绝） |
| `/internal` | 对内 | `POST /api/chat` | 报表表格 + **图表**（ECharts）+ SQL 回显 |
| `/login` | 登录 / 切角色 | —— | 粘贴演示令牌（本仓没有登录接口） |
| `/citation` | 出处定位 | `GET /api/kb`（本服务自己的路由） | 出处串 → 原文（白名单 + 限大小） |

> 📖 **先读操作手册**：[`docs/runbook/16-web-runbook.md`](../docs/runbook/16-web-runbook.md)
> （起服务、签令牌、手工验证、七个坑与分诊、验收证据都在那里）。
> 设计原理见 [`docs/modules/16.web.md`](../docs/modules/16.web.md)。

## 快速开始

```bash
# ① 后端（另一个终端，仓库根）
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

# ② 前端依赖（⚠️ 必须 --ignore-scripts，理由见 runbook §10.1 坑 1）
npm install --ignore-scripts

# ③ 起前端
npm run dev          # http://127.0.0.1:3000

# ④ 签一个能粘进登录页的令牌（仓库根执行）
uv run python -m scripts.dev_token --role internal_employee

# ⑤ 页面报 Failed to fetch 时先跑这个（**别去重签令牌**）
uv run python -m scripts.check_web_cors
```

> ⚠️ **前端与 API 不同源**（3000 vs 8000），所以后端必须放通前端来源
> （`API_CORS_ORIGINS`，开发期默认已放通 `localhost:3000` 与 `127.0.0.1:3000`）。
> 缺它的症状是页面上**一切**请求都 `TypeError: Failed to fetch` —— **没有任何状态码** ——
> 而服务端日志里全是 `200 OK`。判据：**「令牌无效」会得到 401（能读到的响应），
> 不是这种症状**。详见 runbook §10.1 坑 11。
>
> ⚠️ **连接有看门狗**（runbook §10.1 坑 16）：握手 8 秒没应答、或一轮 180 秒没有任何帧，
> 都会被判成失败并给出「重连」按钮 —— 因为「一直显示正在连接」是比报错更糟的状态
> （用户既得不到回答，也看不到原因）。两个值可用 `handshakeTimeoutMs` / `frameTimeoutMs` 注入覆盖。

## 命令

| 命令 | 作用 | 是否需要后端 |
|---|---|---|
| `npm run dev` | 开发模式（改前端即生效） | 页面能开，功能需要 |
| `npm run build` | 构建（验收「构建通过」的判据） | 不需要 |
| `npm start` | 起构建产物 | 功能需要 |
| `npm run typecheck` | 类型检查（`tsc --noEmit`） | 不需要 |
| `npm run lint` | ESLint | 不需要 |
| `npm test` | **离线**用例 95 条（帧时序 / 出处 / 图表 / 角色 / 令牌 / 传输 / 诊断 / 配置） | 不需要 |
| `node tests/run-live.mjs` | **真链路**用例 10 条（会自己签令牌；前端没起时它起） | 需要 |

> ⚠️ `npm test` 只收 `tests/*.test.ts`，真链路那组在 `tests/live/`（**故意分开**：
> 后者需要后端与语料，混在一起会让「没起后端」看起来像「代码坏了」）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEXT_PUBLIC_API_BASE` | `http://127.0.0.1:8000` | 后端地址（`http:`/`https:` 会自动推导 `ws:`/`wss:`）。⚠️ **构建期变量**，改了要重启 dev / 重新 build。⚠️ 若它被设成**页面自己的地址**，`resolvedApiBase()` 会自动改回默认后端并在页面上说明 —— 那种配置会让 HTTP 半通（`/api/kb` 是 Next 自己的路由）而 **WebSocket 必死**（Next 不代理 upgrade） |

真链路用例另外认这几个（由 `tests/run-live.mjs` 自动注入，一般不用手设）：
`SHOPSAGE_API_BASE`、`SHOPSAGE_TOKEN_CUSTOMER`、`SHOPSAGE_TOKEN_INTERNAL`、
`SHOPSAGE_TOKEN_EXPIRED`、`SHOPSAGE_WEB_BASE`、`SHOPSAGE_SKIP_WEB`、`SHOPSAGE_KEEP_WEB`。

## 三条纪律（改这个目录之前先读）

1. **前端不做业务判断**：`web/` 里不出现「这条数据能不能看」「这句话能不能答」的判断 ——
   只做「分流入口 → 调接口 → 渲染帧 → 展示出处」。角色表只是**界面边界**，真正的拦截在后端。
2. **契约只有一份且来自后端**：`src/lib/contracts.ts` 是 `app/api/schemas.py` 的镜像，
   所有网络数据先过 `parseFrame()` / `parseChatResponse()`（别用 `as Xxx` 糊过去）。
   ⚠️ **令牌形状同样以后端为准**：类型声明的键名是 `typ`（不是 `type`）——
   第一版读 `type`，于是所有真令牌都被判成「刷新令牌」而拒收（runbook §10.1 坑 13）。
   手写的 fixture 只能证明「实现与我对契约的理解一致」，所以形状类断言要有一条拿**真令牌/真响应**验。
3. **页面文件不许有额外导出**：`src/app/**/page.tsx` 里除 `default` 之外的导出会让
   `next build` 直接失败（可复用的东西放 `src/components/` 或 `src/lib/`）。
