# 开发依赖启动与验证手册（跨主机：Windows 开发机 + Linux 依赖机）

> 面向开发者的**操作指南**：怎么在「本机跑不了 Docker」的前提下，把 infra 第 2 步的依赖栈起来并验证。
> 设计原理与取舍见 [`modules/infra.md`](modules/infra.md)（8 段模板）；本文只讲「怎么做、怎么确认做对了」。
>
> ⚠️ **本文所有结果都是「待执行」，不是执行记录。** 每条验证都给了「期望输出」，请对照实测结果回填
> `modules/infra.md` 第 8 节「证据链接」。命令已在文中标注执行位置（Linux / Windows）。

---

## 0. 30 秒速查

| 我想… | 去哪 | 命令 / 做法 |
|---|---|---|
| 起开发依赖 | Linux VM | §2 步骤 L1–L3 |
| 看容器是否 healthy | Linux VM | §2 步骤 L4 |
| 确认 pgvector 可用 | Linux VM | §2 步骤 L5 |
| 让 Windows 的代码连上库 | Windows | §3 步骤 W1–W2 |
| 分诊「连不上」 | 两侧 | §4 故障表 |
| 回填验收证据 | 两侧 | §5，落到 `modules/infra.md` §8 |

一条铁律：**所有 host 都写 Linux 虚拟机的 IP，不许写 `localhost`。** 写 `localhost` 时，
Windows 上的代码是在连自己这台机器（而它上面什么都没装）。

---

## 1. 环境拓扑与前提

```
┌─────────────────────────────┐         ┌──────────────────────────────────────┐
│  VMware-Windows（开发机）      │         │  VMware-Linux（依赖机）                 │
│                             │         │                                      │
│  · 代码 + uv 管理的 .venv     │  TCP    │  docker compose                      │
│  · 无 Docker / 无 WSL2        │ ──────► │  ├ postgres (pgvector)  :5432        │
│  · 浏览器看 Grafana/Prometheus │  :5432  │  ├ redis                :6379        │
│                             │  :6379  │  ├ prometheus           :9090        │
│                             │  :3000  │  └ grafana              :3000        │
└─────────────────────────────┘         └──────────────────────────────────────┘
```

前提检查（Linux VM 上）：

```bash
docker --version                # 期望：Docker version 2x.x.x
docker compose version          # 期望：Docker Compose version v2.20+（需要支持 compose 的 name: 顶层键与 depends_on.condition）
docker info --format '{{.ServerVersion}}'   # 能出结果 = 守护进程在跑
free -h                         # 四个容器大概吃 1–1.5GB 内存，确认虚拟机给够了
sg docker -c 'docker ps'        # 若提示 permission denied，说明当前用户在 docker 组里没生效
```

> 若 `docker` 需要 `sudo`：要么 `sudo usermod -aG docker $USER` 后重新登录，要么全程加 `sudo`。

---

### 1.1 Windows 侧：Python 版本与 uv 虚拟环境（本步之前先做）

**版本口径（已定，不要再改）**：Python **3.12**，开发机是 **3.12.9**；
**虚拟环境与依赖全部走 `uv`**（不再用 `python -m venv` + `pip`，理由见下）。

| 位置 | 写的是什么 | 说明 |
|---|---|---|
| 开发机 | uv **0.11.33**（实测）、Python 3.12.9 | `uv --version`；`py -0p` 只有 3.12 一个版本 |
| `pyproject.toml` | `requires-python = ">=3.12"`、`[dependency-groups] dev`、`[tool.uv] required-version = ">=0.11"` | dev 依赖用依赖组，`uv sync` 默认就装 |
| 生产镜像（第 19 步） | `python:3.12-slim` + `uv sync --locked` | 只钉 minor；容器内用 `--locked` 保证与 `uv.lock` 一致 |
| CI（第 19 步） | `astral-sh/setup-uv` + `python-version: "3.12"` | 用 `uv sync --locked` 跑 pytest |

**以下全部为实测结果**（命令在仓库根执行，均已在本机跑通）：

