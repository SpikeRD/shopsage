"""异步任务层（第 12 步）：**长任务与 API 解耦**，失败可重试、超限进死信、可人工重放。

`12.queue.md` §2 的两行用法在这里都能拿到：

```python
from app.queue import enqueue_report, enqueue_agent_run, WorkerSettings

job = await enqueue_report(user_id=1, params={"title": "本月订单"})   # 有 .job_id，API 立刻返回
```

```powershell
uv run arq app.queue.worker.WorkerSettings        # 独立进程消费
```

## 包的边界（与第 7、8 步同一条纪律）

1. **本包不做业务**。它只做四件事：**算幂等键 → 入队 → 跑任务 → 失败收场**。
   报表怎么算在 `agent_runs.py`，Agent 怎么跑在第 8 步的图里，知识库怎么建在第 10 步的 rag 里；
2. **本包不 import FastAPI**。任务与 HTTP 无关（API 只是「入队的一个调用方」）——
   `app/api/tasks.py` 是接线处，反过来 import 就是分层倒了；
3. **重试与死信的判定只有一处**（`core.classify`）：它归并第 5、6 步已经定好的
   「可恢复/不可恢复」白名单，不重新发明一套；
4. **默认队列是进程级缓存**（`backend.default_queue`），生命周期由应用/worker 的
   启动与关闭钩子管 —— 因为 `redis.asyncio` 的连接绑定在事件循环上，
   跨循环复用会得到 `RuntimeError: Event loop is closed`。

## 三条不变式（改这个包之前先读）

1. **死信先落盘，任务才允许离开队列**（`core.execute_job` 的顺序）；
2. **`arq` 的 `max_tries` 必须比最大重试额度多一档**（`worker.MAX_ATTEMPTS`）——
   否则「额度用尽」那一次不进函数，死信永远不会被写（`worker.py` docstring 有源码级说明）；
3. **幂等键是稳定派生，不是随机数**（`jobs.derive_key`）：重投/重放不重复执行副作用（A2）。
"""

from app.queue.agent_runs import rebuild_knowledge_base, run_agent_turn, run_report
from app.queue.backend import (
    DEFAULT_QUEUE_NAME,
    EnqueueOutcome,
    JobQueue,
    JobState,
    QueueGauges,
    QueueUnavailable,
    RedisJobQueue,
    close_default_queues,
    default_queue,
    describe_state,
)
from app.queue.core import (
    DeadLetterRecord,
    DeadLetterStore,
    JobOutcome,
    PermanentTaskError,
    RetryTask,
    classify,
    execute_job,
    replay_plan,
)
from app.queue.jobs import (
    DEFAULT_POLICIES,
    FUNC_AGENT,
    FUNC_KB_REBUILD,
    FUNC_REPORT,
    JOB_KINDS,
    JOB_TIMEOUT_SECONDS,
    JobSpec,
    RetryPolicy,
    derive_key,
    job_id_for,
    policy_for,
)
from app.queue.store import PostgresDeadLetterStore
from app.queue.tasks import (
    build_spec,
    dispatch_enqueue,
    enqueue_agent_run,
    enqueue_kb_rebuild,
    enqueue_report,
)
from app.queue.worker import MAX_ATTEMPTS, WorkerSettings

__all__ = [
    "DEFAULT_POLICIES",
    "DEFAULT_QUEUE_NAME",
    "DeadLetterRecord",
    "DeadLetterStore",
    "EnqueueOutcome",
    "FUNC_AGENT",
    "FUNC_KB_REBUILD",
    "FUNC_REPORT",
    "JOB_KINDS",
    "JOB_TIMEOUT_SECONDS",
    "JobOutcome",
    "JobQueue",
    "JobSpec",
    "JobState",
    "MAX_ATTEMPTS",
    "PermanentTaskError",
    "PostgresDeadLetterStore",
    "QueueGauges",
    "QueueUnavailable",
    "RedisJobQueue",
    "RetryPolicy",
    "RetryTask",
    "WorkerSettings",
    "build_spec",
    "classify",
    "close_default_queues",
    "default_queue",
    "derive_key",
    "describe_state",
    "dispatch_enqueue",
    "enqueue_agent_run",
    "enqueue_kb_rebuild",
    "enqueue_report",
    "execute_job",
    "job_id_for",
    "policy_for",
    "rebuild_knowledge_base",
    "replay_plan",
    "run_agent_turn",
    "run_report",
]
