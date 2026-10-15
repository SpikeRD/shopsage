# ShopSage — 电商企业智能助手

接入企业数据库（用户/订单/商品数据）与内部知识库的多 Agent 协作系统，同时服务「客户问题处理」与「内部员工需求」，支持 Docker 容器化部署到 Linux 服务器。

## 文档导航

| 文档 | 内容 | 建议阅读时机 |
|---|---|---|
| [docs/architecture.md](docs/architecture.md) | 技术框架、模块划分、多 Agent 协作、数据库 Schema、安全/可观测/可伸缩设计、开发部署流程 | 先读，建立整体认知 |
| [docs/problem-solving.md](docs/problem-solving.md) | 痛点 → 方案 → 技术点 → 指标的工程问题清单（含落地优先级） | 后读，用于落地与面试/简历讲述 |
| [docs/runbook/1.config-guide.md](docs/runbook/1.config-guide.md) | **配置实操手册**：配置从哪取值、怎么改（`.env` / 环境变量）、Docker 怎么传值、`test_config.py` 怎么手动测 | 第一次配环境时读，排查「配置不生效」时查 |
| [docs/runbook/2.infra-runbook.md](docs/runbook/2.infra-runbook.md) | **开发依赖启动手册**：跨主机（Windows 开发机 + Linux 依赖机）起 postgres/pgvector/redis/prometheus/grafana、连通性验证、故障速查、证据回填 | 起依赖栈时照抄；「连不上库」时分诊 |
| [docs/runbook/3.db-runbook.md](docs/runbook/3.db-runbook.md) | **数据库实操手册**：建库/扩展校验、SQLAlchemy 连接与连接池、Alembic 迁移、种子数据、读写引擎切换与行级过滤验证 | 做第 3 步（db）时照抄；查连接数/SQL 归属时用 |
| [docs/modules/](docs/modules/README.md) | 每个模块的开发 + 测试文档（8 段模板，文件名按建议开发顺序编号），含建议开发顺序与 problem-solving 映射 | 开发每个模块前/后，边做边查 |
| [docs/runbook/4.uv-pytest-basics.md](docs/runbook/4.uv-pytest-basics.md) | **uv / pytest 基础命令手册**：uv 管环境依赖（`sync`/`run`/`add`/锁文件）、pytest 选用例与读输出（`-k`/`-m`/退出码/fixture）、本项目 `db` 标记与实测口径、故障速查 | 第一次拿到仓库时读；命令报错（`exit code 5`、缓存权限）时查 |
| [docs/runbook/](docs/runbook/) | 实操手册目录（编号与 `docs/modules/` 的第 N 步对齐，如 `1.config-guide.md`、`2.infra-runbook.md`、`3.db-runbook.md`） | 要「怎么做、怎么确认做对了」时读 |
| [docs/环境迁移验收.md](docs/环境迁移验收.md) | **环境迁移验收清单**：换机器后按步验收（工具链 / uv 缓存 / 依赖栈 / 数据库 / 后端 / 跨源 / 前端 / 测试套件 / 知识库），含逐步实测证据、P0 问题与可直接执行的修复命令 | **迁移到新机器后第一份要跑的文档** |
| [issues/](issues/README.md) | **问题台账**：实测撞到但当场不修/修不完的问题（现象 / 根因 / **判据** / 处理状态），一条一个文件 | 遇到「这算不算坏了」时先查这里有没有既定判据 |
| [docs/changes/](docs/changes/README.md) | **需求变更台账**：「原来是什么 → 改成什么 → 为什么 → 怎么做 → 怎么验收」，一条变更一个文件；**先定稿文档，再写代码** | 想加/改功能时，先在这里开一条变更单 |

> 📌 **编号约定**：`docs/modules/` 下的模块文档按 [`docs/modules/README.md`](docs/modules/README.md) 的**建议开发顺序**编号
> （`1.config.md` … `16.web.md`，README 自身不编号）；`docs/runbook/` 下的手册复用同一套编号，即
> **第 N 步的模块文档配 `N.*` 手册**（例：`3.db.md` ↔ `3.db-runbook.md`）。
>
> ⚠️ 一个例外：`4.uv-pytest-basics.md` **不绑定第 4 步模块**，它是任何一步都要用的**工具命令手册**
> （uv/pytest 底座），放在 `runbook/` 下只是因为同属「怎么做」类文档。

## 建议阅读顺序