| # | 步骤 | 实测结果 |
|---|---|---|
| 1 | `uv --version` / `py -0p` | ✅ `uv 0.11.33`；只有 `-V:3.12 *`（3.12.9） |
| 2 | `uv sync` | ✅ 建出 `.venv`（CPython 3.12.9）+ 生成 `uv.lock`（31 KB），装上 7 个包 |
| 3 | `uv run python -c "import sys; print(sys.version, sys.prefix)"` | ✅ `3.12.9` / prefix = 仓库根的 `.venv` |
| 4 | `uv run pytest -q` | ✅ **`54 passed in 1.82s`**（含 6 条 `tmp_path` 用例，全过） |
| 5 | `uv run python -c "..." <参数>` 能正确转发参数 | ✅ 已验证（`sys.argv[1]` 收到的就是那个路径） |

```powershell
# ── 在仓库根执行（uv 会自己找/下解释器，不需要先激活任何东西）──
uv sync                       # 建 .venv + 装依赖（默认含 [dependency-groups] dev，不用加 --extra dev）
uv run python -c "import sys; print(sys.version, sys.prefix)"   # 期望：3.12.9 ...\.venv
uv run pytest -q              # 期望：54 passed
```

**日常命令对照表（旧 → 新）**：

| 传统方式（已弃用） | 现在用 uv |
|---|---|
| `python -m venv .venv` | `uv sync`（自动建 `.venv`，可 `uv python pin 3.12` 固定解释器） |
| `.\.venv\Scripts\Activate.ps1` | **不需要激活**：`uv run <命令>` 直接在项目环境里执行 |
| `pip install -e ".[dev]"` | `uv sync`（dev 依赖在 `[dependency-groups] dev`，默认安装） |
| `pip install <pkg>` | `uv add <pkg>`（同时更新 `pyproject.toml` 与 `uv.lock`，**不要手写 dependencies**） |
| `pip install --dev <pkg>` / extras | `uv add --dev <pkg>` |
| `python -m pytest -q` | `uv run pytest -q` |
| `pip freeze > requirements.txt` | `uv.lock`（提交进 Git；`uv sync --locked` 复现，不需要 requirements.txt） |
| `pip install -U` | `uv lock --upgrade` 然后 `uv sync` |

> **注①：为什么要换掉 `python -m venv`**
> 它依赖 `ensurepip` 往**临时目录**解压捆绑的 pip wheel —— 我在受限沙箱里实测会直接失败：
> `PermissionError: [Errno 13] ...pip-24.3.1-py3-none-any.whl`，结果 `.venv` 建出来了但里面**没有 pip**。
> uv 自己管理解释器与缓存（`%LOCALAPPDATA%\uv\cache`、`%APPDATA%\uv\python`），**不经过 ensurepip**，
> 所以在本机一次性跑通（上表第 2 行）。顺带的好处：`uv sync` 比 `pip install` 快得多，且 `uv.lock` 让
> 「我这能跑、你那不能跑」的依赖漂移消失。

> **注②：`uv.lock` 要提交，`请勿`手改**。它是解析结果的唯一事实来源；
> 换机器/换 CI 只需 `uv sync`（复现用 `uv sync --locked`：lock 与 `pyproject.toml` 不一致时**直接失败**，
> 这正是 CI 想要的）。`.venv/` 已在 `.gitignore` 里，不会被提交；`uv.lock` **不在** ignore 列表里，应随代码提交。

> **注③：跑测试时的两个已知现象**
> - 上表第 4 行的 `54 passed` 是**uv 建的 venv 里的结果**；用全局解释器裸跑时，6 条用 `tmp_path` 的用例
>   可能在受限环境报 `PermissionError`（见 `config-guide.md` §3.5/§3.6）。也就是说：**用 uv 跑更接近 CI 真相**。
> - `uv run` **不会**改变工作目录：它只保证「用项目 `.venv` 的解释器」。因此「必须在仓库根执行」这条对 uv 同样成立 ——
>   实测在 `docs/` 下 `uv run pytest -q` 会 `no tests ran`（退出码 5）、`uv run python -c "import app"` 会
>   `ModuleNotFoundError`。本文与 `config-guide.md` 的命令都默认你在**仓库根**。

