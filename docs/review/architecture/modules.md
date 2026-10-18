# 模块表与阅读路径

> 模块表列的是**代码里真实存在的 16 个模块 + 3 个附属目录**。每行的"代码位置"都能直接打开对应目录/文件；
> 找不到对应目录的模块一律不列入（Q1 自检标准）。

## 1. 模块表

| 模块 | 职责（一句话） | 对外接口（真正被别的模块调的东西） | 依赖（内部） | 代码位置 |
|---|---|---|---|---|
| **config** | 唯一配置类 + prod 启动自检 | `Settings`、`get_settings()`、`dump_for_logging()`；`API_*` 走 `app.api.config` | 无 | `app/config/`（`settings.py` 446 行）+ `app/api/config.py` |
| **db** | 模型 / 会话池 / 读写双引擎 / 强制行级过滤 | `get_write_engine()` `get_read_engine()` `get_db()` `assert_row_filtered()` | config | `app/db/`（6 文件）+ `alembic/`（7 迁移） |
| **security** | 身份 · RBAC · 脱敏 · SQL 守卫 · 注入检测 | `ROLE_PERMISSIONS` `entry_for_role()` `validate_sql()` `mask_pii()` `require_permission` `create_access_token()` | db, config | `app/security/`（7 文件） |
| **llm** | 模型调用门面 + 退避重试 + embedding 双后端 | `llm_call()` `embed()` `RetryableLLMError` `PermanentLLMError` | config | `app/llm/`（3 文件） |
| **tools** | 读/写两类受审计的动作 | `__all__` 27 个：`execute_readonly_query` `retrieve_docs` `create_ticket` `request_refund` `decide_refund` `build_chart_spec` … | db, security, llm, rag | `app/tools/`（9 文件，`refund.py` 930 行） |
| **rag** | 解析 / 分块 / 混合检索 / rerank | `hybrid_search()` `hybrid_search_async()` `ingest_document()` `reindex_document()` | db, llm | `app/rag/`（12 文件） |
| **cache** | 语义缓存 / 结果缓存 / 三级失效 | `lookup()` `remember()` `build_cache()` `semantic_cache_get/set` `bump_after_ingest()` | db, llm, rag | `app/cache/`（14 文件） |
| **agents** | 7 个图节点的实现 + 公共零件 | 各 `*_node(state, **injections)`；`inject_identity()`；`generate_json()` | tools, llm, rag, cache, security | `app/agents/`（9 文件） |
| **graph** | LangGraph 装配 / 状态 / 路由 / checkpointer / 回合驱动 | `build_graph()` `compile_graph()` `graph_config()` `run_turn()` `open_checkpointer()` | agents, config | `app/graph/`（8 文件） |
| **api** | ASGI 装配 / 路由 / WS 流式 / 限流 / 鉴权依赖 | `create_app()`；HTTP + WS 路由（见 `consolidated/modules/09-api.md` 路由表） | graph, security, queue, cache, observability | `app/api/`（18 文件）+ `app/main.py` |
| **queue** | arq 入队 / 重试分级 / 死信 / 重放 | `enqueue_report` `enqueue_agent_run` `enqueue_kb_rebuild` `WorkerSettings` | db, graph, rag | `app/queue/`（9 文件） |
| **observability** | 指标 / span / Langfuse | `setup_observability()` `node_span()` `record_*()` | config | `app/observability/`（6 文件） |
| **eval** | golden dataset / 三指标 / 单 vs 多 Agent | `load_dataset()` `evaluate_rag()` `evaluate_agent()` `compare_single_vs_multi()` `run_suite()` | graph, rag, llm | `app/eval/`（13 文件）+ `eval/`（薄壳 + 2 份数据集） |
| **mcp** | MCP 薄壳（4 个工具） | `build_server()` `main()`；`MCPServer` | tools, security | `app/mcp/`（6 文件） |
| **infra** | Compose 编排 / 镜像 / 观测栈配置 | `docker-compose.yml`、`infra/docker-compose.dev.yml`、两个 Dockerfile | — | `infra/`、`app/Dockerfile`、`web/Dockerfile` |
| **web** | 三个入口 + 流式渲染 + 溯源 + 图表 | 页面路由（7 个）；`lib/api.ts` 封装的 HTTP 调用 | api（HTTP/WS 契约） | `web/src/`（app 10 + components 7 + lib 12） |
| **scripts**（附） | 种子 / 自检 / 取证 / 演示 | `python -m scripts.<name>` | 几乎全部 | `scripts/`（27 文件） |
| **tests**（附） | pytest（后端）+ node:test（前端） | — | 全部 | `tests/`（约 150 文件）、`web/tests/`（14 + live 1） |
| **alembic**（附） | 迁移脚本 | `alembic upgrade head` / `downgrade` | db | `alembic/versions/`（7 文件） |

