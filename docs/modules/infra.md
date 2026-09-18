# infra — 容器化部署与 CI/CD（Windows 开发 → Linux 生产）

## 1. 职责

用 Docker Compose 编排所有依赖服务与业务镜像，用 GitHub Actions 打通「lint → test → 构建镜像 → 部署 Linux」。
目标：**本机能一键起依赖开发，服务器能一键起全套生产**。

> ⚠️ 本模块横跨两个阶段：**P0 只做开发依赖（`docker-compose.dev.yml`，本机起数据库/Redis）**；
> **生产 compose + 镜像 + CI/CD 留到 P6**。见下方「开发步骤」。

## 2. 对外接口

```bash
# 开发期：只起依赖（PostgreSQL+pgvector、Redis），代码跑在本机 venv
docker compose -f docker-compose.dev.yml up -d

# 生产期：全套服务
docker compose up -d
```

## 3. 依赖

- Docker + Docker Compose（Linux 服务器上已具备）
- GitHub Actions（Linux runner）
- 读取 `.env`（生产密钥走环境变量注入，见 `config.md`）

## 4. 关键设计决策

1. **开发/生产一份 Compose 拆分**：`docker-compose.dev.yml` 只含基础设施（postgres/redis）；`docker-compose.yml` 含全部服务（api/worker/web）。用 `.env` 区分（架构第 9 节）。
2. **开发期代码不容器化**：本机 venv 直接跑，热重载快、好调试；只有「依赖服务」进容器。
3. **镜像在 Linux runner 构建**：`docker/build-push-action` 构建 → push registry → 服务器 `pull` 后 compose 起（架构第 9 节「交付」）。
4. **密钥不入镜像**：用 `env_file` / 运行时 `environment` 注入，绝不用 `ARG` 烧进镜像层。

## 5. 核心技术点（要懂为什么）

- **多阶段构建（multi-stage）**：Python 镜像用 `python:3.11-slim` 装依赖 → 复制代码，最后只留运行所需层，镜像体积小、攻击面小。
- **`.dockerignore`**：排除 `.env` / `.git` / `__pycache__` / `venv`，避免把密钥和垃圾打进构建上下文。
- **healthcheck**：给 postgres/redis/api 配 `healthcheck`，compose 用 `depends_on: condition: service_healthy` 保证启动顺序，避免 API 先于数据库起来连不上。
- **为什么 worker 单独一个服务**：Arq 异步任务是常驻进程，和 API 独立扩缩、独立重启（架构第 8 节「按层独立扩缩」）。

## 6. 开发步骤

> **本模块分两阶段，不要一次做完**：P0 只做「本机起开发依赖」（第 1 步），
> 生产部署与 CI/CD（第 2–5 步）留到 P6 全链路跑通后再做。

**P0（开发依赖，先做）**

1. 写 `docker-compose.dev.yml`：`postgres`（含 `pgvector` 镜像或启动脚本装扩展）+ `redis`，配 volume 持久化 + healthcheck。
2. 验证：本机 `docker compose -f docker-compose.dev.yml up` → 数据库/Redis 能连（`db.md` 建表依赖它）。

**P6（生产部署与 CI/CD，最后做）**

3. 写生产 `docker-compose.yml`：`api`、`worker`、`web`、`postgres`、`redis`、`grafana`、`prometheus`。
4. 写 `app/Dockerfile`（多阶段）+ `web/Dockerfile` + `.dockerignore`。
5. 写 `.github/workflows/ci.yml`：ruff/black/mypy → pytest → build & push。
6. 写部署 job：SSH 到 Linux 服务器 `pull` + `docker compose up -d`。

## 7. 测试用例

```bash
# CI 关卡（任何一条不绿则阻断合并）
ruff check . && black --check . && mypy app
pytest
docker compose -f docker-compose.dev.yml config -q   # yaml 合法
```

- 手工验收：删除容器卷后 `up` 能从头重建；`depends_on` 顺序正确（api 在 postgres healthy 后才起）。
- 安全验收：`docker history <镜像>` 里看不到 API Key。

## 8. 验收标准

- [ ] 本机一条命令起全部依赖，服务器一条命令起全套生产。
- [ ] CI 三关（lint / test / build）全绿且阻断不合规合并。
- [ ] 镜像内无密钥；`.env` 不入 Git、不入镜像。
- [ ] 证据链接：`PR#__` / `CI 运行记录#__`