1. **architecture.md**：了解技术栈选型与模块边界，明确「对客/对内」两条业务线。
2. **problem-solving.md**：按 P0 → P1 → P2 优先级，把架构中的设计落到可验证的工程问题，形成「问题 → 方案 → 证据」闭环。
3. **docs/modules/Number.xxx.md**：开始写某个模块前，读它对应的「开发 + 测试」文档；开发完按「验收标准」自检并回填证据。
4. **docs/runbook/**：动到环境/库/迁移这类「必须一步步确认」的操作时，照抄同编号的实操手册（`3.db-runbook.md` 等），并把实测输出回填到模块文档第 8 节。

## 当前进度

| 模块 | 状态 | 代码 | 验证方式 |
|---|---|---|---|
| 1 config | ✅ 完成 | `app/config/` | `uv run pytest -q tests/test_config.py` |
| 2 infra（仅开发依赖） | ✅ 依赖栈在 Linux VM 上跑通 | `infra/docker-compose.dev.yml` | `docs/runbook/2.infra-runbook.md` |
| 3 **db** | ✅ **完成**（8 张表 / 迁移 / 种子 / 行级过滤 / 连接池） | `app/db/`、`alembic/`、`scripts/` | `uv run alembic upgrade head` → `uv run python -m scripts.seed` → `uv run python -m scripts.db_check --check` → `uv run pytest -q`（**97 passed, 1 skipped**） |
| 4–8 security / llm / tools / agents / graph | ✅ 完成（各步的实操手册见 `docs/runbook/`，编号与模块一致） | `app/security/`、`app/llm/`、`app/tools/`、`app/agents/`、`app/graph/` | `uv run pytest -q -k "graph or agents or tools or llm or security"`；第 8 步口径：`79 passed, 1 skipped`（图）+ 全量 `730 passed` |
| 9 **api** | ✅ **完成**（**首个端到端切片**：HTTP → 图 → Text-to-SQL → 回答；含 WebSocket 流式、限流、审批通道） | `app/api/`、`app/main.py`、`scripts/api_demo.py` | `uv run pytest -q tests/api`（**138 passed**）；`uv run python -m scripts.api_demo`（七个场景，首帧 **10~13ms**）；全量 **870 passed** |
| 10–15 rag / observability / queue / cache / eval / mcp | ✅ 完成（各步的实操手册见 `docs/runbook/`，编号与模块一致） | `app/rag/`、`app/observability/`、`app/queue/`、`app/cache/`、`app/eval/`、`app/mcp/` | 见各步 runbook 的状态表；第 15 步口径：全量 **1613 passed, 50 skipped, 13 deselected, 6 errors** |
| 16 **web** | ✅ **完成**（**演示层**：对客流式 + 溯源可视化 + 人机审批 / 对内报表 + 图表 + SQL 回显 / 按角色分流） | `web/`（Next 15 + TS + Tailwind + ECharts）、`scripts/dev_token.py`、`scripts/check_web_cors.py` | `cd web; npm test`（**119 passed**）；`npm run build`（**✓ 4.3s**）；`node tests/run-live.mjs`（**10 passed**） |
| 17 **变更 001：账号登录 + 管理台** | ✅ 完成（**真登录**：账号+口令、刷新续期、三档入口、管理员开账号） | `app/security/accounts.py`、`app/api/auth.py`、`app/api/admin.py`、`web/src/app/admin/`、`web/src/lib/session.ts` | `uv run pytest -q tests/security tests/api`；`uv run python -m scripts.seed`；`cd web; npm test`；全量 **1745 passed, 50 skipped** |
| 18 **变更 002：前端改版** | ✅ 完成（**浅色护眼** + 登录/主界面分离 + **左侧边栏**按权限分流 + **自动探活** + 管理台表格修复） | `web/src/app/(auth)/`、`web/src/app/(app)/`、`web/src/components/SideNav.tsx`、`HealthBanner.tsx`、`web/src/lib/health.ts` | `cd web; npm test`（**150 passed**）；`npm run build`；`npm run typecheck`；`uv run python -m scripts.evidence_accounts`；`uv run python -m scripts.evidence_paths` |

> 📌 **变更 002（前端改版）的用法**：
> ① 后端照旧 `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`；
> ② 前端 `cd web; npm run dev` → <http://127.0.0.1:3000>；
> ③ 打开 `/login` 用账号+口令登录，登录后落在**左侧边栏**外壳里：
> 侧边栏按你的权限显示入口（客户只看得到「对客问答」，员工多一个「对内查询」，管理员三个都有），
> 底部固定显示**个人信息 + 权限状态**（权限点来自 `GET /api/auth/me`，失败时回退本地并**明确标注是回退值**）。
> ④ **每次打开或刷新页面都会自动探活一次**（`GET /health`）；后端没连上时页面顶部出现异常横幅，
> 含后端地址与分诊步骤，另有「重新探活」按钮。
> ⚠️ **登录页不再有「开发模式粘贴令牌」**（变更 002 按「只管登录账户」删掉了）。
> 排障要手粘令牌时，改在 DevTools 里执行：
> `localStorage.setItem("shopsage.token.customer", "<令牌>")`（key 见 `web/src/lib/auth.ts::tokenKey`）。
> 变更说明、决策记录与实测证据见 [`docs/changes/002-light-ui-sidebar-and-autoprobe.md`](docs/changes/002-light-ui-sidebar-and-autoprobe.md)。

> 📌 **变更 001（账号登录 + 管理台）的用法**：
> ① 起依赖栈与后端；② 灌演示账号 `uv run python -m scripts.seed`（口令写在 `scripts/seed.py`
> 的 `DEMO_ACCOUNTS` 里，**刻意不打印到终端**）；③ 打开 `/login`，用这三个之一登录：
> `admin` / `13800000002` / `employee` → 分别落在 `/admin`、`/`、`/internal`。
> 管理员可在 `/admin` 开账号；停用/改角色/重置口令会**立刻作废该账号所有在途令牌**。
> 变更说明、决策记录与实测证据见 [`docs/changes/001-account-login-and-admin-console.md`](docs/changes/001-account-login-and-admin-console.md)。

> ⚠️ **变更 001 的升级影响**：旧令牌（没有 `aid`/`ver` 声明）一律被拒，
> 且 `thread_id` 形状从 `u{user_id}:` 改为 `c{user_id}:` / `e{emp_id}:`
> （员工与客户不再共用会话桶）—— **升级后需要重新登录，旧会话作废**。

> 📌 第 9 步的启动方式：`uv run uvicorn app.main:app --reload --port 8000`
> （`/docs` 看 OpenAPI、`/health` 看依赖状态、`WS /api/chat/stream` 走流式；
> 逐步操作与排障见 [`docs/runbook/9.api-runbook.md`](docs/runbook/9.api-runbook.md)）。

> 📌 第 16 步的启动方式（**两个进程 + 一次自检**）：
> ① 后端 `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`；
> ② 前端 `cd web; npm install --ignore-scripts; npm run dev` → <http://127.0.0.1:3000>；
> ③ 登录：**直接用账号 + 口令**（变更 001 起有真登录接口了，见上面那条）。
> 排障时仍可用 `uv run python -m scripts.dev_token --account admin` 签一个令牌，
> 在 `/login` 的「开发模式」里粘贴（它**没有刷新令牌**，30 分钟后要重来）。
> ⚠️ 页面报 `Failed to fetch` 时先跑 `uv run python -m scripts.check_web_cors`
> （**别去重签令牌** —— 没有状态码的 fetch 失败不是令牌问题，是「进程没起」或「跨源被拦」）；
> ⚠️ **打开页面请用 `http://127.0.0.1:3000`（或 `http://localhost:3000`）**：
> `next dev` 打印的 `Network: http://<网卡IP>:3000` **默认不在跨源白名单里**，照它打开会
> 「页面能开、WebSocket 也能连，但一直显示连不上、发消息没反应」（症状极像后端坏了）。
> 要按实际打开的地址核对，加 `--web`：`... check_web_cors --web http://<该地址>:3000`；
> 故障记录与修法见 [`docs/runbook/16-web-runbook.md`](docs/runbook/16-web-runbook.md) §10.1 坑 18。
> 若「HTTP 通但对客页说 WebSocket 连接失败」，多半是 `NEXT_PUBLIC_API_BASE` 指到了前端自己
> （它是**构建期**变量，改了要重启 `npm run dev`）。
> 逐步操作、坑与分诊见 [`docs/runbook/16-web-runbook.md`](docs/runbook/16-web-runbook.md)。

常用命令（都在**仓库根**执行；本机无 Docker，数据库在 VMware-Linux 上）：

```bash
uv run pytest -q                              # 全量测试（连不上库时 db 用例自动 skip）
uv run pytest -q -m "not db"                  # 只跑不需要数据库的用例
uv run alembic upgrade head                   # 建表 / 升级（连接串从 .env 读，见 alembic/README）
uv run python -m scripts.seed                 # 灌演示数据（幂等：重复执行结果一致）
uv run python -m scripts.db_check             # 现场核查：表、行数、约束统计
uv run python -m scripts.db_check --check     # 验收断言：缺表/行数不足则退出码 1
uv run python -m scripts.load_knowledge_base  # 灌知识库语料（跑完全量回归后必须重灌，见 16 runbook §10.1 坑 6）
uv run python -m scripts.dev_token --account admin   # 签一个能粘进 Web 登录页的演示令牌（按账号签，变更 001）
uv run python -m scripts.dev_token --role admin      # 也可以按角色取「第一个启用账号」来签
uv run python -m scripts.evidence_accounts           # 变更 002 取证：账号与口令确实在库里（Argon2id，非明文）
uv run python -m scripts.evidence_paths              # 变更 002 取证：三条主路径回归（需后端在跑）
```

前端（都在 `web/` 下执行；需要 Node ≥ 22，本机实测 v24.16.0）：

```bash
npm install --ignore-scripts   # ⚠️ 必须带 --ignore-scripts（见 16 runbook §10.1 坑 1）
npm run dev                    # 开发模式 → http://127.0.0.1:3000
npm test                       # 离线用例（150 条，不连后端；变更 002 起 +31）
npm run typecheck              # 类型检查
npm run build                  # 构建通过（验收第 1 条）
node tests/run-live.mjs        # 真链路用例（10 条，需要后端在跑）
npx tsx scripts/probe-evidence.ts      # 变更 002 取证：探活判据（改 NEXT_PUBLIC_API_BASE 可对比断连/正常）
npx tsx scripts/sidebar-evidence.ts    # 变更 002 取证：三角色的侧边栏入口分流（需后端在跑 + TOK_* 环境变量）
```
