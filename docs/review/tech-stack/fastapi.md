# FastAPI + uvicorn + asyncio（Web 与事件循环）

## 本项目用它解决什么

对外提供 HTTP 与 WebSocket 两类接口；用**依赖注入**把鉴权、限流、图工厂做成可替换的东西（离线测试的前提）；用**中间件顺序**保证 CORS 预检不被限流吃掉。

## 实际用法（代码片段 + 文件:行号）

### 1）装配：所有外部依赖可注入

```python
# app/api/main.py:141-154
def create_app(
    *, settings=None, backend: CheckpointBackend = "memory",
    checkpointer_factory=None, graph_factory=None, runtime=None,
    rate_limiter=None, metrics=None, telemetry=None, queue=None, cache=None,
    probe_timeout_value=None,
) -> FastAPI:
```

### 2）中间件顺序：`_request_id` → 限流 → **CORS 最外层**

```python
# app/api/main.py:307-348
app.add_middleware(_request_id_middleware())
app.add_middleware(ApiMiddleware)
cors = api_config.cors_origins(settings)
if cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],   # 漏 PATCH = issues/005
        allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
        expose_headers=["X-Request-Id"],                     # 常量 REQUEST_ID_HEADER
        max_age=600,
    )
```

> FastAPI 的 `add_middleware` 是**往栈顶插**：最后加的那一层最外层。所以 CORS 必须最后加。

### 3）路由前缀：业务挂 `/api`，探针挂根

```python
# app/api/main.py:359-368
app.include_router(auth_router, prefix=prefix)      # auth_router 自带 /auth
app.include_router(admin_router, prefix=prefix)
app.include_router(chat_router, prefix=prefix)
app.include_router(tasks_router, prefix=prefix)
app.include_router(orders_router, prefix=prefix)
app.include_router(refunds_router, prefix=prefix)
app.include_router(infra_router)                    # /health /metrics 不带 prefix
```

### 4）依赖注入：身份与限流桶

```python
# app/security/rbac.py:164 —— 声明式鉴权
def require_permission(*permissions: str, identity_from: str = "actor") -> Callable[[F], F]:
    ...

# 用法（app/api/orders.py:79）
@require_permission(PERM_READ_OWN, identity_from="actor")
def list_my_orders(...): ...
```

```python
# app/api/deps.py:96-149 —— Actor：一次调用的身份与派生上下文
class Actor:
    user_id: int          # 行级过滤用的客户 id（员工恒为 0）
    role: str
    session_id: str
    thread_id: str        # c{uid}:{session} / e{emp_id}:{session}
    internal: bool
    subject_type: str
    subject_id: int
    account_id: int
    request_id: str
    @property
    def subject(self) -> str: ...   # 按人隔离（限流桶、任务归属）用它
```

### 5）WebSocket：握手顺序即安全顺序

```python
# app/api/chat.py:408-424
subprotocols = deps.ws_subprotocols(websocket)
try:
    principal = deps.ws_actor(websocket)
except Exception as exc:
    await websocket.accept(subprotocol=subprotocols[0] if subprotocols else None)
    await _send_frame(websocket, 1, FRAME_ERROR, {"code": "unauthorized", ...})
    await websocket.close(code=WS_CLOSE_UNAUTHORIZED)      # 4401
    return
await websocket.accept(subprotocol=WS_SUBPROTOCOL if subprotocols else None)
```

```python
# app/api/chat.py:465-486 —— issues/007 的修复点
except WebSocketDisconnect:
    return
except WebSocketDisconnected:
    return          # 连接已不可用：不接住就会滑进兜底分支 → 无让出点 → 纯 CPU 忙等
```

### 6）lifespan：三件事顺序不能换

```python
# app/api/main.py:192-197
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.security.auth import ensure_usable, is_dev_secret
    ensure_compatible_event_loop()   # 必须在建事件循环之前（Windows 上 psycopg 异步需要）
    ensure_usable()                  # prod 缺 JWT_SECRET → 这里就失败
```

## 最小原理

1. **ASGI 是三层函数**：`scope`（请求元数据）+ `receive`（收体）+ `send`（回响应）。中间件就是"包一层 ASGI 可调用对象"，所以它能改 `scope`、拦截、改响应头。
2. **事件循环是单线程**：一个协程只有在 `await`（真正的 I/O 或 sleep）处才会让出。**没有任何 await 的循环会独占整个进程** —— 这就是 `issues/007`。
3. **依赖注入是"每请求求值一次的参数"**：`Depends(fn)` 在请求进来时调用 `fn`，结果按需缓存；子依赖共享同一份缓存，所以"每请求查一次 token_version"是自然写法。
4. **WebSocket 握手阶段拿不到 HTTP 状态码**：浏览器只能感知"连接被拒绝"，所以要 accept 之后发一条 `error` 帧 + 自定义关闭码。

## 常见坑

| 坑 | 症状 | 本项目怎么处理 |
|---|---|---|
| CORS 中间件加早了 | 预检请求被限流/鉴权吃掉 → 浏览器只报 `Failed to fetch`，后端日志一条都没有 | CORS 最后加（`issues/005`） |
| `allow_methods` 漏非简单方法 | 同上（GET/POST 不触发预检，所以看不出来） | 从 OpenAPI schema 反查所有方法逐一断言（`tests/api/test_cors.py`） |
| 在 `import` 期创建 `TracerProvider` | "import 一个模块"变成"起一套监控" | 放到 lifespan（`app/api/main.py:234-244`） |
| WS 里 `except Exception` 兜底 | 连接已死时无让出点 → 忙等锁死（`issues/007`） | 单独接 `WebSocketDisconnected` |
| 用 `--reload` 起后端 | 本机实测起不来（watchfiles 要起子进程） | 开发期手动重启 |
| 多 worker 用内存限流 | 额度被放大到「副本数 × limit」 | 生产要 Redis（`app/api/ratelimit.py`） |

## 深入关键词

`ASGI` · `Starlette middleware` · `CORSMiddleware` preflight · `Depends` / `yield` 依赖 · `lifespan` · `WebSocket.accept(subprotocol=)` · `receive_json(mode="text")` · `event loop` / `await` 让出点 · `run_in_threadpool` · `uvicorn --workers`
