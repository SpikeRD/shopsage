# infra — 容器化部署与 CI/CD（Windows 开发 → Linux 生产）

## 1. 职责

用 Docker Compose 编排所有依赖服务与业务镜像，用 GitHub Actions 打通「lint → test → 构建镜像 → 部署 Linux」。
目标：**Linux VM 一键起开发依赖，服务器一键起全套生产**。

> ⚠️ 本模块横跨两个阶段：**第 2 步只做开发依赖（`infra/docker-compose.dev.yml`，在 VMware-Linux 那台起数据库/Redis/Prometheus/Grafana）**；
> **生产 compose + 镜像 + CI/CD 留到第 19 步**。见下方「开发步骤」。
>
> 📖 逐步照抄的启动/验证清单（含故障速查与证据回填格式）见 [`docs/infra-runbook.md`](../infra-runbook.md)；本节讲「是什么、为什么」，手册讲「怎么点」。本机是 Windows 客户机、**跑不了 Docker**，所以所有 `docker compose` 命令的执行位置都是 Linux VM。

## 2. 对外接口

```bash
# ── 开发期：只起依赖（PostgreSQL+pgvector、Redis、Prometheus、Grafana），代码跑在开发机 venv ──
# 执行位置：VMware-Linux 虚拟机；且必须在【仓库根】执行（原因见下方「为什么带这两个参数」）
docker compose -f infra/docker-compose.dev.yml --project-directory . up -d
docker compose -f infra/docker-compose.dev.yml --project-directory . down      # 停止（保留数据卷）
docker compose -f infra/docker-compose.dev.yml --project-directory . down -v   # 清空重建（连数据一起删）
docker compose -f infra/docker-compose.dev.yml --project-directory . config    # 只看渲染结果，不启动

# ── 生产期：全套服务（第 19 步）──
docker compose up -d
```

**为什么带 `-f infra/...` + `--project-directory .` 这两个参数**（少写任一个都会踩坑）：

| 写法 | 后果 |
|---|---|
| `docker compose -f infra/docker-compose.dev.yml up -d`（只给 `-f`，在仓库根执行） | compose 默认把「第一个 `-f` 文件所在目录」（`infra/`）当成项目目录，`.env` 也在那里找 → **读不到仓库根 `.env`**，`${POSTGRES_USER}` 这类变量替换**静默**退回默认值（不报错、不告警）。与 `config-guide.md` §1.7 讲的「`.env` 是当前工作目录相对、读不到不报错」是同一类坑 |
| `cd infra && docker compose -f docker-compose.dev.yml up -d` | 同样读不到仓库根 `.env`；且项目目录变成 `infra/`，项目名/卷名前缀跟着漂移 |
| 两个参数一起写 | ① 项目目录 = 仓库根，`.env` 定位正确；② 项目名与卷名前缀稳定为 `shopsage-dev_*`（文件里 `name: shopsage-dev` 已显式钉住项目名，卷即 `shopsage-dev_postgres_data` 等）——**靠默认推导时项目名取自项目目录名，换个目录执行就会另起一套空卷，看起来像「数据凭空消失」** |

> 注意：compose 文件里 `./prometheus/prometheus.yml` 这类挂载路径是**相对 `-f` 文件本身**解析的，所以加了 `--project-directory .` 也不会指错，仍指向 `infra/prometheus/`。

## 3. 依赖

- Docker + Docker Compose（跑在 VMware-Linux 虚拟机上；开发机 Windows 客户机**没有** Docker，见 §3.1）
- GitHub Actions（Linux runner）
- 读取 `.env`（生产密钥走环境变量注入，见 `config.md`）

### 3.1 开发拓扑（跨主机）

**两台机器的分工是硬约束，不是偏好**：开发机是 VMware 里的 Windows 客户机，本机没有 Docker（5432/6379/9090/3000 全无监听，也没装 PostgreSQL/Redis），因此

```
VMware-Linux 虚拟机  ←── 依赖容器全部跑在这里（infra/docker-compose.dev.yml）
        ▲
        │  通过 VMware 网卡 IP（下称 <Linux-IP>）连接
        │
VMware-Windows 开发机 ←── 业务代码跑在这里的 venv（uvicorn / pytest / arq）
```

