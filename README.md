# ShopSage — 电商企业智能助手

接入企业数据库（用户/订单/商品数据）与内部知识库的多 Agent 协作系统，同时服务「客户问题处理」与「内部员工需求」，支持 Docker 容器化部署到 Linux 服务器。

---

## 🚀 想快速开始？先看这里（3 条路，选一条）

| 你的目的 | 直接读 | 耗时 |
|---|---|---|
| **第一次接手，想搞懂这套系统在干什么** | [`docs/review/architecture/diagrams.md`](docs/review/architecture/diagrams.md) | 15 min |
| **只想把它跑起来** | 本文 [§4 本地开发](#4-本地开发：起服务) → [§6 容器化部署](#6-容器化部署) | 40 min |
| **想知道现在哪里有问题、别踩什么坑** | [`docs/review/02-异常清单.md`](docs/review/02-异常清单.md) + [`docs/review/03-待确认清单.md`](docs/review/03-待确认清单.md) | 25 min |

> 📌 **文档整理成果总入口：[`docs/review/SUMMARY.md`](docs/review/SUMMARY.md)**（完成情况、Top5 问题、必读清单、质量自检、已知边界）。
> 如果只读一个文件，读它。

---

## 1. 这是什么

两条业务线，三个入口：

| 入口 | 使用者 | 典型需求 |
|---|---|---|
| **对客** `/` | 客户 | 查订单状态、退换货咨询、产品咨询、物流查询、**申请退款** |
| **对内** `/internal` | 员工（含客服的退款审批台 `/internal/refunds`） | 销售数据分析、库存查询、客户信息查询、生成报表、**审批退款** |
| **管理台** `/admin` | 管理员 | 开账号 / 停用 / 改角色 / 重置口令 |

技术形态一句话：**FastAPI + LangGraph 多节点图 + LiteLLM，PostgreSQL(带 pgvector) + Redis，Next.js 前端，Docker Compose 部署。**

一次问答的完整链路（骨架帧 → 图 → 答案帧）见 [`docs/review/architecture/diagrams.md`](docs/review/architecture/diagrams.md) §3 时序图。

---

## 2. 文档导航（三张表，别混看）

### 2.1 先读这三份（面向"理解"）

| 文档 | 内容 | 建议阅读时机 |
|---|---|---|
| [`docs/review/architecture/diagrams.md`](docs/review/architecture/diagrams.md) | 系统上下文图 / 分层依赖图 / 一次请求时序图 / 图内部拓扑（Mermaid，每个节点带代码位置） | **最先读**，建立整体认知 |
| [`docs/review/architecture/modules.md`](docs/review/architecture/modules.md) | 模块表（职责/接口/依赖/代码位置）+ 4 条阅读路径（按 RPA→agent 背景定制） | 第二次读 |
| [`docs/review/architecture/glossary.md`](docs/review/architecture/glossary.md) | 术语表：`user_id` vs `subject_id`、`can_enter_internal` vs `canUseInternal`、`blocked` vs 挂起…… | 遇到不认识的词时查 |

### 2.2 干活时查（面向"做"）

| 文档 | 内容 | 什么时候用 |
|---|---|---|
| [`docs/review/consolidated/`](docs/review/consolidated/) | 全部原始文档的**精简版**：16 个模块篇 + 7 个 runbook 主题篇 + 变更台账 + 问题台账 + 语料清单 | 想快速知道某模块"是什么、坑在哪" |
| [`docs/review/tech-stack/index.md`](docs/review/tech-stack/index.md) | 技术栈总览 + **P0/P1/P2 学习优先级** + 10 篇技术分册（LangGraph / FastAPI+asyncio / sqlglot / LiteLLM / pgvector+RAG / Redis+arq / JWT+Argon2 / OTEL+Prometheus / Next.js / MCP） | 要补某个技术时 |
| [`docs/review/appendix/问题定位索引.md`](docs/review/appendix/问题定位索引.md) | **现象 → 先看哪 → 判据 → 详见**，6 类共 40 条 + 脚本速查 | 出故障时按现象查 |
| [`docs/review/appendix/存疑保留.md`](docs/review/appendix/存疑保留.md) | 拿不准该不该删、但保留在原处的文档索引 | 想知道某份旧文档还算不算数 |

### 2.3 原始文档（面向"细节与溯源"）

> ⚠️ **以下文档是整理前的原始版本，部分内容已过时**。过时项清单见 [`docs/review/02-异常清单.md`](docs/review/02-异常清单.md)（24 条，每条带位置与裁决）。
> **判断优先级：代码 > `docs/review/` > 原始文档。**

| 文档 | 内容 | 注意 |
|---|---|---|
| [`docs/architecture.md`](docs/architecture.md) | 技术框架、模块划分、多 Agent 协作、数据库 Schema | ⚠️ §2 技术栈表有过时项（见下 §8） |
| [`docs/problem-solving.md`](docs/problem-solving.md) | 痛点 → 方案 → 技术点 → 指标的工程问题清单（A1–G1） | ⚠️ A3/G1 状态需按代码复核 |
| [`docs/modules/`](docs/modules/README.md) | 每个模块的开发 + 测试文档（8 段模板） | 先看 `docs/review/consolidated/modules/` |
| [`docs/runbook/`](docs/runbook/) | 实操手册（编号与 `docs/modules/` 对齐） | 先看 `docs/review/consolidated/runbook/` |
| [`docs/changes/`](docs/changes/README.md) | 需求变更台账 001–003 | 想加/改功能时，先在这里开变更单 |
| [`issues/`](issues/README.md) | 问题台账 001–010（现象/根因/**判据**/处理） | 「这算不算坏了」先查这里 |
| [`docs/环境迁移验收.md`](docs/环境迁移验收.md) | 换机器后的逐步验收清单 | 含历史 IP，勿照抄 |
| [`docs/kb/`](docs/kb/) | **运行时语料**（8 份，灌进 pgvector） | 不是说明书，改它等于改答案 |

---

## 3. 技术栈（**以代码为准**）

> ⚠️ `docs/architecture.md:21-37` 的"确认版"技术栈表里 **4 项与代码不符**（LlamaIndex / LangChain SQLDatabaseToolkit / GitHub Actions / 测试数字）。
> 下表重建自 `pyproject.toml` + `web/package.json`。详细分册见 [`docs/review/tech-stack/`](docs/review/tech-stack/index.md)。

| 层 | 实际选型 | 说明 |
|---|---|---|
| 语言 / Web | Python ≥3.12 + **FastAPI** + uvicorn | 异步、类型友好 |
| 依赖 / 虚拟环境 | **uv** | `uv sync` / `uv run`，不用 `venv + pip` |
| Agent 编排 | **LangGraph** ≥1.2.14 | 有状态图 + checkpointer 断点重放（`durability="sync"`） |
| LLM 接入 | **LiteLLM** + tenacity | 一家接口打多家（deepseek + dashscope embedding） |
| Text-to-SQL | **sqlglot（自研 AST 守卫）** | *不是* LangChain SQLDatabaseToolkit |
| RAG | **自研**（解析/分块/混合检索/rerank） | *不是* LlamaIndex |
| 向量库 | **pgvector**（HNSW 索引，`vector(1024)`） | 与业务库同实例 |
| 业务库 | **PostgreSQL** + SQLAlchemy 2 + psycopg3 | 读写双引擎（只读角色兜底） |
| 迁移 | **Alembic** | 7 个迁移，单链 |
| 缓存 / 会话 | **Redis** | 限流桶 / 语义缓存快路径 / arq 队列 |
| 异步任务 | **arq** | 重试分级 + 死信表 + 人工重放 |
| 鉴权 | **PyJWT**(HS256) + **Argon2id** | 令牌带 `token_version`（立即失效） |
| 可观测 | **OpenTelemetry + Prometheus + Grafana + Langfuse** | 指标 / span / LLM 记录 |
| 评测 | **自研**（指标名对齐 Ragas，不装 ragas） | golden dataset + 单/多 Agent 对照 |
| 协议 | **MCP** SDK 2.x（`MCPServer`） | 4 个工具对外暴露 |
| 前端 | **Next.js 15.5.27** + React 19 + Tailwind 4 + ECharts 5 | App Router，standalone 产物 |
| 部署 | **Docker Compose** | 9 个服务，详见 §6 |
| CI/CD | ❌ **未落地** | 仓库无 `.github/` |

---

## 4. 本地开发：起服务

> **前置**：Python ≥3.12、uv ≥0.11、Node ≥22、PostgreSQL + pgvector、Redis。
> 依赖栈怎么起见 [§6.1 或 `docs/review/consolidated/runbook/环境与部署.md`](docs/review/consolidated/runbook/环境与部署.md)。

```bash
# ① 装依赖（仓库根执行；uv run 不改工作目录，命令必须在仓库根）
uv sync

# ② 配置：复制模板 → 填真实值
cp .env.example .env
#   DATABASE_URL / REDIS_URL / LLM_API_KEY / JWT_SECRET 必填
#   JWT_SECRET 生产必须换随机值：openssl rand -hex 32（ENV=prod 用开发默认密钥会拒绝启动）
#   ENV=prod 时缺 DATABASE_URL 或 LLM_API_KEY 会【直接拒绝启动】

# ③ 建表 + 灌数据
uv run alembic upgrade head
uv run python -m scripts.seed          # 幂等
uv run python -m scripts.db_check --check

# ④ 灌知识库语料（约 45–82s）—— 漏了这步检索会全空
uv run python -m scripts.load_knowledge_base

# ⑤ 起后端
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
#   ⚠️ 不要加 --reload：本机实测起不来（watchfiles 要起子进程）
#   /docs 看 OpenAPI · /health 看依赖状态 · WS /api/chat/stream 走流式
```

```bash
# ⑥ 起前端（另开一个终端）
cd web
npm install --ignore-scripts          # ⚠️ 必须带 --ignore-scripts
npm run dev                           # → http://127.0.0.1:3000
```

**登录**（演示账号，口令在 `scripts/seed.py::DEMO_ACCOUNTS`，刻意不打印到终端）：

| 账号 | 落在 |
|---|---|
| `admin` | `/admin`（管理台） |
| `13800000002` | `/`（对客） |
| `employee` | `/internal`（对内） |

> ⚠️ **打开页面请用 `http://127.0.0.1:3000` 或 `http://localhost:3000`**。
> `next dev` 打印的 `Network: http://<网卡IP>:3000` **默认不在跨源白名单里**，照它打开会
> 「页面能开、WebSocket 也连不上、发消息没反应」——**症状极像后端坏了**。
> 要按实际地址核对：`uv run python -m scripts.check_web_cors --web http://<该地址>:3000`

**常见启动失败**：[`docs/review/appendix/问题定位索引.md`](docs/review/appendix/问题定位索引.md) §A（起不来 / 连不上，8 条）。

---

## 5. 常用命令速查

全部在**仓库根**执行（前端命令在 `web/`）：

```bash
uv run pytest -q                              # 全量测试（连不上库时 db 用例自动 skip）
uv run pytest -q -m "not db"                  # 只跑不需要数据库的用例
uv run pytest -q tests/api                    # 按目录/文件选
uv run alembic upgrade head                   # 建表 / 升级
uv run python -m scripts.seed                 # 灌演示数据（幂等）
uv run python -m scripts.db_check --check     # 验收断言：缺表/行数不足则退出码 1
uv run python -m scripts.load_knowledge_base  # 灌知识库语料（跑完全量回归后必须重灌）
uv run python -m scripts.check_infra          # 配置自检
uv run python -m scripts.dev_token --account admin   # 签一个演示令牌（按账号签）
uv run python -m scripts.db_readonly --regrant-only  # ⚠️ 迁移回滚后必须重放只读授权
```

```bash
cd web
npm test                       # 离线用例（node --test，不连后端）
npm run typecheck
npm run build                  # 构建通过（验收第 1 条）
node tests/run-live.mjs        # 真链路（需后端在跑）
```

**取证脚本**（⚠️ 会**真的改演示库**，只该对开发/演示库跑，跑完自行复原）：

```bash
uv run python -m scripts.evidence_accounts    # 账号与口令确实在库里（Argon2id，非明文）
uv run python -m scripts.evidence_paths       # 三条主路径回归（需后端在跑）
uv run python -m scripts.evidence_refunds     # 退款申请/审批闭环（需后端在跑）
node scripts/admin-create-evidence.mjs        # 真浏览器：开账号
node scripts/admin-patch-evidence.mjs         # 真浏览器：停用（验 CORS 预检）
node scripts/admin-role-evidence.mjs          # 真浏览器：改角色
node scripts/logout-during-llm-evidence.mjs   # 真浏览器：等 LLM 时退出登录
```

后端地址可用 `SHOPSAGE_API` 覆盖（默认 `http://127.0.0.1:8000`）——**改完代码后原进程不会自动更新**，取证前先确认端口上跑的是哪一版。

---

## 6. 容器化部署

两种形态都支持，**差异只在少数几处**：

| 项 | ① 独立 Linux 服务器 | ② Windows + Docker Desktop + WSL2 |
|---|---|---|
| 执行位置 | 服务器 `~/ShopSage` | **WSL Ubuntu 的 `~/shopsage`**（原生文件系统，不是 `/mnt/e/...`） |
| `NEXT_PUBLIC_API_BASE` | `http://<服务器IP>:8000` | 用默认 `http://localhost:8000` |
| `API_CORS_ORIGINS` | `https://shopsage.example.com` | `http://localhost:3000,http://127.0.0.1:3000` |
| `EMBEDDING_API_KEY` | 可留空（同厂商时才可） | **必须填**（主模型 DeepSeek、embedding dashscope → 否则首次检索 401） |

> ⚠️ 上面每一项**照抄服务器口径到本机都会静默失败**（不报错，只是连不上或 401）。

### 6.1 一次性部署（Linux 侧仓库根执行）

```bash
cp env.list.example env.list && chmod 600 env.list        # 里面全是密钥
docker compose -f docker-compose.yml --project-directory . config -q   # 只读校验
docker compose -f docker-compose.yml --project-directory . build
docker compose -f docker-compose.yml --project-directory . up -d postgres redis
docker compose -f docker-compose.yml --project-directory . run --rm migrate
docker compose -f docker-compose.yml --project-directory . up -d --wait
uv run python -m scripts.check_infra
```

9 个服务：`migrate`（一次性）/ `api` / `worker` / `web` / `postgres` / `redis` / `prometheus` / `grafana` / `otel-collector`（`--profile tracing`）。
`migrate` / `api` / `worker` **共用同一个后端镜像**（换命令不换镜像）——这保证「入队用的任务名」与「消费用的实现」是同一份代码。

### 6.2 改完之后跑哪条（速查）

| 改了什么 | 命令 | 丢数据？ |
|---|---|---|
| `env.list` | `up -d --force-recreate api worker` | 不丢 |
| `app/**` `.py` | `up -d --build api worker`（改模型再补 `run --rm migrate`） | 不丢 |
| `web/**` 或 `NEXT_PUBLIC_API_BASE` | `up -d --build web`（**重启无效**，它是构建期变量） | 不丢 |
| `pyproject.toml` / `uv.lock` | `up -d --build` | 不丢 |
| `infra/prometheus/**` | `up -d --force-recreate prometheus` | 不丢 |
| 彻底重来 | `down -v && up -d` | ⚠️ **全丢** |

> **两条铁律**：① `docker compose restart` **不重新解析 YAML、不重读 `env.list`**；② `env.list` 的改动只对**下一次创建的容器**生效。

### 6.3 ⚠️ 部署验证状态（诚实标注）

- **Windows + WSL2 形态**：已跑通（`git log`：`c169a37 在windows本机完成docker部署并启动`）。
- **独立 Linux 服务器形态**：**尚无证据**。`docs/modules/2.infra.md:9-10` 自己写着"尚未在任何真实 Linux 服务器上执行过"。
- 详见 [`docs/review/02-异常清单.md`](docs/review/02-异常清单.md) E-06 与 [`docs/review/03-待确认清单.md`](docs/review/03-待确认清单.md) Q-09。

详细手册：[`docs/review/consolidated/runbook/环境与部署.md`](docs/review/consolidated/runbook/环境与部署.md) / [`docs/runbook/17.docker-ops.md`](docs/runbook/17.docker-ops.md) / [`docs/runbook/18.docker-install-windows.md`](docs/runbook/18.docker-install-windows.md)。

---

## 7. 角色与权限

> **权威定义在代码里（改权限请改代码）**：
> 角色→权限点 [`app/security/rbac.py::ROLE_PERMISSIONS`](app/security/rbac.py)，
> 角色→敏感等级 [`app/security/policy.py::ROLE_MAX_SENSITIVITY`](app/security/policy.py)，
> 角色→入口 [`app/security/accounts.py::entry_for_role`](app/security/accounts.py)。
> 前端 [`web/src/lib/auth.ts::ROLE_PERMISSIONS`](web/src/lib/auth.ts) 是**副本**，只用于渲染菜单，**不用于授权判断**。

### 7.1 权限点（8 个）

| 权限点 | 含义 |
|---|---|
| `read:own` | 读**自己的**数据（对客路径，永远叠加行级过滤 `WHERE user_id = 本人`） |
| `read:all_orders` | 读**全站**订单（对内分析路径） |
| `read:finance` | 读财务级数据（金额明细、支付方式） |
| `write:refund` | **代客户发起**退款（员工侧；变更 003 起语义收窄为「发起」，不再等于「批准」） |
| `write:ticket` | 建工单 |
| `request:refund` | **申请**退款（变更 003 新增）。客户持有；只能建 `requested`，等员工审批 |
| `approve:refund` | **审批**退款（变更 003 新增）。`requested → approved / rejected` |
| `admin` | 管理台：开账号 / 停用 / 改角色 / 重置口令 ——**有它就进「管理台」入口** |

### 7.2 角色对照表（6 个角色）

| 角色 | 入口 | 权限点 | 数据清晰度上限 |
|---|---|---|---|
| `customer` | 对客 | `read:own` `write:ticket` `request:refund` | 脱敏 |
| `customer_service` | **对内**（变更 003 起） | `read:own` `write:ticket` `write:refund` `approve:refund` | **公开（可对外）** |
| `internal_employee` | 对内 | `read:own` `read:all_orders` `write:refund` `write:ticket` `approve:refund` | 脱敏 |
| `manager` | 对内 | `read:own` `read:all_orders` `write:refund` `write:ticket` `approve:refund` | **内部可见** |
| `finance_auditor` | 对内 | `read:own` `read:all_orders` `read:finance` `write:ticket` `approve:refund` | 财务级 |
| `admin` | 管理台 | 全部 8 个 | 财务级 |

### 7.3 三条容易看错的地方

1. **`manager` 与 `internal_employee` 的权限点完全相同** —— 差别只在**数据清晰度上限**（主管看得到真实姓名，普通员工只看到脱敏值）。「权限点管能做什么，敏感等级管看得多清楚」是两条**正交**的轴。
2. **`customer_service` 的可见度是六个角色里最低的（公开级）**，比客户本人还低。这是**刻意**的：客服是一线、最容易被社工或要求截图，所以连脱敏后的手机号都不给（要联系客户走工单流程）。
3. **`admin` 是「财务级」而不是「无限」** —— 它真正多出来的是**动作权限**（`admin` 权限点），不是「能看到所有人的手机号明文」。

### 7.4 数据清晰度等级（4 级）

| 等级 | 名称 | 典型字段 / 形态 |
|---|---|---|
| 0 | 公开（可对外） | 订单号、状态、时间 |
| 1 | 脱敏 | 手机号 `138****1234`、地址只到区/县级、邮箱 |
| 2 | 内部可见 | 真实姓名 |
| 3 | 财务级 | 金额明细、支付方式 |

**未登记的角色 = 权限点空集、清晰度 -1（什么都看不到）**，即**默认拒绝**。

### 7.5 三条正交的边界（不要混）

| 边界 | 回答的问题 | 唯一真相 |
|---|---|---|
| **行级过滤（D1）** | 能不能看到**这一行** | [`app/db/queries.py`](app/db/queries.py)（对客强制 `WHERE user_id = 本人`） |
| **RBAC** | 这个**角色**能不能做这件事 | [`app/security/rbac.py`](app/security/rbac.py) |
| **脱敏** | 同一条数据，给这个角色**看多清楚** | [`app/security/masking.py`](app/security/masking.py) + `policy.ROLE_MAX_SENSITIVITY` |

> ⚠️ **入口判据 ≠ 查询判据**：`can_enter_internal`（能不能进对内界面 = 有 `read:all_orders` **或** `approve:refund`）
> 与 `has_permission(role, read:all_orders)`（能不能查全站）是**两个函数**，前后端都不许混用。
> 客户把地址栏敲成 `/admin` 一样能打开那个页面，但那一页的每个请求都会被后端拒（越权**按 404 返回**，不是 403 —— 避免订单号存在性探测）。

---

## 8. ⚠️ 已知问题与注意事项（读代码前先看这 6 条）

| # | 事 | 影响 | 详见 |
|---|---|---|---|
| 1 | **技术栈表过时**：`docs/architecture.md` 写 LlamaIndex / LangChain SQLDatabaseToolkit / GitHub Actions，代码里都没有 | 对外介绍/面试会被追问 | `02-异常清单.md` E-01/E-02/E-05 |
| 2 | **多模型自动降级 + 熔断未实现**（problem-solving A3） | 不能对外宣称"主模型故障自动降级" | `02` E-03、`03` Q-01 |
| 3 | **`POST /api/chat/approve` 已删除**，审批搬到 `POST /api/refunds/decision`（需 `approve:refund`） | 旧 runbook 会让人找不存在的接口 | `02` E-04 |
| 4 | **`payment_method` 对所有角色都打码成 `***`**（含财务级） | 订单页"支付方式"列等于没信息 | `issues/008`（**未修**） |
| 5 | **应用数据库角色仍是 SUPERUSER** | 凭据泄露则不受权限约束 | `docs/runbook/4.security-runbook.md:402-428` |
| 6 | **CI/CD 未落地**，测试数字（`1846 passed` 等）全是历史快照 | 完成度不可按 README 旧数字判断 | `02` E-05、`03` Q-02 |

另外两条最有教学价值的坑（不修，但值得知道）：
- `issues/007`：一条死 WebSocket 连接 → 无让出点忙等 → **锁死整个事件循环**。
- `issues/009`：语义缓存命中后 `docs` 归零 → 护栏把**真答案**判成编造。**「有出处」≠「答出来了」**。

---

## 9. 项目进度（按代码核实，非历史快照）

| 模块 | 状态 | 代码 | 验证方式 |
|---|---|---|---|
| 1 config | ✅ | `app/config/` | `uv run pytest -q tests/test_config.py` |
| 2 infra（开发依赖栈） | ✅ | `infra/docker-compose.dev.yml` | `docs/runbook/2.infra-runbook.md` |
| 3 db | ✅ 10 张表 / 7 迁移 / 种子 / 行级过滤 / 连接池 | `app/db/`、`alembic/`、`scripts/` | `alembic upgrade head` → `scripts.seed` → `db_check --check` |
| 4–8 security / llm / tools / agents / graph | ✅ | `app/security/`、`app/llm/`、`app/tools/`、`app/agents/`、`app/graph/` | `uv run pytest -q -k "graph or agents or tools or llm or security"` |
| 9 api | ✅ 首个端到端切片（HTTP → 图 → Text-to-SQL → 回答；WS 流式、限流） | `app/api/`、`app/main.py` | `uv run pytest -q tests/api`、`scripts/api_demo.py` |
| 10–15 rag / observability / queue / cache / eval / mcp | ✅ | 对应 `app/*` 目录 | 见各 runbook 状态表 |
| 16 web | ✅ 三入口 + 流式 + 溯源 + 图表 | `web/` | `cd web && npm test && npm run build` |
| 变更 001 账号登录 + 管理台 | ✅ | `app/security/accounts.py`、`app/api/{auth,admin}.py` | `scripts/evidence_accounts` |
| 变更 002 前端改版（浅色 + 侧边栏 + 自动探活） | ✅ | `web/src/components/`、`web/src/lib/health.ts` | `scripts/evidence_paths` |
| 变更 003 退款闭环 + 入口贯通 | ✅ | `app/api/{orders,refunds}.py`、`app/tools/refund.py`、`app/agents/refund.py` | `scripts/evidence_refunds` |
| issue 004–007 修复 | ✅ | 见 `issues/` 各条 | 见各条"验证"节 |
| issue 008（payment_method 打码） | ❌ **未修** | `app/security/masking.py:136` | — |
| 生产部署（Linux 服务器形态） | ⚠️ **未验证** | `docker-compose.yml` | 见 §6.3 |
| CI/CD | ❌ **未落地** | 无 | — |

> ⚠️ 本表的"验证方式"列是**怎么验**，不是"已经验过"。要确认真实基线，跑一次 `uv run pytest -q` 与 `cd web && npm test`（见 [`docs/review/03-待确认清单.md`](docs/review/03-待确认清单.md) Q-02）。

---

## 10. 编号约定

`docs/modules/` 下的模块文档按建议开发顺序编号（`1.config.md` … `16.web.md`）；`docs/runbook/` 复用同一套编号，即**第 N 步的模块文档配 `N.*` 手册**（例：`3.db.md` ↔ `3.db-runbook.md`）。

> ⚠️ 两个已知例外：
> 1. `4.uv-pytest-basics.md` **不绑定第 4 步模块**，是任何一步都要用的工具命令手册。
> 2. `docs/modules/README.md` 的"建议开发顺序"表与实际文件编号**整体错开 1–2**（见 [`docs/review/02-异常清单.md`](docs/review/02-异常清单.md) E-13）——**以文件名编号为准**。

---

## 11. 贡献约定（现状）

- **分支**：实际只有 `main`（文档里写的 `main / develop / feature-*` 未落地）。
- **提交信息**：中文短句，非 Conventional Commits。
- **pre-commit / ruff / black / mypy**：未配置。
- **加功能请先开变更单**：按 [`docs/changes/README.md`](docs/changes/README.md) 的五步流程（**先定稿文档，再写代码**）。
- **改权限请改代码**（`app/security/rbac.py`），不要只改前端副本。
- **改语料等于改答案**（`docs/kb/`），改完要重跑 `scripts.load_knowledge_base`。

---

<sub>
📚 本次文档整理成果在 [`docs/review/`](docs/review/SUMMARY.md)
</sub>
