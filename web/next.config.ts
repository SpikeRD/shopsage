import type { NextConfig } from "next";

/**
 * Next 的构建期配置。
 *
 * 两个刻意的决定（第 16 步 runbook §3.1 有取舍说明）：
 *
 * 1. **后端地址来自环境变量**（`NEXT_PUBLIC_API_BASE`）：地址是**部署参数**，
 *    与「限流额度」同类（`app/api/config.py` 的注释）。写死 `127.0.0.1:8000` 的后果是
 *    「换一台机器演示就白屏」——而排查方向会先去怀疑后端。
 *    ⚠️ 必须 `NEXT_PUBLIC_` 前缀：这个值要在**浏览器**里用（WebSocket 直连后端，
 *    Next 的路由层不参与 WS 升级），只有带前缀的变量才会被打进客户端包。
 *
 * 2. **不做 Next 侧的重写代理**：代理看着更「干净」，但它会让 `WS /api/chat/stream`
 *    走不通（App Router 的 `rewrites` 不代理 WebSocket 升级），而流式问答正是本步的主角。
 *    两条路径（HTTP/WS）用同一个来源，行为才一致。
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  // `next build` 时这些包只在 Node 侧用，避免被打进客户端包
  serverExternalPackages: [],
};

export default nextConfig;