### 依赖方向的强约束（代码里显式写明的）

- `app/eval` → **不 import FastAPI**。
- `app/mcp` → **不 import `app.api` / fastapi / uvicorn / starlette**。
- `app/agents` → **不 import `app/observability`**（埋点在 `app/graph/nodes.py` 统一做）。
- 前端 `web/src/lib/auth.ts` 的角色表是**后端副本**，不参与授权判断。

---

## 2. 阅读路径（3 条，按转型背景排序）

> 你的背景是 RPA 开发工程师转 agent 开发 —— 你对"流程编排、脚本化取数、稳定性/重试"已经很熟，
> 缺的通常是**异步 Web 服务的生命周期、LLM 的不确定性、以及"权限边界"这条线**。下面三条按此定制。

### 路径 A · 先建立"一次问答"的完整心智模型（约 2 小时）

适合第一次接手。目标：能在纸上画出一次请求经过的每一层，并说出每层的失败会怎样。

1. `docs/review/architecture/diagrams.md` §3 时序图 —— 先看骨架帧为什么在跑图之前。
2. `docs/review/consolidated/modules/09-api.md`（路由表 + WS 五类帧 + 限流三道登记）。
3. `docs/review/consolidated/modules/08-graph.md`（拓扑 + `durability="sync"` + 断点重放）。
4. `docs/review/consolidated/modules/07-agents.md`（7 个节点各自干什么、注入点在哪）。
5. 代码对照：`app/api/chat.py:392-605` → `app/graph/run.py:125-244` → `app/graph/nodes.py:420-440`。

### 路径 B · 先建立"LLM 不稳定怎么兜"的工程直觉（约 3 小时）

适合已经会写 RPA 流程、想把"异常处理"升级到"LLM 场景"的人。目标：分清**可重试 / 永久失败 / 断点重放 / 降级话术**四种处置。

1. `docs/review/consolidated/modules/05-llm.md`（两种错误语义；4xx 不重试）。
2. `docs/review/consolidated/modules/12-queue.md`（重试分级 + 死信表的判据）。
3. `docs/review/consolidated/issues/问题台账.md` 的 007 —— 一条死连接如何锁死整个事件循环（事件循环视角的"死循环"，和 RPA 的忙等是同一类问题）。
4. `docs/review/consolidated/modules/08-graph.md` §8.4（网络型 vs 收口型节点的兜底策略差异）。
5. 代码对照：`app/llm/errors.py` → `app/graph/nodes.py:134-171` → `app/graph/run.py:166-211`。

### 路径 C · 先建立"权限边界"这条线（约 2.5 小时）

适合要接手安全评审或排越权问题的人。目标：能说清"同一行数据，谁能看到、谁能改、改了几层拦"。

1. `docs/review/consolidated/modules/04-security.md`（四条正交边界 + 6 角色 8 权限点）。
2. `docs/review/consolidated/modules/06-tools.md`（读写路径分离 + 退款幂等与额度）。
3. `docs/review/consolidated/changes/变更台账.md` 的变更 003（退款拆成申请/发起/审批三个权限点）。
4. `docs/review/consolidated/issues/问题台账.md` 的 003（两个 P0：登录限流、行级过滤可绕过）。
5. 代码对照：`app/security/rbac.py:54-160` → `app/security/sql_guard.py:82-162` → `app/agents/data.py:420-470`。

### 补充路径 D · 只想跑起来（约 40 分钟）

1. `docs/review/consolidated/runbook/环境与部署.md` §E（一次部署的命令序列）。
2. `docs/review/consolidated/runbook/前端.md` §A（起前端）。
3. `README.md:145-150`（三个演示账号）。

---

## 3. 与 `docs/modules/` 的对应关系

| 精简件 | 原文档 |
|---|---|
| `consolidated/modules/01-config.md` … `16-web.md` | `docs/modules/1.config.md` … `16.web.md`（1:1） |
| `consolidated/modules/07-agents.md` | `docs/modules/7.agents_module.md`（文件名与其他不统一，内容对应） |
| `consolidated/runbook/*.md` | 见 `01-巡视报告.md` §4.2 的"runbook 合并表" |
| `consolidated/changes/变更台账.md` | `docs/changes/001/002/003` |
| `consolidated/issues/问题台账.md` | `issues/001`–`010` + `issues/README.md` |
| `consolidated/kb/kb-语料清单.md` | `docs/kb/`（8 份语料） |