这正好落实架构第 9 节「开发期 VMware-Windows + VMware-Linux」：**Linux 侧跑依赖容器，Windows 侧跑代码 venv**，只是把两台机器的职责写明确了。

| 服务 | 容器内端口 | Windows 侧访问地址 | 用途（对应模块） |
|---|---|---|---|
| postgres（`pgvector/pgvector:pg16`） | 5432 | `<Linux-IP>:5432` | `DATABASE_URL`；第 3 步 `db.md` 建表、第 10 步 `rag.md` 向量表 |
| redis（`redis:7-alpine`） | 6379 | `<Linux-IP>:6379` | 第 9 步限流、第 14 步 Arq 队列、第 15 步语义缓存 |
| prometheus（`prom/prometheus:v2.55.1`） | 9090 | `http://<Linux-IP>:9090` | 第 12 步指标采集端 |
| grafana（`grafana/grafana:11.3.0`） | 3000 | `http://<Linux-IP>:3000` | 第 12 步看板；开发期开了**匿名只读**（Viewer），后台账号 `admin`/`admin` |

三条跨主机必须记住的规则：

1. **端口必须绑 `0.0.0.0`，不能绑 `127.0.0.1`**。compose 里的端口映射写作 `${POSTGRES_BIND_IP:-0.0.0.0}:${POSTGRES_PORT:-5432}:5432`（redis/prometheus/grafana 同理），默认就是 `0.0.0.0` —— 因为**连接来自另一台机器**，绑 `127.0.0.1` 只对 Linux VM 自己可见，Windows 连过去会超时/refused，而且极易被误判成「容器没起来」。要更严时，在仓库根 `.env` 里把 `POSTGRES_BIND_IP` 等设成 Linux VM 在 VMware 网段上的地址（如 `192.168.x.x`），只对 VMware 网段开放。
2. **Linux 侧防火墙要放通**：firewalld 用 `sudo firewall-cmd --add-port=5432/tcp --permanent`（6379/9090/3000 同理）后 `sudo firewall-cmd --reload`；ufw 用 `sudo ufw allow from 192.168.x.0/24 to any port 5432 proto tcp`。注意 Docker 发布端口时是自己写 iptables NAT 规则、**会绕过一部分 ufw 规则**，所以别只看 `ufw status` 就下结论 —— 以 Windows 侧实测为准（`Test-NetConnection`，见 §6 第 2 步）。
3. **`<Linux-IP>` 的取法**：Linux VM 上执行 `hostname -I` 或 `ip -4 addr show`（找 VMware 网卡那一项，通常与 Windows 侧 `ipconfig` 里的 VMware 网段同段）。Windows 侧所有 `.env` / 连接串里的 host 都填这个 IP，**不要写 `localhost`**。

## 4. 关键设计决策

1. **开发/生产一份 Compose 拆分**：`infra/docker-compose.dev.yml` 只含基础设施（postgres/redis/prometheus/grafana）；生产 `docker-compose.yml` 含全部服务（api/worker/web）。用 `.env` 区分（架构第 9 节）。
2. **开发期代码不容器化**：开发机（Windows）本机 venv 直接跑，热重载快、好调试；只有「依赖服务」进容器 —— 而容器在 Linux VM 上，见 §3.1 与第 5 条。
3. **镜像在 Linux runner 构建**：`docker/build-push-action` 构建 → push registry → 服务器 `pull` 后 compose 起（架构第 9 节「交付」）。
4. **密钥不入镜像**：用 `env_file` / 运行时 `environment` 注入，绝不用 `ARG` 烧进镜像层。
5. **为什么依赖不跑在开发机（Windows 客户机）**：
   - **技术上不可行**：VMware 里的 Windows 客户机要跑 Docker Desktop，宿主必须先开嵌套虚拟化给 Hyper-V/WSL2 —— 这受宿主 CPU 与策略限制，本机这两条路都不可用（也没有 WSL2）。硬凑的结果是把「环境问题」变成「虚拟化问题」，收益为零。
   - **生产形态本来就在 Linux**：生产是 Linux 服务器上跑 compose（架构第 9 节），Linux VM 开发既解决了依赖，又让「开发时验证的 compose 行为」与生产同源 —— 镜像/healthcheck/端口映射/卷语义在两边一致，省掉一整类「Windows 上能跑、服务器上不行」的偏差。
   - **代价要认**：依赖「不在手边」。① `infra/` 下的 compose 文件**本身无法在开发机上验证**，只能到 Linux VM 或 GitHub Actions runner 上跑 `config -q`（见 §7）；② 排障多一层网络路径，连不上时先分「容器没起」还是「网络/防火墙不通」（§6 第 2 步的验证顺序就是为此设计的）；③ 依赖数据在 Linux VM 的 named volume 里，`down -v` 会连同数据一起删。

