# 模块 09 · api（FastAPI 路由 / WebSocket / 限流）

## 目的

把图、鉴权、限流、可观测性装配成一个 ASGI 应用，对前端给两类出口：**HTTP**（问答、订单、退款、账号、管理台、任务）与 **WebSocket**（流式问答）。

## 关键结论

### 9.1 入口与装配

- 启动入口：`app/main.py` → `app = create_app(backend=checkpoint_backend(...))`；`create_app()` 在 `app/api/main.py:141`，**所有外部依赖可注入**（`runtime` / `rate_limiter` / `metrics` / `queue` / `cache` / `checkpointer_factory` / `graph_factory` / `telemetry`），这是"离线可测"的前提。
- lifespan 三件事（顺序不能换）：`ensure_compatible_event_loop()` → `auth.ensure_usable()` → 装配依赖挂到 `app.state`（`app/api/main.py:192-293`）。
- 中间件顺序：`_request_id` → `ApiMiddleware`（限流）→ **CORS 最外层**（`app/api/main.py:307-348`）。CORS 必须最外层，否则预检会被限流/鉴权先吃掉。
- CORS 的 `allow_methods=["GET","POST","PATCH","OPTIONS"]` **不能漏 `PATCH`**（`issues/005`：漏了 → 管理台停用/改角色 `Failed to fetch` 且后端一条日志都没有）。

### 9.2 路由清单（代码事实）

业务路由统一挂 `API_PREFIX`（默认 `/api`，`app/api/config.py:43`）；`/health`、`/metrics` **固定挂根**（`app/api/main.py:368`）。

| 方法 | 路径 | 文件 | 鉴权 |
|---|---|---|---|
| POST | `/api/auth/login` · `/api/auth/refresh` | `app/api/auth.py:100/119` | 无 / refresh 令牌 |
| GET | `/api/auth/me` | `app/api/auth.py:161` | access |
| GET/POST | `/api/admin/accounts` | `app/api/admin.py:93/129` | `admin` |
| PATCH | `/api/admin/accounts/{id}` | `app/api/admin.py:159` | `admin` |
| POST | `/api/admin/accounts/{id}/password` | `app/api/admin.py:185` | `admin` |
| POST | `/api/chat` | `app/api/chat.py:271` | access |
| WS | `/api/chat/stream` | `app/api/chat.py:392` | 子协议 `shopsage.v1` + `bearer.<token>` |
| GET | `/api/orders` · `/api/orders/{order_id}` | `app/api/orders.py:74/117` | `read:own`（`:79`/`:122`） |
| POST | `/api/refunds` | `app/api/refunds.py:137` | `request:refund`（`:143`） |
| GET | `/api/refunds` | `app/api/refunds.py:204` | `approve:refund`（`:209`） |
| POST | `/api/refunds/decision` | `app/api/refunds.py:276` | `approve:refund`（`:281`） |
| GET | `/api/tasks/queue` · `/api/tasks/{job_id}`；POST `/api/tasks` | `app/api/tasks.py:101/216/139` | access |
| GET | `/health` · `/metrics` | `app/api/health.py:251/302` | 无 |

**`POST /api/chat/approve` 已删除**（`app/api/chat.py:369-386`）—— 见 `../../02-异常清单.md` E-04。

### 9.3 限流（三道登记，缺一不生效）

一个接口要被限流，必须**同时**登记在三处：

1. `app/api/middleware.py:57-84 LIMITED_SCOPES`（中间件要不要问限流器）；
2. `app/api/scope.py SCOPE_BY_ROUTE`（用哪个 scope 名）；
3. `app/api/ratelimit.py:88 DEFAULT_RULES`（默认额度）。

当前 6 条：`POST /api/chat`(30/60s)、`POST /api/tasks`(10/60s)、`POST /api/auth/login`、`POST /api/auth/refresh`、`POST /api/refunds`、`POST /api/refunds/decision`。
`tests/api/test_api_contracts.py` 有**双向断言**钉住三处一致。

限流桶按 `subject`（`c2`/`e1`）而不是 `user_id`（员工 `user_id` 恒为 0，会共用桶）。

### 9.4 WebSocket 流式（5 类帧）

| event | 含义 |
|---|---|
| `skeleton` | 骨架帧：握手后**立刻**发（首字节，不含模型产物）；route 确定后再发一条"收敛"帧 |
| `node` | 每个跑过的节点一条（`node` 名 + `progress` + 裁剪后的 `delta`） |
| `answer` | 最终回答（整段，`done:true`）+ `citations` + `chart` |
| `done` | 收尾（`flow_state` / `latency_ms` / `first_frame_ms`） |
| `error` | 错误（`code` / `message`） |

（常量：`app/api/schemas.py:50-54`；发送顺序：`app/api/chat.py:548-605`）

关闭码（应用自定义段）：`4401` 未授权、`4429` 限流、`4400` 请求不合法（`web/src/lib/contracts.ts:33-35`）。

**注意**：`answer` 帧目前是**整段产出**而非逐字流式（`app/api/chat.py:602` 的注释「目前是整段产出」）。"首帧 <1s"由 `skeleton` 帧保证，不是 token 流。

### 9.5 一轮的完整链路

```
HTTP: request_id → 限流 → 鉴权(deps.require_actor) → chat() → _graph_for(app, actor)
      → run_turn(graph, entry_state(actor, message), thread_id=actor.thread_id)
      → build_response(turn) → ChatResponse
WS:   子协议+令牌 → accept → 限流 → 循环{ 收消息 → actor → _stream_turn }
      → skeleton → run_turn → node 帧* → answer 帧 → done 帧
```

## 待办 / 已知边界

- `issues/007` 的修复点在 `app/api/chat.py:468-486`（`WebSocketDisconnected` 必须单独接住，否则纯 CPU 忙等锁死整个事件循环）。
- `app/api/health.py:9-18`：`/health` **恒 200**，依赖故障写进 body。这是刻意设计。
- ⚠️ 多 worker 时的限流：`MemoryRateLimitStore` 会被放大到「副本数 × limit」，生产要 Redis（`app/api/main.py` 模块 docstring 表格已列）。

## 来源

- `docs/modules/9.api.md`（122 行，2026-10-15 17:41）、`docs/runbook/9.api-runbook.md`（947 行）
- `docs/changes/001`、`docs/changes/003`、`issues/005`、`issues/007`
- 代码：`app/api/*.py`；测试：`tests/api/*.py`（13 个文件）