## 2. Linux VM：起依赖栈并验证

## 2. Linux VM：起依赖栈并验证

### L1. 把仓库放到 Linux VM 上

```bash
# 用虚拟机【原生文件系统】（如 ~/ShopSage）
git clone <你的仓库地址> ~/ShopSage
cd ~/ShopSage
```

> ⚠️ **别用 VMware 共享文件夹（hgfs / /mnt/hgfs）放这个仓库**：`infra/prometheus/prometheus.yml` 与
> `infra/grafana/provisioning/` 是以 bind mount 方式挂进容器的，共享目录的权限映射常导致容器读不到配置，
> 表现为 Prometheus 起不来或 Grafana 没有数据源。原生目录最省事。

记下这台机器的 IP，后面 Windows 侧要用：

```bash
hostname -I                 # 期望：输出一个或多个 IP，取 VMware 网段那个（NAT 常见 192.168.x.x）
ip -4 addr show | grep inet # 上一条不灵时用这个看具体网卡
```

### L2. 准备 `.env`（compose 和代码都读它）

```bash
cp .env.example .env
grep -E '^(POSTGRES_|DATABASE_URL|REDIS_URL)' .env
```

期望看到（**`.env.example` 里已预置这组默认值**）：
`POSTGRES_USER=shopuser`、`POSTGRES_PASSWORD=shoppass`、`POSTGRES_DB=shopsage`。

要改的话记住这条硬约束：**`POSTGRES_*` 与 `DATABASE_URL` 必须指向同一套凭据与端口**
（前者被 compose 用来初始化容器，后者被应用用来建连接）——改一边忘一边就是「容器 healthy 但应用连不上」。

可选收紧：想让数据库只对 VMware 网段开放，在 `.env` 里加 `POSTGRES_BIND_IP=<Linux-IP>`；
不加则默认绑 `0.0.0.0`（本项目开发期的默认选择）。

### L3. 先校验配置，再启动

```bash
# ① 语法校验（这就是 infra.md §7 那条 CI 关卡，本机 Windows 跑不了，只能在这台或 CI runner 上跑）
docker compose -f infra/docker-compose.dev.yml --project-directory . config -q
echo "exit=$?"
#    期望：无任何输出，exit=0。有输出 = YAML 或字段写错，照报错行号改。

# ② 看渲染结果（确认变量替换成功、没有空的必填项）
#    ⚠️ 不要直接 config | head —— 渲染结果里含明文库密码。
#    只想看端口和挂载这种「无密钥」部分：
docker compose -f infra/docker-compose.dev.yml --project-directory . config \
  | grep -E 'published|target|image:'

# ③ 启动
docker compose -f infra/docker-compose.dev.yml --project-directory . up -d
```

### L4. 等 healthy（第 2 步的核心验收）

```bash
docker compose -f infra/docker-compose.dev.yml --project-directory . ps
```

**期望输出**：4 个服务，`STATUS` 全部为 `Up ... (healthy)`：

| 服务 | 容器名 | 端口 | 健康判定方式 |
|---|---|---|---|
| postgres | shopsage-dev-postgres | 5432 | `pg_isready` |
| redis | shopsage-dev-redis | 6379 | `redis-cli ping` |
| prometheus | shopsage-dev-prometheus | 9090 | `promtool check healthy` |
| grafana | shopsage-dev-grafana | 3000 | `/api/health` |

第一次 `up` 时 postgres 初始化数据目录需要十几秒，`(health: starting)` 是正常的；持续 `unhealthy` 才去 §4 查。

```bash
# 没起来时看日志（不要只看最后一个服务）
docker compose -f infra/docker-compose.dev.yml --project-directory . logs --tail=50 postgres
```

### L5. 验证 pgvector 扩展可用（第 3 步 db.md 的前置）