## 5. 核心技术点（要懂为什么）

- **多阶段构建（multi-stage）**：Python 镜像用 `python:3.12-slim` 装依赖 → 复制代码，最后只留运行所需层，镜像体积小、攻击面小。（与 `pyproject.toml` 的 `requires-python = ">=3.12"` 对齐；开发机是 3.12.9，镜像只需钉 minor，`3.12-slim` 会跟随官方更新补丁版，避免手工追 CVE 补丁。**别退回 `python:3.11`** —— 会让 CI 与开发机凭空多一个版本差异。）
- **`.env` 只服务本机开发，容器里一律走环境变量**（与 `config.md` 的「同名变量会覆盖 .env」配套）：
  - 本机开发：`infra/docker-compose.dev.yml` 只起依赖（postgres/redis/…，跑在 Linux VM 上），代码跑在开发机 venv，配置来自项目 `.env`。
  - 容器运行：用 compose 的 `environment` / `env_file` 在**运行时**注入，`.dockerignore` 排除 `.env` → 镜像里没有任何配置文件。此时 `Settings` 只读进程环境变量（`.env` 不存在，冲突检测自然跳过，零噪音）。
  - 于是两侧的「同名覆盖」问题各自有明确归属：本机由 `config.md` 的启动告警兜住；容器里环境变量本就是唯一来源，不存在覆盖。
  - ⚠️ 反例：不要为了「让容器内也能读 .env」而把 `.env` 打进镜像或挂到 `WORKDIR` —— 那会把密钥固化进镜像层与卷，违背第 4 节第 4 条。
- **`.dockerignore`**：排除 `.env` / `.git` / `__pycache__` / `venv`，避免把密钥和垃圾打进构建上下文。
- **healthcheck**：给 postgres/redis/api 配 `healthcheck`，compose 用 `depends_on: condition: service_healthy` 保证启动顺序，避免 API 先于数据库起来连不上。
- **为什么 worker 单独一个服务**：Arq 异步任务是常驻进程，和 API 独立扩缩、独立重启（架构第 8 节「按层独立扩缩」）。
- **为什么 postgres 用 `pgvector/pgvector:pg16` 而不是 `postgres:16` + 启动脚本装扩展**：向量能力是**硬依赖**（第 10 步 `rag.md` 要建 `embedding vector(n)` 列与 IVFFlat/HNSW 索引），而这个官方镜像已把扩展**编译进镜像**，`CREATE EXTENSION vector` 开箱可用。换成 `postgres:16` + `/docker-entrypoint-initdb.d/` 脚本装扩展，就多出一条「只在首次初始化数据卷时执行一次」的隐蔽故障链：脚本报错只体现在容器日志里（`up -d` 看起来照样成功）、数据卷已存在时脚本根本不会再跑、编译扩展还要拉构建工具链。**结论：能用现成镜像就别自建初始化流程**，第 10 步建表时不必再关心扩展是否装好。
- **跨主机坑：Prometheus 的 scrape target 不能写 `localhost`**（`infra/prometheus/prometheus.yml`）：
  - Prometheus 跑在 **Linux VM 的容器**里，而指标端点是**Windows 宿主机上的业务代码**（`uvicorn` 的 8000 / worker 的 9100）。此时 target 写 `localhost:8000` 指的是**容器自己**（必然是 target down），不是开发机。
  - 从容器看，Windows 宿主机是 docker 网桥的**网关**，默认 `172.17.0.1`（`ip route` 里 `default via ...` 那一项；自定义 bridge 网段可能不同）。配置里业务 job 先注释掉，等第 12 步 `observability.md` 落地再解开 —— 否则 Prometheus 会一直刷 target down 的噪音。
  - 光改 target 还不够：**Windows 侧也要放通 8000**（防火墙入站规则 / 确认 uvicorn 监听 `0.0.0.0` 而非 `127.0.0.1`）。漏了这一步的症状是「Prometheus 显示 target down、业务侧毫无感知」。
  - 对照：Grafana 挂 Prometheus 数据源走的是**容器网络内**的服务名（`http://prometheus:9090`），所以 provisioning 文件里用服务名而不是 IP —— 同机容器之间和跨机访问是两套寻址方式，别混用。

