# Next.js + React + Tailwind + ECharts（前端）

## 本项目用它解决什么

三个入口（对客 / 对内 / 管理台）+ 登录页，把后端的流式问答、订单退款、管理台做成可演示界面；ESLint 与 `node --test` 都不起额外进程。

## 实际用法（代码片段 + 文件:行号）

### 1）standalone 产物（容器只带运行时依赖）

```ts
// web/next.config.ts
const nextConfig: NextConfig = {
  reactStrictMode: true,
  serverExternalPackages: [],
  output: "standalone",          // 产出 .next/standalone，镜像体积 ~600MB → ~150MB
};
```

```dockerfile
# web/Dockerfile:48-59
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
```

### 2）后端地址是**构建期**变量

```ts
// web/src/lib/config.ts:20
export function apiBase(raw: string | undefined = process.env.NEXT_PUBLIC_API_BASE): string { ... }

// web/src/lib/config.ts:71
export function streamUrl(path = "/api/chat/stream", base: string = resolvedApiBase()): string { ... }
```

### 3）WS 握手：子协议 + 令牌（浏览器不能给 WS 自定义头）

```ts
// web/src/lib/contracts.ts:27-35
export const WS_SUBPROTOCOL = "shopsage.v1";
export const WS_TOKEN_PREFIX = "bearer.";
export const WS_CLOSE_UNAUTHORIZED = 4401;
export const WS_CLOSE_RATE_LIMITED = 4429;
export const WS_CLOSE_BAD_REQUEST = 4400;
```

```ts
// web/src/lib/stream.ts:356
export function openChatStream(options: OpenStreamOptions): ChatStreamHandle { ... }
```

### 4）帧契约与状态机

```ts
// web/src/lib/contracts.ts:22-24
export type FlowState = "delivered" | "blocked" | "approval_required";
export type FrameEvent = "skeleton" | "node" | "answer" | "done" | "error";

// web/src/lib/stream.ts:143
export function reduceStream(state: StreamState, frame: Frame, now: number = Date.now()): StreamState { ... }
```

### 5）令牌续期

```ts
// web/src/lib/session.ts:53 / :231 / :332
export const REFRESH_LEEWAY_SECONDS = 60;
export async function ensureFreshToken(...): ...
export async function withFreshToken<T>(...): ...
```

### 6）入口分流（两个函数不许混用）

```ts
// web/src/lib/auth.ts:107-128
export function canUseInternal(role: string): boolean {        // 能不能查全站
  return hasPermission(role, PERM_READ_ALL_ORDERS);
}
export function canUseInternalEntry(role: string): boolean {   // 能不能进对内页
  return canUseInternal(role) || hasPermission(role, PERM_APPROVE_REFUND);
}
```

### 7）图表：后端给 spec，前端转 option

```ts
// web/src/lib/chart.ts:100
export function toEChartsOption(spec: ChartSpec): EChartsOption { ... }
```

## 最小原理

1. **App Router 用"目录即路由"**：`app/(app)/internal/refunds/page.tsx` → `/internal/refunds`；括号目录是**路由组**（分组不影响 URL，可给不同 layout）。
2. **`NEXT_PUBLIC_*` 在构建时被字面替换进客户端 bundle**：所以改它必须重新 build，运行期改环境变量无效。
3. **浏览器不能给 WebSocket 设自定义请求头**：所以令牌只能走"子协议"（`Sec-WebSocket-Protocol: shopsage.v1, bearer.<token>`）。
4. **`standalone` 是把"运行真正用到的 node_modules"裁剪出来**：所以镜像里 `node server.js` 就能起，不需要完整 `node_modules`。
5. **Next 的 `rewrites` 不代理 WebSocket 升级**：所以 WS 必须直连后端。

## 常见坑

| 坑 | 症状 | 依据 |
|---|---|---|
| 用 `Network: <网卡IP>:3000` 打开 | 页面能开、WS 一直连不上（像后端坏了） | `README.md:166-172` |
| 改了 `NEXT_PUBLIC_API_BASE` 只重启 | 无效（构建期变量） | `docker-compose.yml:117` |
| `npm install` 不带 `--ignore-scripts` | 依赖的安装脚本会跑（本机踩过） | `docs/runbook/16-web-runbook.md` §10.1 坑 1 |
| 用 `canUseInternalEntry` 决定查询框可用性 | 客服看到可用查询框，但每次都被后端拒 | `web/src/lib/auth.ts:122-124` |
| 只看 `citations` 非空就认为"答出来了" | 缓存命中被护栏拦下时 citations 仍非空 | `issues/009` |
| 以为 `npm test` 是 jest | 是 Node 内置 `node --test` | `web/package.json` 的 `test` 脚本 |

## 深入关键词

`App Router` · 路由组 `(group)` · `layout.tsx` · Server / Client Component · `next.config.ts` `output: "standalone"` · `NEXT_PUBLIC_` 构建期内联 · React 19 `useTransition` · Tailwind 4 `@tailwindcss/postcss` · ECharts `setOption` / `notMerge` · `node --test`