```bash
docker compose -f infra/docker-compose.dev.yml --project-directory . \
  exec postgres psql -U shopuser -d shopsage \
  -c "CREATE EXTENSION IF NOT EXISTS vector;" \
  -c "SELECT extname, extversion FROM pg_extension WHERE extname='vector';"
```

**期望**：第二条输出一行 `vector | 0.7.x`（版本号随镜像版本浮动）。
这一条过了，第 10 步 rag 的 `embedding vector(n)` 列与 IVFFlat/HNSW 索引才有地基。

### L6. 确认监听地址是 `0.0.0.0`

```bash
ss -lntp | grep -E ':(5432|6379|9090|3000)'
```

**期望**：`LISTEN` 那一列是 `0.0.0.0:5432`（或 `*:5432`）。
**如果是 `127.0.0.1:5432`** → Windows 永远连不进来，说明端口映射被写窄了，改 `infra/docker-compose.dev.yml`
里对应的 `ports:`（本仓库默认已是 `${...BIND_IP:-0.0.0.0}`，只有你手动改成过 `127.0.0.1` 才会这样）。

### L7. 放通防火墙

```bash
# firewalld（CentOS/RHEL 系）
sudo firewall-cmd --add-port=5432/tcp --add-port=6379/tcp --add-port=9090/tcp --add-port=3000/tcp
sudo firewall-cmd --runtime-to-permanent

# ufw（Ubuntu/Debian 系）—— 注意 ufw 默认会放通 Docker 自己写的规则，
# 但它和 docker 的链式规则交互有坑，最可靠的做法是「以 Windows 侧实测为准」
sudo ufw status
```

> **判断标准只有一个**：下一节的 `Test-NetConnection`。防火墙配置对不对，不由 `firewall-cmd --list-all` 决定。

---

## 3. Windows：让本机代码连上依赖

### W1. 改本机 `.env` 的 host

```powershell
# 在仓库根（C:\Users\13398\Desktop\DeepSeekHarness\ShopSage）
# 把 .env.example 里的 192.168.x.x 占位符换成 L1 记下的 Linux VM IP：
#   DATABASE_URL=postgresql://shopuser:shoppass@<LINUX-IP>:5432/shopsage
#   REDIS_URL=redis://<LINUX-IP>:6379/0
notepad .env
```

改完核对（这条命令会掩码敏感字段，可以放心贴出来）：

```powershell
uv run python -c "from app.config import get_settings; import json; print(json.dumps(get_settings().dump_for_logging(), ensure_ascii=False, indent=2))"
```

**期望**：`database_url` 的 host 显示为你填的 Linux IP、密码显示为 `***`。
如果提示 `ModuleNotFoundError: No module named 'app'`，说明不在仓库根 —— 见 `config-guide.md` §1.7。

> 若同时看到「配置冲突：进程环境变量 DATABASE_URL 正在覆盖 .env」告警，说明你机器上有个同名用户级
> 环境变量在抢，按 `config-guide.md` §1.5 查名字再决定删哪个。

### W2. 四端口连通性（第 2 步「能连」的最直接证据）

```powershell
$ip = '<LINUX-IP>'
5432,6379,9090,3000 | ForEach-Object {
  $r = Test-NetConnection $ip -Port $_
  "{0,-5} -> {1}" -f $_, $(if ($r.TcpTestSucceeded) { 'OPEN' } else { 'BLOCKED' })
}
```

**期望**：四行全 `OPEN`。任何一个 `BLOCKED` 就照下表分诊（前两条覆盖 90% 的情况）。

| 症状 | 检查 | 结论 / 修法 |
|---|---|---|
| 全 BLOCKED | L6 的 `ss -lntp` 输出 | 绑成了 `127.0.0.1` → 改 compose 的 `ports` 为 `0.0.0.0:xxx:xxx` |
| 全 BLOCKED（且 L6 正常） | Linux 上 `firewall-cmd --list-all` / `ufw status` | 防火墙没放通 → 执行 L7 |
| 单个 BLOCKED | `docker compose ... ps` | 该服务没起来/在 restart 循环 → 看它的 `logs` |
| ping 得通但端口不通 | 两边 `ip -4 addr show` | 不在同一网段（NAT vs 仅主机 vs 桥接）→ 在 VMware 里统一网络模式 |
| Windows 上能连、容器间不能连 | `docker compose exec redis redis-cli ping` | 容器间走 compose 网络，与服务名 `postgres`/`redis` 解析，与宿主 IP 无关 |

