# syntax=docker/dockerfile:1
#
# ShopSage Web 镜像（Next.js 15，standalone 输出）
#
# ⚠️ **构建上下文必须是仓库根**（不是 web/）：
#   `web/src/app/api/kb/route.ts`（出处定位）要读 `docs/kb/` 下的语料，
#   而它在运行期按 `path.resolve(process.cwd(), "..", "docs", "kb")` 找文件
#   —— 即「workdir 的上一级 /docs/kb」。上下文设成 `web/` 就复制不进来，
#   症状是「页面其它都正常，只有『查看原文』永远 404 / 空列表」。
#
# 构建（仓库根）：
#   docker build -f web/Dockerfile \
#     --build-arg NEXT_PUBLIC_API_BASE=http://<服务器IP>:8000 \
#     -t shopsage-web:latest .
#
# ⚠️⚠️ `NEXT_PUBLIC_API_BASE` 是**构建期**变量，不是运行期：
#   Next 在 `next build` 时把它**字面替换**进客户端 bundle。
#   所以「改 env.list 里这一项 → 重启容器」是**无效的**，必须重新 build。
#   这是容器化 Next 最容易踩的一条，见 docs/runbook/17.docker-ops.md §5.2。

ARG NODE_VERSION=22

# ── 阶段 1：依赖层（只由 package.json / package-lock.json 决定）──────────────
FROM node:${NODE_VERSION}-alpine AS deps
WORKDIR /app

# 用 npm ci 而不是 npm install：它严格按 package-lock.json 装，
# 装出来的版本与开发机一致（与后端 `uv sync --locked` 同一条纪律）。
COPY web/package.json web/package-lock.json ./
RUN npm ci

# ── 阶段 2：构建 ────────────────────────────────────────────────────────────
FROM node:${NODE_VERSION}-alpine AS builder
WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY web/ ./

# ⚠️ 必须在本阶段声明并导出：`next build` 期间要读到它（构建期替换）
ARG NEXT_PUBLIC_API_BASE=""
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE} \
    NEXT_TELEMETRY_DISABLED=1

# `next.config.ts` 里已配置 `output: "standalone"`：
# 产物 `.next/standalone` 只带运行真正用到的 node_modules（镜像小一个数量级）。
# ⚠️ 没有那个配置时 `.next/standalone` **不会生成**，下面的 COPY 会直接失败。
RUN npm run build

# ── 阶段 3：运行镜像 ────────────────────────────────────────────────────────
FROM node:${NODE_VERSION}-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    TZ=Asia/Shanghai

# tzdata：与后端同一条理由（容器内 ZoneInfo 需要数据）
RUN apk add --no-cache tzdata

# standalone 的目录结构：server.js 在包根，`node_modules` 已裁剪
COPY --from=builder --chown=node:node /app/.next/standalone ./
# 静态资源与 public **不会被 standalone 自动带上**，必须手工复制（官方文档明确写了）
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public

# ⚠️ 语料目录：`/api/kb` 路由按 `cwd/../docs/kb` 找，workdir=/app → `/docs/kb`
#    必须放在**根**下而不是 /app 里，否则「查看原文」永远读不到文件。
#    这里直接从构建上下文复制（builder 阶段没有 docs/，它的上下文是 web/ 的内容 + 根 docs）。
COPY --chown=node:node docs/kb /docs/kb

USER node

EXPOSE 3000

CMD ["node", "server.js"]
