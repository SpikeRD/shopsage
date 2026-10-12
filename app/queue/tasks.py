"""入队 API：`enqueue_report` / `enqueue_agent_run` / `enqueue_kb_rebuild`（`12.queue.md` §6 第 2 步）。

本模块是**「一次入队」这件事的唯一入口**，也是 API 层与 worker 之间那条线的落点：

```
第 9 步 API  ──►  enqueue_*()  ──►  JobQueue（arq/Redis）──►  worker（app/queue/worker.py）
   秒级返回 job_id        上面这三步都不阻塞
```

## 为什么入队函数必须**快**

`12.queue.md` §8 的第一条验收是「长任务秒级返回 job_id，worker 异步执行，API 不阻塞」。
这句话在实现上只意味着一件事：**入队路径里不准做任何「干活」的事** ——
不查库、不调模型、不做 embedding。本模块因此只有三步：校验 → 算幂等键 → 投。

真正可能慢的只有 Redis 的往返（本仓实测跨主机 < 5ms），而它有一个显式的兜底：
`RedisJobQueue.enqueue` 在连接层故障时抛 `QueueUnavailable`，
由 API 翻成 **503 + `Retry-After`**（而不是 500：这是可用性问题，调用方等一会儿重试就好）。

## 为什么用 `enqueue_*` 包装而不是让调用方自己拼 `JobSpec`

四个理由，每一个都对应一次真实的错误：

1. **幂等键的默认值**。调用方忘了传 `idempotency_key` 时，`derive_key` 从
   （任务名 + user_id + 参数）派生出稳定键 —— 而「自己拼一个 uuid」是最容易犯的错
   （症状：调用方重试一次，任务跑两遍）；
2. **入参自检的位置**（`tasks_module.check_payload`）。缺 `user_id` 这类错误必须在
   **入队前**报出来，而不是等 worker 跑一趟、进死信、再让运维去看（见那里的说明）；
3. **`defer_by` 的上限**。延迟 5 年的任务不该是「一次性队列项」（那是 cron 的活）；
4. **返回值是对象**，不是裸字符串。调用方要 `job_id`，但排障时还需要
   「这次是新建的还是幂等命中」—— `EnqueueOutcome.created` 就是那个字段。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from app.queue.agent_runs import check_payload
from app.queue.backend import JobQueue
from app.queue.jobs import (
    FUNC_AGENT,
    FUNC_KB_REBUILD,
    FUNC_REPORT,
    MAX_DEFER_SECONDS,
    JobSpec,
    derive_key,
    normalize_key,
)

logger = logging.getLogger(__name__)


def build_spec(
    *,
    function: str,
    kind: str,
    payload: Mapping[str, Any],
    idempotency_key: str | None = None,
    user_id: int | None = None,
    defer_by: float = 0.0,
) -> JobSpec:
    """组装一个 `JobSpec`（校验 + 算键，**唯一**的组装点）。

    `idempotency_key` 缺省时从业务字段派生（见模块 docstring 第 1 条）。
    ⚠️ 派生时把 `user_id` **单独**参与计算（而不是塞进 payload）：payload 里可能
    没有 user_id（例如知识库重建），而「谁发起的」是幂等语义的一部分 ——
    两个用户各自发起的同名报表是两个任务。
    """
    data = dict(payload or {})
    check_payload(kind, data)
    defer = float(defer_by or 0.0)
    if defer < 0:
        raise ValueError(f"defer_by 不能为负（收到 {defer_by}）：想立刻执行就传 0")
    if defer > MAX_DEFER_SECONDS:
        raise ValueError(
            f"defer_by={defer:.0f}s 超出上限 {MAX_DEFER_SECONDS:.0f}s（30 天）："
            "更远的定时任务请用 cron（worker.py 的 cron_jobs），不要让它躺在 Redis 里"
        )
    key = normalize_key(idempotency_key) if idempotency_key else derive_key(
        function=function, user_id=user_id, params=data
    )
    return JobSpec(
        kind=kind,  # type: ignore[arg-type]
        function=function,
        payload=data,
        idempotency_key=key,
        user_id=user_id,
        defer_by=defer,
    )


async def enqueue_report(
    *,
    user_id: int,
    title: str = "订单报表",
    params: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    defer_by: float = 0.0,
    queue: JobQueue | None = None,
) -> Any:
    """入队一份报表（`report` 任务）。

    用法与 `12.queue.md` §2 一致：

    ```python
    job = await enqueue_report(user_id=1, params={...})    # → 有 .job_id，API 立刻返回
    ```

    `queue=None` 时用**默认队列**（按 `REDIS_URL` 建，进程内缓存，见 `backend.default_queue`）——
    这就是「一行入队」能成立的原因。传入 `queue` 是**注入点**（测试注入内存队列、
    脚本注入特定队列名），不是后门。
    """
    payload = {"user_id": int(user_id), "title": str(title or "订单报表"), **dict(params or {})}
    spec = build_spec(
        function=FUNC_REPORT,
        kind="report",
        payload=payload,
        idempotency_key=idempotency_key,
        user_id=int(user_id),
        defer_by=defer_by,
    )
    async with _queue_scope(queue) as active:
        outcome = await active.enqueue(spec)
    logger.info("报表入队：%s user=%s 标题=%s", outcome.job_id, user_id, title)
    return outcome


async def enqueue_agent_run(
    *,
    user_id: int,
    thread_id: str,
    message: str,
    role: str = "customer",
    replay_budget: int = 1,
    idempotency_key: str | None = None,
    defer_by: float = 0.0,
    queue: JobQueue | None = None,
) -> Any:
    """入队一轮长链路 Agent（`agent` 任务）。

    ⚠️ `thread_id` **必须由鉴权结果派生**（`app/api/deps.thread_id_for`）——
    队列这一层不做鉴权，它只是把调用方给的键带来带去。第 9 步的 API 若允许客户端
    自己指定 `thread_id`，这条路径就成了「推进别人的会话」的旁路
    （与 `8.graph-runbook.md` §4.4 讲的是同一件事）。
    """
    payload = {
        "user_id": int(user_id),
        "thread_id": str(thread_id),
        "message": str(message),
        "role": str(role or "customer"),
        "replay_budget": int(replay_budget),
    }
    spec = build_spec(
        function=FUNC_AGENT,
        kind="agent",
        payload=payload,
        idempotency_key=idempotency_key,
        user_id=int(user_id),
        defer_by=defer_by,
    )
    async with _queue_scope(queue) as active:
        outcome = await active.enqueue(spec)
    logger.info("Agent 任务入队：%s thread=%s", outcome.job_id, thread_id)
    return outcome


async def enqueue_kb_rebuild(
    *,
    source_dir: str = "",
    force: bool = False,
    user_id: int | None = None,
    idempotency_key: str | None = None,
    defer_by: float = 0.0,
    queue: JobQueue | None = None,
) -> Any:
    """入队一次知识库重建（`kb_rebuild` 任务，幂等长任务）。"""
    payload: dict[str, Any] = {"force": bool(force)}
    if source_dir:
        payload["source_dir"] = str(source_dir)
    spec = build_spec(
        function=FUNC_KB_REBUILD,
        kind="kb_rebuild",
        payload=payload,
        idempotency_key=idempotency_key,
        user_id=user_id,
        defer_by=defer_by,
    )
    async with _queue_scope(queue) as active:
        outcome = await active.enqueue(spec)
    logger.info("知识库重建入队：%s", outcome.job_id)
    return outcome


@asynccontextmanager
async def _queue_scope(queue: JobQueue | None) -> AsyncIterator[JobQueue]:
    """取队列：注入的**原样用且不关**；默认的**用完即释放**。

    ⚠️ 这个 `async with` 不是形式主义：默认队列拿的是**进程级缓存**的连接池
    （`default_queue()`），而池必须活到「本事件循环结束」—— 在入队函数里关掉它，
    下一个请求会拿到一个已关闭的池（症状是第二个请求开始偶发 `Connection closed`）。
    所以默认队列**不在这里关**，由 `close_default_queues()` 在应用/worker 退出时统一关。
    """
    if queue is not None:
        yield queue
        return
    from app.queue.backend import default_queue

    yield await default_queue()


#: 任务种类 → 入队函数（API 的 `POST /api/tasks` 按 kind 分派；**一处登记**）
DISPATCH: dict[str, str] = {
    "report": "enqueue_report",
    "agent": "enqueue_agent_run",
    "kb_rebuild": "enqueue_kb_rebuild",
}


async def dispatch_enqueue(kind: str, *args: Any, **kwargs: Any) -> Any:
    """按 `kind` 分派到对应的入队函数（API 层唯一需要 import 的函数）。

    为什么不让 API 自己写 `if kind == "report": ...`：那条 `if` 会与
    `jobs.KIND_TO_FUNCTION` 形成两处真相，新增一种任务时**漏改哪一处都不报错**
    （前者会 422，后者会在 worker 里找不到函数）。这里用一张表 + 一次 `getattr`，
    漂移会被 `tests/queue/test_queue_contracts.py` 的对账用例立刻抓住。
    """
    name = DISPATCH.get(str(kind))
    if name is None:
        raise ValueError(f"未知任务种类 {kind!r}：只能是 {', '.join(sorted(DISPATCH))} 之一")
    handler = globals()[name]
    return await handler(*args, **kwargs)


__all__ = [
    "DISPATCH",
    "build_spec",
    "dispatch_enqueue",
    "enqueue_agent_run",
    "enqueue_kb_rebuild",
    "enqueue_report",
]