### W3. 浏览器确认可观测栈（第 12 步的前置彩排）

- `http://<LINUX-IP>:9090` → Status → Targets：`prometheus` 这个 job 应为 **UP**
  （业务 API 的 job 在 `infra/prometheus/prometheus.yml` 里默认被注释掉，第 12 步再启用，现在看到只有 1 个 target 是对的）
- `http://<LINUX-IP>:3000` → 开发期已开匿名只读，直接进；Connections → Data sources 里应已自动存在
  **Prometheus**（由 `infra/grafana/provisioning/datasources/prometheus.yml` 挂好，不需要手点）

### W4. 协议层验证（真正「库能用」，不只是端口通）

这一步要等 `psycopg` 进依赖表（`db.md` 第 3 步会做，届时用 `uv add "psycopg[binary]"` 加依赖、`uv sync` 装）。
在那之前，端口 OPEN 就够本步验收。

```powershell
# 装了 psycopg 之后（第 3 步及以后）
uv run python -c "import os,psycopg; from dotenv import dotenv_values; d=dotenv_values('.env'); print(psycopg.connect(d['DATABASE_URL']).execute('select version()').fetchone()[0])"
# 期望：打印 PostgreSQL 16.x ... 一行

# Redis（装了 redis 包之后）
uv run python -c "import redis; from dotenv import dotenv_values; print(redis.from_url(dotenv_values('.env')['REDIS_URL']).ping())"
# 期望：True
```

> 想立刻验证协议层又不想等第 3 步，可以在 **Linux VM** 上跑（容器里自带客户端）：
> ```bash
> docker compose -f infra/docker-compose.dev.yml --project-directory . exec redis redis-cli ping   # 期望 PONG
> ```

---

## 4. 故障速查

| 症状 | 最可能的原因 | 怎么确认 | 修法 |
|---|---|---|---|
| `uv sync` 报 `Failed to initialize cache at ...\uv\cache` / `拒绝访问` | uv 全局缓存目录不可写（权限、受控文件夹访问、企业策略） | 看报错里那个路径 | 按报错路径修权限；或临时改缓存位置：`$env:UV_CACHE_DIR="$PWD\.uv-cache"; uv sync`（用完记得清） |
| `uv sync` 报解释器缺失/`No interpreter found` | 本机没有 uv 认得的 3.12，且不打算让它下载 | `uv python list --only-installed` | `uv python install 3.12`（uv 会自己下，不经过 ensurepip） |
| `.venv` 建出来但没有 pip | 那是**旧的** `python -m venv` 失败留下的残留（uv 的 venv 本来就不需要 pip） | `uv run python -c "import sys; print(sys.prefix)"` | 删掉 `.venv` 后改用 `uv sync`；本仓不再支持 `python -m venv` 工作流 |
| 改了 `pyproject.toml` 但依赖没变 | 没同步 lock / 没重跑 sync | `uv sync` 的输出 | `uv add` / `uv remove` 改依赖（会自动更新 `uv.lock`），改完 `uv sync` |
| `docker compose config` 报变量为空 | 没走 `--project-directory .`，根 `.env` 没被读到（**静默**） | `docker compose ... config \| grep published` 看端口有没有渲染出来 | 命令写全 `-f infra/docker-compose.dev.yml --project-directory .` |
| postgres 反复 unhealthy | 数据卷是旧版本初始化的 / 密码改过但卷没重建 | `docker compose ... logs postgres` 是否报 `database ... does not exist` 或认证失败 | `down -v` 后重来（⚠️ 会删数据），或改回原凭据 |
| 改了 `POSTGRES_PASSWORD` 但没生效 | postgres 的初始化变量**只在空数据卷时生效一次** | 卷已存在 | `down -v` 重建，或进库 `ALTER USER` |
| Grafana 没数据源 | provisioning 文件没挂上（常见于仓库放在共享文件夹） | `docker compose ... logs grafana \| grep -i provision` | 仓库挪到 Linux 原生目录后重建 |
| Prometheus 起不来 | 配置 bind mount 权限问题 | `docker compose ... logs prometheus` | 同上；`chmod 644 infra/prometheus/prometheus.yml` |
| 端口被占用 | Linux 上已有 postgres/redis（或别的项目） | `ss -lntp \| grep 5432` | 在 `.env` 里改 `POSTGRES_PORT`，**并同步 `DATABASE_URL` 的端口** |
| Windows 报 `psycopg` 缺失 | 第 3 步的依赖还没加，**或**命令没走 `uv run`（用到了全局解释器） | `uv run python -c "import sys; print(sys.prefix)"` 看是不是仓库根的 `.venv` | 用 `uv add psycopg[binary]`（第 3 步）加依赖，并用 `uv run` 执行命令 |
| `ModuleNotFoundError: No module named 'app'` | 用裸 `python` 在非仓库根执行（`app/` 靠当前工作目录进 `sys.path`） | `Get-Location` | 改用 `uv run`（固定项目根），或 `cd` 回仓库根；见 `config-guide.md` §1.7 |
| `docker` 提示 permission denied | 用户不在 docker 组 | `groups` | `sudo usermod -aG docker $USER` 后重新登录 |

