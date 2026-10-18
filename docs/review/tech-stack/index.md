# 技术栈总览与学习优先级

> **以代码为准**：本页只列 `pyproject.toml` / `web/package.json` / `docker-compose.yml` 里真实存在的东西。
> `docs/architecture.md:21-37` 里写的 LlamaIndex、LangChain SQLDatabaseToolkit、GitHub Actions 在代码中**不存在**（见 `../02-异常清单.md` E-01/E-02/E-05）。

## 1. 总览表

| 领域 | 技术 | 版本（声明下限） | 本项目用它解决什么 | 分册 |
|---|---|---|---|---|
| 语言 | Python | `>=3.12`（实测 3.12.9） | 后端全部 | — |
| Web 框架 | **FastAPI** | `>=0.143.0` | 路由 / 依赖注入 / WebSocket / 自动 OpenAPI | `fastapi.md` |
| ASGI 服务器 | uvicorn[standard] | `>=0.54.0` | 起服务；`--host 0.0.0.0` 供浏览器直连 | `fastapi.md` |
| Agent 编排 | **LangGraph** | `>=1.2.14` | 有状态图 / 条件边 / checkpointer / 断点重放 | `langgraph.md` |
| LLM 接入 | **LiteLLM** | `>=1.50` | 一家接口打多家模型（当前 deepseek + dashscope） | `litellm.md` |
| 重试 | tenacity | `>=9.1.4` | 指数退避 + jitter；只重试可恢复错误 | `litellm.md` |
| 业务库 | **PostgreSQL** + **SQLAlchemy 2** + psycopg3 | `sqlalchemy>=2.0`、`psycopg[binary]>=3.2` | 10 张表 / 连接池 / 读写双引擎 | `postgres-sqlalchemy.md` |
| 向量 | **pgvector**（扩展） | 镜像 `pgvector/pgvector:pg16`；表 `vector(1024)` | 语义检索 + 语义缓存的相似度 | `pgvector-rag.md` |
| 迁移 | Alembic | `>=1.20.0` | 7 个单链迁移 | `postgres-sqlalchemy.md` |
| 缓存/队列 | **Redis** + **arq** | `arq>=0.26` | 限流桶 / 语义缓存快路径 / 异步任务 / 死信 | `redis-arq.md` |
| 鉴权 | **PyJWT**(HS256) + **Argon2id** | `pyjwt>=2.8`、`argon2-cffi>=25.1.0` | 令牌与口令 | `jwt-argon2.md` |
| SQL 解析 | **sqlglot** | `>=25.0` | SQL 守卫（AST 而非正则）+ 行级过滤形状校验 | `sqlglot.md` |
| 可观测 | OpenTelemetry SDK + OTLP + prometheus-client + Langfuse | 见 `pyproject.toml:29,36-38` | span / 指标 / LLM 记录 | `otel-prometheus.md` |
| 协议 | **MCP** SDK 2.x | `mcp>=1.0` | 把工具暴露给外部客户端 | `mcp.md` |
| 前端框架 | **Next.js** | `15.5.27` | App Router + standalone 产物 | `nextjs.md` |
| UI/样式 | React 19 + Tailwind 4 + ECharts 5 | — | 三档入口界面 + 图表 | `nextjs.md` |
| 容器 | **Docker Compose** | compose v2 | 开发栈 + 生产栈 | —（见 `consolidated/modules/02-infra.md`） |

**刻意没有引入的**（代码里无依赖，文档却有说法）：

| 技术 | 状态 | 替代方案 | 证据 |
|---|---|---|---|
| LlamaIndex | ❌ 未使用 | 自研解析/分块/检索 | `uv.lock` 零命中 |
| LangChain SQLDatabaseToolkit | ❌ 未使用 | sqlglot + 自研 prompt | `pyproject.toml` 无 langchain |
| Ragas | ❌ 未安装 | 自研三指标（名字对齐） | `app/eval/metrics.py:19-37` |
| GPTCache | ❌ 未使用 | 自研语义缓存 | `app/cache/` |
| Debezium / CDC | ❌ 未使用 | 灌库推进 `corpus_revision` | `issues/010` |
| GitHub Actions | ❌ 未配置 | 手抄 runbook | 无 `.github/` |