## 6. 开发步骤

> **本模块分两阶段，不要一次做完**：第 2 步只做「在 Linux VM 起开发依赖」（第 1–2 步），
> 生产部署与 CI/CD（第 3–6 步）留到第 19 步全链路跑通后再做。

**第 2 步（开发依赖，先做）**

1. 写 `infra/docker-compose.dev.yml` + `infra/prometheus/prometheus.yml` + `infra/grafana/provisioning/datasources/prometheus.yml`：`postgres`（`pgvector/pgvector:pg16`）+ `redis` + `prometheus` + `grafana`，配 volume 持久化 + healthcheck，端口绑 `0.0.0.0`（理由见 §3.1）。

2. 按下面顺序验证。⚠️ **本机（Windows）没有 Docker**，所以 `docker compose ...` 全部在 **Linux VM** 上执行，`docker compose config -q` 这类静态校验只能在 **Linux VM 或 GitHub Actions runner** 上做 —— 不要在开发机上试，那里没有 `docker` 命令。
   （这套顺序的完整版、含「期望输出 / 失败症状 → 修法」对照表，见 [`docs/infra-runbook.md`](../infra-runbook.md) §2–§4；下面是要点摘要。）

   **① Linux VM：拿到代码与 `.env`（在仓库根执行）**

   ```bash
   git clone <repo-url> ShopSage && cd ShopSage      # 或从共享目录拷贝现有工作副本
   cp .env.example .env                              # 已有 .env 则跳过，但要按 ② 检查
   grep -E '^(POSTGRES|REDIS|GRAFANA|DATABASE_URL)' .env
   ```

   **② Linux VM：把 `.env` 的 host 改成 Linux VM 自己的 IP**（`hostname -I` 取），并确认「compose 读的 `POSTGRES_*`」与「应用读的 `DATABASE_URL` / `REDIS_URL`」两组变量指向同一个 host/凭据/端口（见 `config-guide.md` §2.4）；要用容器自身的初始化逻辑，就别在这份 `.env` 里留 `localhost`。

   **③ Linux VM：起依赖（在仓库根执行）**

   ```bash
   docker compose -f infra/docker-compose.dev.yml --project-directory . config -q   # 先静态校验，再启动
   docker compose -f infra/docker-compose.dev.yml --project-directory . up -d
   docker compose -f infra/docker-compose.dev.yml --project-directory . ps          # 反复执行直到全部 healthy
   ```

   等 healthcheck 变 `healthy`（postgres/redis 各自有 `pg_isready` / `redis-cli ping` 探针，启动后约 10–30 秒内到位；`starting` 不等于失败，但 `unhealthy` 要看 `docker compose ... logs <服务名>`）。

   **④ Windows：把 `.env` 的 host 改成 `<Linux-IP>`**（PowerShell，在仓库根执行）

   ```powershell
   Copy-Item .env.example .env -ErrorAction SilentlyContinue   # 已有 .env 则跳过
   # 编辑 .env：把 DATABASE_URL / REDIS_URL 的 host 段改成 Linux VM 的 IP（不要写 localhost）
   python -c "from app.config import get_settings; import json; print(json.dumps(get_settings().dump_for_logging(), ensure_ascii=False, indent=2))"
   ```

   上面最后一条是「应用到底连哪儿」的唯一可信来源（输出已掩码，可安全贴出）；改了 `.env` 必须**重启进程**才生效（`config-guide.md` §1.7）。

   **⑤ Windows：连通性验证（先网络、后协议）**

   ```powershell
   Test-NetConnection <Linux-IP> -Port 5432        # TcpTestSucceeded : True
   Test-NetConnection <Linux-IP> -Port 6379
   Test-NetConnection <Linux-IP> -Port 9090
   Test-NetConnection <Linux-IP> -Port 3000
   ```

   这一步是分诊关键：`Test-NetConnection` 不通 → 问题在**端口绑定/防火墙/网段**（§3.1 三条规则），不要去查 SQL；通了但应用报错 → 才是凭据/Schema/驱动层问题。

   **⑥ Windows：把 ①–⑤ 的实际输出回填第 8 节「证据链接」**（`ps` 的 healthy 片段、`Test-NetConnection` 结果、`SELECT version()` 输出）。**未回填即视为未验收。**

