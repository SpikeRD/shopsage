# 模块 02 · infra（容器化部署）

## 目的

两套 Compose：**开发栈只起依赖**（业务代码跑在宿主机的 `uv` 环境里）与**生产栈全栈容器化**。目标是"Linux 上衣一键起依赖、服务器上一键起全套"。

## 关键结论

1. **开发栈**：`infra/docker-compose.dev.yml`，5 个服务 —— `postgres`(pgvector/pg16)、`redis`、`prometheus`、`otel-collector`、`grafana`。项目名钉死 `shopsage-dev`，卷名因此稳定。
2. **生产栈**：`docker-compose.yml`，9 个服务 —— `migrate`（一次性）、`api`、`worker`、`web`、`postgres`、`redis`、`prometheus`、`grafana`、`otel-collector`。
   - `migrate` / `api` / `worker` **共用同一个后端镜像**（`x-app-base` 锚点，`docker-compose.yml:22-43`），换命令不换镜像 —— 保证"入队用的任务名"与"消费用的实现"是同一份代码。
   - `migrate` 是 `restart: "no"` 的一次性服务，`api`/`worker` 用 `depends_on: migrate: condition: service_completed_successfully` 卡住启动（`docker-compose.yml:49-68`）。**迁移失败即阻断发布**。
3. **环境变量两条通道，别混**：
   - `.env`（仓库根）→ 只管 compose 自己的 `${...}` 插值（`IMAGE_TAG`、`*_PORT`）；
   - `env.list` → 经 `env_file:` 注入进容器（`ENV`/`DATABASE_URL`/`LLM_API_KEY`/`POSTGRES_*`…）。
   - 混用的症状：**容器起得来，但业务配置全是默认值**。
4. **`environment:` 覆盖 `env_file:`**（compose 优先级），所以 `docker-compose.yml:36-39` 只强制三件事：`ENV=prod`、`TZ`、`HF_HOME`。
5. **`NEXT_PUBLIC_API_BASE` 是构建期变量**（`docker-compose.yml:117`）：改它必须 `up -d --build web`，重启容器无效。
6. **生产默认不装 torch**：`WITH_LOCAL_EMBEDDING` 默认 `false`，配合 `EMBEDDING_BACKEND=litellm` + 阿里云 `dashscope/qwen3.7-text-embedding-flash`（`env.list.example:116-118`）。两者必须一起改（`docs/runbook/17.docker-ops.md` §6.3）。
7. **`/health` 恒 200**，所以容器 healthcheck 用状态码判"进程活着"，依赖故障不触发容器重启（`app/api/health.py:9-18`、`docker-compose.yml:71-78`）。
8. **CI/CD 未落地**：仓库无 `.github/`（见 `../../02-异常清单.md` E-05）。
9. **部署验证状态存在两种说法**（见 `../../02-异常清单.md` E-06）：`docs/modules/2.infra.md:9-10` 说"未在真实 Linux 服务器执行过"；`git log` 最新提交是"在windows本机完成docker部署并启动"。**当前能确认的是 Windows+WSL2 形态跑通过，独立 Linux 服务器形态无证据**。

## 实际用法

```bash
# 开发：只起依赖（必须在仓库根，两个参数缺一不可）
docker compose -f infra/docker-compose.dev.yml --project-directory . up -d

# 生产：迁移 → 依赖 healthy → 业务+观测
docker compose -f docker-compose.yml --project-directory . up -d --build
docker compose -f docker-compose.yml --project-directory . ps
```

```yaml
# docker-compose.yml:22-43（节选）—— 一个镜像三个角色
x-app-base: &app-base
  image: shopsage-api:${IMAGE_TAG:-latest}
  build: { context: ., dockerfile: app/Dockerfile, args: { WITH_LOCAL_EMBEDDING: "${WITH_LOCAL_EMBEDDING:-false}" } }
  env_file: [env.list]
  environment: { ENV: "prod", TZ: "${TZ:-Asia/Shanghai}", HF_HOME: "/app/.hf-cache" }
  volumes: [hf_cache:/app/.hf-cache]
```

| 改了什么 | 生效方式 | 命令 |
|---|---|---|
| `env.list` | 容器重建 | `up -d --force-recreate api worker` |
| `app/**` `.py` | 镜像重建 | `up -d --build api worker` |
| `web/**`、`NEXT_PUBLIC_API_BASE` | 镜像重建 | `up -d --build web` |
| `pyproject.toml` / `uv.lock` | 镜像依赖层变 | `up -d --build` |
| `infra/prometheus/**` | 只在启动时读 | `up -d --force-recreate prometheus` |
| 任何 `docker-compose.yml` 字段 | compose 重新解析 | `up -d`（**不是** `restart`） |

（表来源：`docs/runbook/17.docker-ops.md:36-50`）

## 待办 / 已知边界

- ⚠️ 独立 Linux 服务器形态**未验证**（只在本机 WSL2 验证过）。
- `.dockerignore` 是否把 `.env` / `env.list` 排除了，本次未逐行核对；`docs/runbook/17.docker-ops.md` §8.3 给了自查命令 —— 属**待执行**项，进 `../../03-待确认清单.md` Q-08。
- CI 流水线（lint → test → build → deploy）不存在。

## 来源

- `docs/modules/2.infra.md`（168 行，2026-10-17 12:03）
- `docs/runbook/2.infra-runbook.md`（316 行）、`docs/runbook/17.docker-ops.md`（761 行，2026-10-18 02:26）、`docs/runbook/18.docker-install-windows.md`（328 行）
- 代码：`docker-compose.yml`、`infra/docker-compose.dev.yml`、`app/Dockerfile`、`web/Dockerfile`、`env.list.example`