## 2. 学习优先级（针对"RPA 工程师转 agent 开发"）

排序依据三条：**① 这个技术在项目里被依赖得多深 ② 它与你已有技能的差异有多大 ③ 不懂它会造成什么级别的误判**。

### P0 · 必须先懂，否则会看错代码

| 序 | 技术 | 为什么排这么前 | 建议投入 | 分册 |
|---|---|---|---|---|
| 1 | **LangGraph** | 整个后端围绕它组织；`durability`、条件边、checkpointer 断点重放这三件事不懂，图的代码读不懂也改不对 | 6–8 h | `langgraph.md` |
| 2 | **asyncio / 事件循环**（属 FastAPI 篇） | `issues/007` 那个"一条死连接锁死全站"的根因是**没有任何 await 让出点**；这是 RPA 里没有的一类故障 | 3–4 h | `fastapi.md` |
| 3 | **sqlglot（AST 校验）** | 安全层的核心；用正则理解它必然读错（`SELECT 1; DROP TABLE` 的坑就是这么来的） | 3 h | `sqlglot.md` |

### P1 · 决定了能否独立加功能

| 序 | 技术 | 为什么 | 建议投入 | 分册 |
|---|---|---|---|---|
| 4 | **FastAPI 依赖注入 + 中间件顺序** | 加一个路由要动三处（路由/scope/限流）；中间件顺序错会让预检被吃掉（`issues/005`） | 4 h | `fastapi.md` |
| 5 | **pgvector + 混合检索（BM25）** | RAG 与语义缓存都压在这上面；阈值标定不重做就会"看起来正常但全错" | 5 h | `pgvector-rag.md` |
| 6 | **Redis + arq** | 限流、缓存快路径、异步任务三条链路共用 Redis；arq 的幂等键与死信是运维重点 | 3 h | `redis-arq.md` |
| 7 | **LiteLLM + 重试语义** | 4xx 不重试、降级模型必须独立密钥、embedding 换后端要重标定 | 2–3 h | `litellm.md` |

### P2 · 需要时再深入

| 序 | 技术 | 什么时候需要 | 建议投入 | 分册 |
|---|---|---|---|---|
| 8 | **OTEL + Prometheus** | 要回答"慢在哪一步"或接告警时 | 3 h | `otel-prometheus.md` |
| 9 | **JWT + Argon2id** | 要改鉴权、排查令牌问题时 | 2 h | `jwt-argon2.md` |
| 10 | **Next.js App Router** | 要改前端或排查 WS 连接问题时 | 4 h | `nextjs.md` |
| 11 | **MCP** | 要给外部 IDE 接工具时 | 2 h | `mcp.md` |
| 12 | **SQLAlchemy 2 风格 / Alembic** | 要加表或改模型时 | 3 h | `postgres-sqlalchemy.md` |

### 你的既有优势可以直接迁移的部分

| RPA 里的经验 | 本项目里的对应物 |
|---|---|
| 流程编排（节点/连线） | LangGraph 的节点与条件边 |
| 异常处理与重试 | `Retryable/Permanent` 两类错误 + tenacity |
| 选择器/固定脚本 vs 动态生成 | **读写路径分离**：写操作绝不交给 LLM 生成 SQL |
| 操作日志与留痕 | `app/tools/audit.py` 的工具审计 + `issues/` 台账 |
| 定时任务/队列 | arq + 死信表 |

### 你的经验容易踩的坑（提前标注）

1. **"重试一切"的直觉**在这会出事：LLM 的 4xx 参数错误重试只会更慢更贵（`app/llm/errors.py:46-56`）。
2. **"脚本里 sleep 等待"**在这会出事：`issues/007` 里 `asyncio.wait_for` 都压不住忙等，必须让出点。
3. **"配置改了就重启"**在这行不通：`env.list` 要重建容器、`NEXT_PUBLIC_*` 要重建镜像。
4. **"权限就是菜单显隐"**在这不够：菜单是副本，真正的判断在后端（越权返回 404 而不是 403）。