**第 19 步（生产部署与 CI/CD，最后做）**

3. 写生产 `docker-compose.yml`：`api`、`worker`、`web`、`postgres`、`redis`、`grafana`、`prometheus`。
4. 写 `app/Dockerfile`（多阶段）+ `web/Dockerfile` + `.dockerignore`。
5. 写 `.github/workflows/ci.yml`：ruff/black/mypy → pytest（含 `eval.md` 评测关卡，阈值不达标阻断）→ build & push。
6. 写部署 job：SSH 到 Linux 服务器 `pull` + `docker compose up -d`。

## 7. 测试用例

```bash
# CI 关卡（任何一条不绿则阻断合并；全部在 CI runner（Linux）上跑）
ruff check . && black --check . && mypy app
pytest
python -m eval.run --suite rag && python -m eval.run --suite agent   # 评测阈值不达标也阻断

# 开发依赖栈的静态校验（在【仓库根】执行）
# 执行位置：Linux VM 或 CI runner —— 开发机（Windows）没有 Docker，跑不了这条
# -q：静默模式，只回退出码（0 = yaml 合法且变量替换通过）；要看渲染结果就去掉 -q
docker compose -f infra/docker-compose.dev.yml --project-directory . config -q
```

- 手工验收：删除容器卷后 `up` 能从头重建；`depends_on` 顺序正确（api 在 postgres healthy 后才起）。
- 跨主机验收：Windows 侧四个端口 `Test-NetConnection` 全通，且应用能真正建立连接（`SELECT version()` 返回 `PostgreSQL 16.x` + `CREATE EXTENSION vector` 不报错）—— 端口通不等于库能用，两步都要做。
- 安全验收：`docker history <镜像>` 里看不到 API Key。

> 以上是**待执行**的关卡清单，不是执行结果；实际通过情况见第 8 节证据。

## 8. 验收标准

- [ ] Linux VM 一条命令起全部依赖，服务器一条命令起全套生产。
- [ ] CI 三关（lint / test / build）全绿且阻断不合规合并。
- [ ] 镜像内无密钥；`.env` 不入 Git、不入镜像。
- [ ] 证据链接（待回填，**每条都必须是可复现命令 + 原始输出，不接受口头结论**）：
  - 依赖栈状态：`docker compose -f infra/docker-compose.dev.yml --project-directory . ps` 的 healthy 输出（在 Linux VM 执行），贴文本或截图。
  - 跨主机连通性（Windows 执行）：`Test-NetConnection <Linux-IP> -Port 5432` / `-Port 6379` / `-Port 9090` / `-Port 3000` 的输出。
  - 应用侧连接成立：`SELECT version()`（以及 `CREATE EXTENSION vector`）的输出；`REDIS_URL` 用 `PING` → `PONG`。
  - `docker compose ... config -q` 的执行位置与结果（Linux VM 或 CI runner，标明是哪台）。
  - `PR#__` / `CI 运行记录#__`（第 19 步补齐，当前留空）。
  - 占位：`<Linux-IP>` = `__.__.__.__`（落地时替换为真实 IP，不要写成 `localhost`）。
