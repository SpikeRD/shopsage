# Redis + arq（缓存快路径与异步任务）

## 本项目用它解决什么

三件事共用一个 Redis：**限流计数**（滑动窗口）、**语义缓存的键值快路径**、**异步任务队列**（arq，含重试分级与死信）。

## 实际用法（代码片段 + 文件:行号）

### 1）限流：内存 / Redis 两种 store，同一条规则表

```python
# app/api/ratelimit.py:88-110（节选）
DEFAULT_RULES: Final[dict[str, RateLimitRule]] = {
    "chat": RateLimitRule(limit=30, window_seconds=60.0),
    "chat.stream": RateLimitRule(limit=60, window_seconds=60.0),
    "tasks": RateLimitRule(limit=10, window_seconds=60.0),
    ...
}
```

```python
# app/api/ratelimit.py:155-169
class RateLimitStore(Protocol): ...
class RateLimitRuntime(Protocol): ...

# app/api/ratelimit.py:200 / :250 —— 两种实现
class MemoryRateLimitStore: ...
class RedisRateLimitStore: ...

# app/api/ratelimit.py:352 —— 按 REDIS_URL 自动选
def open_rate_limiter(*, redis_url: str, rules) -> RateLimitRuntime: ...
```

### 2）缓存快路径：Redis 键值层 + pgvector 语义层

```python
# app/cache/api.py:64
class CacheHealth: ...        # 降级状态必须可见

# app/cache/api.py:403
def build_cache(...) -> Cache: ...
```

### 3）arq：三种任务 + 幂等键 + 重试分级

```python
# app/queue/jobs.py:43-56
JobKind = Literal["report", "agent", "kb_rebuild"]
JOB_KINDS: Final[tuple[str, ...]] = ("report", "agent", "kb_rebuild")
FUNC_REPORT: Final[str] = "shopsage.run_report"
FUNC_AGENT: Final[str] = "shopsage.run_agent_turn"
FUNC_KB_REBUILD: Final[str] = "shopsage.rebuild_knowledge_base"
```

```python
# app/queue/jobs.py:169 / :253 / :277
DEFAULT_POLICIES: Final[dict[str, RetryPolicy]] = {...}
def derive_key(*, function: str, user_id: int | None, params: dict | None) -> str: ...
def job_id_for(idempotency_key: str) -> str: ...          # 前缀 q_
```

```python
# app/queue/worker.py:439 —— 容器里的启动命令就是它
class WorkerSettings: ...
# docker-compose.yml:88 → arq app.queue.worker.WorkerSettings
```

```python
# app/queue/core.py:162 —— 失败分类决定"重试还是终态"
def classify(exc: BaseException) -> bool: ...
```

### 4）死信落业务库（不是留 Redis）

```python
# app/queue/store.py:124
class PostgresDeadLetterStore: ...
# 表 queue_dead_letters（迁移 20261011_7164220823a3）
```

```python
# app/queue/core.py:486
def replay_plan(entry: DeadLetterRecord, *, defer_by: float = 0.0, key_suffix: str = "") -> ReplayPlan: ...
```

### 5）默认队列按事件循环缓存（生命周期坑）

```python
# app/queue/backend.py:425-474
_QUEUES: dict[int, RedisJobQueue] = {}
def _loop_key() -> int: ...
async def default_queue(...) -> RedisJobQueue: ...
async def close_default_queues() -> int: ...
```

## 最小原理

1. **Redis 是单线程内存 KV**：所有"计数/去重/短路"类需求都能用 `INCR` + `EXPIRE` 或 `SETNX` 表达。它的价值不是"快"，而是"多副本共享一份状态"。
2. **arq 是 asyncio 原生的 Redis 队列**：任务 = `(函数名, 参数, job_id)`；`max_tries` 控制总尝试次数；延迟任务用 sorted set 实现。
3. **job_id 做幂等键**：同一个 id 再入队一次，arq 认得出来（配合业务侧的 `created` 标志）。
4. **死信 = 业务状态，不是基础设施状态**：Redis 里的失败记录会随 TTL 消失；而"失败到最后一步"需要人来看、来重放，所以落 PostgreSQL。

## 常见坑

| 坑 | 症状 | 依据 |
|---|---|---|
| 只登记 `scope.py` 不登记 `LIMITED_SCOPES` | 额度配了但完全不生效（实测连 40 次登录都是 401，没一次 429） | `app/api/middleware.py:69-76`（issue 003 的修复） |
| 限流桶用 `user_id` | 员工 `user_id` 恒为 0 → 所有人共用一个桶 | `app/api/deps.py:113-114` |
| 多 worker 用内存限流 | 额度被放大到「副本数 × limit」 | `app/api/main.py` 模块 docstring |
| 默认队列跨事件循环复用 | 同一进程起第二个 app → `RuntimeError: Event loop is closed` | `app/api/main.py:277-284` |
| 把死信表 0 行当成"没接上" | 0 行是**健康**形态 | `docs/runbook/12.queue-runbook.md` §1.1 |
| api 与 worker 分别构建镜像 | "api 已更新、worker 还是旧版" → 任务永不执行 | `docs/runbook/17.docker-ops.md:115-117`（所以共用一个镜像） |

## 深入关键词

`Redis INCR / EXPIRE` · sliding window vs fixed window · `arq` `WorkerSettings` / `max_tries` / `job_id` · `defer_by` 延迟任务 · dead letter queue · idempotency key · `close()` 生命周期 · `redis_settings_from_url`
