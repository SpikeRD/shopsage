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

> 📌 第 9 步的启动方式：`uv run uvicorn app.main:app --reload --port 8000`
> （`/docs` 看 OpenAPI、`/health` 看依赖状态、`WS /api/chat/stream` 走流式；
> 逐步操作与排障见 [`docs/runbook/9.api-runbook.md`](docs/runbook/9.api-runbook.md)）。

常用命令（都在**仓库根**执行；本机无 Docker，数据库在 VMware-Linux 上）：

```bash
uv run pytest -q                              # 全量测试（连不上库时 db 用例自动 skip）
uv run pytest -q -m "not db"                  # 只跑不需要数据库的用例
uv run alembic upgrade head                   # 建表 / 升级（连接串从 .env 读，见 alembic/README）
uv run python -m scripts.seed                 # 灌演示数据（幂等：重复执行结果一致）
uv run python -m scripts.db_check             # 现场核查：表、行数、约束统计
uv run python -m scripts.db_check --check     # 验收断言：缺表/行数不足则退出码 1
```
