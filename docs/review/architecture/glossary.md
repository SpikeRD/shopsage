# 术语表与核心约定

> 本项目文档里高频出现、但**含义容易被想当然**的词。每条都给代码位置，不靠记忆。
> 建议配合 `architecture/diagrams.md` 一起看。

## 1. 身份与权限

| 术语 | 本项目的确切含义 | 代码位置 |
|---|---|---|
| **三档入口** | `customer`（对客）/ `internal`（对内）/ `admin`（管理台）。**由服务端算好**随登录响应返回，前端不复制这条规则 | `app/security/accounts.py:77-79`、`:246 entry_for_role()` |
| **`entry_for_role`** | 有 `admin` 权限点 → 管理台；否则能进对内 → 对内；否则 → 对客 | `app/security/accounts.py:246` |
| **`can_enter_internal`** | 能不能**进对内界面** = 有 `read:all_orders` **或** `approve:refund`。⚠️ **不等于**"能查全站" | `app/security/rbac.py:139` |
| **`canUseInternal`**（前端） | 能不能**查全站** = 有 `read:all_orders`。与上面是两个函数，不许混用 | `web/src/lib/auth.ts:107` |
| **`Actor`** | 一次调用的身份与派生上下文。`user_id` 是**行级过滤用的客户 id（员工恒为 0）**，不是"账号 id" | `app/api/deps.py:96-149` |
| **`subject`** | 主体标识串 `c{user_id}` / `e{emp_id}`。**按人隔离（限流桶、任务归属）必须用它**，不能用 `user_id` | `app/api/deps.py:127-132` |
| **`subject_type` / `subject_id`** | `customer`/`employee` + 各自表里的主键（`users.user_id` / `employees.emp_id`） | `app/security/auth.py:79-84` |
| **`token_version`（世代号）** | 账号上的一个整数；停用/改密/改角色时 +1，所有在途令牌下一次请求即失效 | `app/security/auth.py:75`、`app/api/deps.py:291` |
| **`account_id`（`aid` 声明）** | 是哪一次登录，用于审计归因 | `app/security/auth.py:74` |
| **权限点** | 8 个：`read:own` `read:all_orders` `read:finance` `write:refund` `write:ticket` `request:refund` `approve:refund` `admin` | `app/security/rbac.py:36-51` |
| **敏感等级** | 0 公开 / 1 脱敏 / 2 内部可见 / 3 财务级。**与权限点正交**：权限管"能做什么"，等级管"看多清楚" | `app/security/policy.py:81-115` |
| **行级过滤（D1）** | 对客路径强制 `WHERE user_id = 本人`。**唯一真相在 `app/db/queries.py`**，别处只校验不生成 | `app/db/queries.py:52` |
| **收口型 / 网络型节点** | 收口型（synthesis / guardrail_output）失败必须停；网络型（data / rag / analysis / refund_request）失败只写 refusal 后继续 | `app/graph/nodes.py:105`、`:134` |

## 2. 图与状态

| 术语 | 含义 | 代码位置 |
|---|---|---|
| **`thread_id`** | checkpoint 隔离键，形状 `c{uid}:{session}` / `e{emp_id}:{session}`。变更 001 从 `u{}` 改过来（否则员工与客户同号会撞桶） | `app/api/deps.py:176-199` |
| **`durability="sync"`** | 每个 super-step 结束**先落盘再返回**。异步模式（框架默认）下崩溃会"节点跑了、checkpoint 没写" | `app/graph/builder.py:89` |
| **断点重放** | 节点抛异常 → `next` 不推进 → `astream(None, cfg)` 从这里继续，已完成节点不重跑 | `app/graph/run.py:173-211` |
| **`as_node=target`** | `aupdate_state` 时"替失败节点收尾"，让图从它的下游继续（不传会无限重放） | `app/graph/run.py:206` |
| **`refusals`** | 一个列表：这一轮"没拿到/没做成/被拦"的原因。synthesis 据此如实作答 | `app/agents/state.py:51`（`GraphState`） |
| **`blocked`** | "这一轮不能继续/不能出网"的**唯一**表达。挂起**不是** blocked（`Turn.interrupt` 才表达挂起） | `app/graph/nodes.py:390-393` |
| **`Turn`** | 一轮的结果对象：`visited` / `blocked` / `replayed` / `exhausted` / `interrupt` / `fallback` | `app/graph/run.py:72` |
| **`gate` 节点** | 人工审批闸门原语。**默认图里不注册**（`GATED_ROLES` 为空集）；变更 003 起审批搬到退款单据上 | `app/graph/nodes.py:373`、`app/graph/config.py:82` |
| **`GraphRuntime`** | 一次跑图的全部外部依赖与预算（`llm` / `search` / `cache` / `row_limit` / `max_attempts` / `interrupts`） | `app/graph/config.py:102` |

