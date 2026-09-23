# 电商企业智能助手 — 技术框架与模块设计

> 项目定位：一个接入企业数据库（用户/订单/商品数据）与内部知识库的多 Agent 协作系统，
> 同时服务「客户问题处理」与「内部员工需求」，支持 Docker 容器化部署到 Linux 服务器。

---

## 1. 业务场景

两个入口，两条业务线：

| 入口 | 使用者 | 典型需求 |
|---|---|---|
| 对外（Web/IM） | 客户 | 查询订单状态、退换货咨询、产品咨询、物流查询 |
| 对内（内部系统） | 员工 | 销售数据分析、库存查询、客户信息查询、生成报表 |

---

## 2. 技术栈（确认版）

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 / Web | Python 3.12 + FastAPI | 异步、类型友好、生态成熟（开发机实测 3.12.9，`pyproject.toml` 钉 `>=3.12`） |
| 依赖 / 虚拟环境 | uv | 不用 `python -m venv` + `pip`：`uv sync` 建 `.venv` + 生成 `uv.lock`，`uv run` 免激活执行（实测 `uv run pytest -q` → 54 passed，见 `docs/2.infra-runbook.md` §1.1） |
| Agent 编排 | LangGraph | 有状态图、checkpoint、human-in-the-loop |
| LLM 接入 | LiteLLM | 多模型路由、自动降级、统一计费 |
| Text-to-SQL | LangChain SQLDatabaseToolkit + 自研安全层 | schema 感知 + few-shot + 越权/只读校验 |
| RAG 框架 | LlamaIndex | chunking、检索链、混合检索 + rerank |
| 向量库 | pgvector | 向量存储 + 相似度检索（ANN） |
| 业务库 | PostgreSQL | 用户/订单/商品 + 会话状态 |
| 缓存/会话 | Redis | 缓存 + 语义缓存 + 会话状态 |
| 异步任务 | Arq | 长任务、重试、延迟任务、死信队列 |
| 可观测 | OpenTelemetry + Langfuse + Prometheus + Grafana | 指标、链路追踪、token/成本 |
| 前端 | Next.js | 演示对客/对内两个入口 |
| 部署 | Docker Compose | 小企业够用 |
| CI/CD | GitHub Actions | lint/test → 镜像 → 部署 Linux |
| 开发环境 | VMware-Windows + VMware-Linux；依赖管理 `uv` | 依赖服务（postgres/redis/prometheus/grafana）起在 Linux 那台，代码在 Windows 上用 `uv run` 跑（Windows 无 Docker，见 §9） |

---

## 3. 后端模块划分（含全部补充模块）

```
app/
├─ api/            # FastAPI 路由、WebSocket 流式、限流中间件、JWT 鉴权
├─ graph/          # LangGraph 图定义：router → data/rag/analysis → synthesis；guardrail 全程横切把关
├─ agents/
│  ├─ router.py        # 意图识别 + 客户/员工分流 + 权限上下文注入
│  ├─ data.py          # Text-to-SQL：schema 感知、few-shot、执行、校验重试
│  ├─ rag.py           # 知识库检索：混合检索 + rerank + 溯源
│  ├─ analysis.py      # 对内数据洞察/聚合解读
│  ├─ synthesis.py     # 按角色合成输出（对客/对内语气）
│  └─ guardrail.py     # SQL 安全、越权拦截、脱敏、注入防护
├─ tools/          # 工具：SQL 执行器、知识库检索、图表生成、工单操作
├─ db/             # SQLAlchemy 模型、连接池、读写路径分离、区分权限
├─ rag/            # 文档解析、分块、embedding、增量索引
├─ security/       # 脱敏、SQL 白名单、限流、鉴权(JWT)
├─ queue/          # 异步任务、重试、延迟任务、死信队列
├─ cache/          # 语义缓存、结果缓存、缓存失效策略
├─ mcp/            # MCP Server：以统一协议对外暴露 tools/ 的能力（数据库/知识库/工具）
├─ observability/  # OTEL 埋点、Prometheus 指标、Langfuse 回调
└─ config/         # 多环境配置、密钥管理(.env)
eval/
```

### 补充模块（全部纳入）

1. **异步任务 + 断点重放层**（`queue/` + LangGraph checkpointer）：Agent 长任务走队列，节点状态持久化到 checkpoint，失败从断点重放。
2. **可观测性三件套**（`observability/`）：OTEL + Langfuse + Prometheus/Grafana。
3. **多模型路由**（LiteLLM 接入层）：模型故障/限流自动降级，主备模型切换。
4. **语义缓存**（`cache/`）：重复问题命中缓存，降低 token 成本。
5. **评测体系**（`eval/`）：Ragas + golden dataset + 单/多 Agent 效果对比。
6. **MCP**（`mcp/`）：以 MCP 协议统一暴露 `tools/` 的能力，与内部直接调用并存。
7. **增量索引/数据同步**（`rag/`）：知识库、订单数据变更实时反映到检索。

---

## 4. 多 Agent 协作设计

```
                    ┌─────────────────────────────────────────────┐
  客户(Web/IM)  ──► │              Router / 意图路由 Agent            │
                    │  判断：客户 or 员工？订单/售后/产品/数据/政策？    │
  员工(内部系统) ──► └───────────────┬─────────────────────────────┘
                                     │ 路由到不同工作流
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
        ┌───────────┐        ┌───────────────┐      ┌──────────────┐
        │ Data Agent │        │  RAG Agent    │      │ Analysis Agent│
        │ Text-to-SQL│        │  知识库检索     │      │ 数据解读/洞察  │
        │ 查订单/用户 │        │ 政策/产品/FAQ  │      │ (对内报表)    │
        └─────┬─────┘        └───────┬───────┘      └──────┬───────┘
              └──────────────────────┼──────────────────────┘
                                     ▼
                           ┌──────────────────┐
                           │  Synthesis Agent │  ← 按角色(对客/对内)合成最终回答
                           └────────┬─────────┘
                                    │
                        ┌───────────▼───────────┐
                        │  Guardrail / 安全 Agent │  ← 全程把关(权限/脱敏/只读/合规)
                        └───────────────────────┘
    数据层：  PostgreSQL(用户/订单/商品/库存)   +   向量库(内部知识库)
```