**别做的事**：不要为了「让容器也能读 `.env`」把宿主 `.env` 挂进容器（`modules/infra.md` 第 4 节第 4 条、
`config-guide.md` §2.3 第 2 条）——密钥会落进卷，且「容器只读环境变量」这条契约会失效。

---

## 5. 验收与证据回填

跑完把下面这些**原始输出**贴进 `modules/infra.md` 第 8 节「证据链接」（`<Linux-IP>` 替换成真实 IP）：

| 验收项 | 证据（命令） | 期望 |
|---|---|---|
| compose 语法合法 | `docker compose -f infra/docker-compose.dev.yml --project-directory . config -q` | 无输出，exit 0 |
| 一条命令起全部依赖 | 同上 `up -d` + `ps` | 4 个服务全 `(healthy)` |
| pgvector 可用 | L5 的 `SELECT extname, extversion ...` | `vector \| 0.7.x` |
| Windows 侧能连 | W2 的四行 `OPEN/BLOCKED` | 四行全 `OPEN` |
| 配置取值正确 | W1 的 `dump_for_logging()` 输出 | host = `<Linux-IP>`，密码为 `***` |
| 可观测栈可用 | W3 的两个 URL + Target UP 截图 | Prometheus target UP、Grafana 数据源已挂 |
| 清空可重建 | `down -v` 后再 `up -d`（**会删数据**，确认后再做） | 能重新 healthy |

回填格式约定（`modules/README.md` 三条铁律之一）：`证据链接` 处写「命令 + 输出摘要 + 日期」，
例如 `2026-09-20 · compose ps 全 healthy · pgvector 0.7.4`。**不要写没跑过的结论、不要勾没验证的复选框。**

---

## 6. 记住这五个坑（面试/复述用）

1. **compose 的 `.env` 是「项目目录相对」的**，读不到时不报错、静默退默认值 —— 与 `config-guide.md` §1.7 同一个坑。
2. **`POSTGRES_*` 与 `DATABASE_URL` 是两套消费者**（compose 初始化 vs 应用连接），必须同步改。
3. **端口必须绑 `0.0.0.0`** 才能被跨主机的 Windows 连到；`127.0.0.1` 只对自己可见。
4. **postgres 的初始化环境变量只在空卷时生效一次** —— 「改了密码不生效」几乎都是这条。
5. **Prometheus 容器里的 `localhost` 是它自己**，要采集 Windows 上跑的代码得用 docker 网桥网关（默认 `172.17.0.1`）。