## 3. 请求与协议

| 术语 | 含义 | 代码位置 |
|---|---|---|
| **五类 WS 帧** | `skeleton` / `node` / `answer` / `done` / `error` | `app/api/schemas.py:50-54` |
| **骨架帧** | 握手后**立刻**发的第一帧（不含模型产物）。"首帧 <1s"靠它，不靠 token 流 | `app/api/chat.py:548-551` |
| **子协议 `shopsage.v1`** | 浏览器不能给 WS 设自定义头，所以令牌走子协议：必须同时带 `shopsage.v1` 与 `bearer.<token>` | `web/src/lib/contracts.ts:27-30` |
| **关闭码 4401 / 4429 / 4400** | 未授权 / 限流 / 请求不合法（应用自定义段） | `web/src/lib/contracts.ts:33-35` |
| **`flow_state`** | `delivered` / `blocked` / `approval_required`。回答 HTTP 码回答不了的问题："这一轮做到哪一步" | `web/src/lib/contracts.ts:22` |
| **API 前缀** | 业务路由挂 `API_PREFIX`（默认 `/api`）；`/health`、`/metrics` **固定挂根** | `app/api/config.py:43`、`app/api/main.py:368` |
| **限流三道登记** | 一个接口要被限流必须同时登记：`LIMITED_SCOPES` + `SCOPE_BY_ROUTE` + `DEFAULT_RULES` | `app/api/middleware.py:57-84` |
| **`X-Request-Id`** | 请求追踪 id，中间件生成/透传，出现在响应头与日志 | `app/api/middleware.py:88`（常量 `REQUEST_ID_HEADER`） |
| **`/health` 恒 200** | 依赖故障写进 body 的 `status`/`components`；进程死了由"没有响应"表达 | `app/api/health.py:9-18` |

## 4. 检索、缓存与评测

| 术语 | 含义 | 代码位置 |
|---|---|---|
| **混合检索** | 向量腿（HNSW 余弦）+ 关键词腿（n-gram + SQL 内手算 BM25），RRF 融合 | `app/rag/retriever.py:491` |
| **RRF** | Reciprocal Rank Fusion，按名次而非分数融合两路结果（`RAG_RRF_K=60`） | `app/rag/retriever.py:159` |
| **`corpus_revision`** | 语料版本号。灌库时推进 → 缓存指纹全变 → 旧答案失效 | `app/cache/corpus.py:219` |
| **语义缓存五道闸** | scope / 版本指纹 → 时效分档 → 兜底线 → 命中线 → 多轮检查 | `app/cache/core.py:326` |
| **灰区** | 相似度落在 `[floor, threshold)` = 判未命中但记账（"像但不敢答"） | `app/cache/core.py:390-395` |
| **命中线 / 兜底线** | `CACHE_SIM_THRESHOLD=0.85` / `CACHE_SIM_FLOOR=0.70`。**换 embedding 必须重标定** | `app/cache/settings.py:92`、`:107` |
| **`grep` 得到的 `citations`** | 出处字符串列表（`docs/kb/退货政策.md #c9138...`）。⚠️ **非空 ≠ 答出来了**（`issues/009`） | `app/agents/rag.py:44` |
| **golden dataset** | `rag_golden_v1.json`（15 条）/ `agent_golden_v1.json`（10 条） | `eval/datasets/` |
| **三指标** | `recall` / `faithfulness` / `answer_relevancy`（名字对齐 Ragas，实现自研） | `app/eval/metrics.py:72` |
| **判官三档** | `rules`（默认，离线）/ `strict` / `llm` | `app/eval/judge.py:312` |

## 5. 运维

| 术语 | 含义 | 代码位置 |
|---|---|---|
| **`env.list` vs `.env`** | `.env` 只管 compose 的 `${...}` 插值；`env.list` 经 `env_file:` 注入**容器内** | `docker-compose.yml:12-17` |
| **`WITH_LOCAL_EMBEDDING`** | 是否把这套本地 embedding 依赖（torch ≈2GB）装进镜像。默认 `false`，必须与 `EMBEDDING_BACKEND` 一起改 | `docker-compose.yml:28-32` |
| **死信（dead letter）** | 重试用尽的任务落到业务库 `queue_dead_letters`，可人工重放。**0 行是健康形态** | `app/queue/store.py:124` |
| **`CHECKPOINT_BACKEND`** | `memory`（默认，重启即丢）/ `postgres`（生产）/ `redis` | `app/api/config.py:178` |
| **`MCP_POLICY`** | `customer`（强制行级过滤）/ `internal` | `app/mcp/settings.py:58-60` |
| **服务名 vs IP** | compose 里的连接串 host 必须写**服务名**（`postgres` / `redis`），写 IP 会在容器重建后突然连不上 | `env.list.example` 注释 |