| Agent | 职责 | 异构能力/工具 |
|---|---|---|
| Router Agent | 识别「客户 vs 员工」+ 意图分类，决定走哪条工作流 | 意图分类、权限上下文注入 |
| Data Agent | NL→SQL，查订单/用户/商品，执行并校验结果 | 数据库 schema、SQL 执行、报错重试 |
| RAG Agent | 检索内部知识库（退换货政策、产品手册、操作流程、FAQ） | 混合检索 + rerank |
| Analysis Agent | 对内需求：聚合统计、趋势解读、生成结论（可选图表） | 数据分析、可视化 |
| Synthesis Agent | 按角色合成最终回答（对客礼貌简短 / 对内详细专业） | 角色化输出模板 |
| Guardrail Agent | SQL 安全校验、越权拦截、敏感字段脱敏、输出合规审查 | 只读白名单、脱敏规则、注入防护 |

---

## 5. 数据库 Schema（电商模拟数据）

```sql
users           (user_id, name, phone, email, address, level, created_at)
products        (product_id, name, category, price, stock, description)
orders          (order_id, user_id, status, total_amount, payment_method, created_at)
order_items     (order_item_id, order_id, product_id, quantity, unit_price)
refunds         (refund_id, order_id, user_id, amount, status, reason, created_at)
stock_movements (movement_id, product_id, change_qty, type, operator_id, created_at) -- 库存流水/变更审计
tickets         (ticket_id, user_id, type, status, assignee_id, created_at)   -- 工单
employees       (emp_id, name, role, dept, level)                             -- 内部员工
```

---

## 6. 安全设计

1. **区分权限**：区分「对客路径=行级过滤（row-level filtering）」与「对内路径=基于角色的分级权限（RBAC）」。
2. **读写路径分离**：「读路径」（NL→SQL 只读白名单）与「写路径」（工单/退款走预定义、审计的 service API，不经 LLM 动态生成 SQL）分离。SQL 执行前校验——禁止 `DROP/DELETE`、禁止无 `WHERE` 的全表扫描、限制返回行数。
3. **敏感字段脱敏**：手机号 `138****1234`、地址只显示区级、员工分级权限。
4. **Prompt 注入防护**：Guardrail Agent 识别"忽略之前的指令"等注入，双向校验输入输出。

---

## 7. 可观测性

- **指标**：Prometheus 采集（请求量、P99 延迟、各 Agent 节点耗时、SQL 耗时、队列长度、错误率）。
- **链路**：OpenTelemetry 在"每个 Agent 节点 / 工具调用 / LLM 调用 / 向量检索"埋点。
- **成本/质量**：Langfuse 记录 token 消耗、每步耗时、最终答案质量。
- **可视化**：Grafana 面板（黄金指标 + 分 Agent 耗时 + 队列深度）。

---

## 8. 可伸缩性设计（水平扩容 + 会话外置）

- **服务无状态化**：会话/状态外置到 Redis + PostgreSQL，API 层可水平扩容多副本。
- **按层独立扩缩**：API 网关、各 Agent 节点、队列 worker 按负载独立扩容；LLM 调用天然可并发横向扩展。
- **有状态层扩容**：Redis 主从/集群、PostgreSQL 读写分离 + 只读副本；pgvector 按数据量分库/分区。
- **背压与削峰**：突发流量走 Redis 队列削峰，队列积压触发 worker 自动扩容。

---

## 9. 开发与部署流程（Windows → Linux）

- **开发期**：VMware-Windows + VMware-Linux，两台机器分工固定 —— **依赖服务（PostgreSQL、Redis、pgvector、Prometheus、Grafana）在 VMware-Linux 那台用 `docker-compose.dev.yml` 起，代码在 VMware-Windows 上用 `uv run` 跑（虚拟环境由 uv 管理，见下方「依赖 / 虚拟环境」），通过网络连 Linux 的 IP**（Windows 客户机没有 Docker）。文件实际位置是 `infra/docker-compose.dev.yml`，启动命令与端口/防火墙要求见 `docs/modules/2.infra.md` §2 与 §3.1「开发拓扑（跨主机）」。
- **一致性**：`docker-compose.yml` 一份，用 `.env` 区分开发/生产环境。
- **交付**：GitHub Actions 在 Linux runner 构建镜像 → push registry → 服务器拉取后 Compose 起。
- **密钥**：API Key 等走 `.env` / 环境变量 / Secret，不进 Git。

---

## 10. Git 管理

- monorepo 结构：`app/`、`web/`（Next.js 前端）、`infra/`（docker yaml，开发依赖栈为 `infra/docker-compose.dev.yml`；生产 compose 属第 19 步）、`docs/`、`scripts/`（`seed.py` 种子数据、`db_check.py` 现场核查）、`tests/`（第 3 步的 `tests/test_db_*.py`）、`eval/`；**数据库迁移在 `alembic/`（第 3 步已落地，`env.py` 从 `Settings` 注入连接串、不把密码写进 `alembic.ini`）**。
- Conventional Commits + pre-commit（ruff/black/mypy）。
- 分支：main / develop / feature-*。
- CI：lint → test → 构建镜像 → 部署。
