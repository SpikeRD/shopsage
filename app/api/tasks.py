"""`POST /api/tasks`、`GET /api/tasks/queue`、`GET /api/tasks/{job_id}`（第 12 步的接缝）。

`12.queue.md` §2 的对外接口是 `enqueue_report(...) → job_id`，而「API 秒级返回 job_id、
worker 异步执行」这条验收（§8 第 1 条）需要一个**能被人调用**的入口 —— 就是本模块。

## 四条边界（每一处都对应一次容易犯的错）

1. **入队路径里不干活**：不查库、不调模型、不做 embedding。三件事加起来才叫「秒级返回」；
2. **入队成功 ≠ 任务成功**：`POST` 返回 **202 Accepted**（不是 200）—— 200 会让客户端
   以为「拿到了结果」，而它只拿到了一个**凭据**（`job_id`）。202 的语义正是
   「已受理、还没完成」，与队列的本质一致；
3. **`GET /api/tasks/{job_id}` 只给发起人看**：任务结果里可能有该用户的数据，
   而 `job_id` 是可枚举的（它是幂等键的派生）。所以查询时比对
   「令牌里的 user_id」与「结果里的 user_id」，不一致就按 404 处理
   （与 `app/api/errors.py` 的「越权按 404」口径一致 —— 不确认资源是否存在）；
4. **静态路径必须比参数路径先声明**：`/tasks/queue` 与 `/tasks/{job_id}` 同时存在时，
   FastAPI 按**声明顺序**匹配，反了的话 `queue` 会被当成一个 job_id
   （症状：查队列现状返回 404「未找到该任务」—— 路由其实是对的，只是匹配顺序反了）。
   所以本文件里 `queue_status` 排在 `task_status` 之前，`tests/queue/test_queue_api.py`
   有一条用例钉住它。

## 为什么闸门与审批不在这里

`ticket` 那类敏感分支的挂起/批准走 `POST /api/chat/approve`（第 9 步已经落地）。
本模块的 `agent` 任务**可能**在图里挂起（`pending_approval=True`），那时它算
「任务成功结束、等待批准」—— 批准通道仍在 chat 那一侧，队列只负责把这一轮推到闸门前。
把批准也塞进队列会造出第二条审批通道（两份鉴权、两份审计），那是明确的倒退。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.api import deps
from app.api.errors import ApiError
from app.api.schemas import (
    DeadLetterView,
    QueueStatusResponse,
    TaskAcceptedResponse,
    TaskRequest,
    TaskStatusResponse,
)
from app.queue import JobQueue, QueueUnavailable, describe_state, dispatch_enqueue
from app.queue.jobs import JOB_KINDS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])

#: 长任务入队后的建议轮询间隔（秒）。
#:
#: 为什么不给「预计完成时间」：队列里没有那个信息（它取决于积压与任务耗时），
#: 编一个数字出来只会让客户端写死一个错的轮询节奏。
POLL_HINT_SECONDS: int = 5

#: **内部专属**的任务种类（普通客户提交会被 403 挡回）。
#:
#: `kb_rebuild` 会重建整库向量索引 —— 它影响的是**所有人**的检索结果，
#: 而且很贵（每次全文 embedding）。把它开放给客户，等于给了一个
#: 「一次请求打满 CPU」的入口（与限流的初衷正好相反）。
INTERNAL_KINDS: frozenset[str] = frozenset({"kb_rebuild"})


async def queue_for(request: Request) -> JobQueue:
    """取本次请求要用的队列（**异步**：默认队列要连 Redis）。

    ⚠️ 与 `deps.rate_limiter` 同一形状：优先 `app.state.queue`（**注入点**），
    没有才回落到进程默认队列。测试与演示注入内存队列（离线、无 Redis），
    生产不注入任何东西 → 走 `REDIS_URL`（理由见 `app/queue/backend.default_queue`）。

    ⚠️ 刻意**不**用 FastAPI 的同步依赖：同步依赖里不能 await，而 `default_queue()`
    是协程 —— 硬写会得到 `RuntimeError: no running event loop`，
    且它只在「第一次真的用默认队列」时炸（注入队列的用例反而全绿）。
    """
    injected = getattr(request.app.state, "queue", None)
    if injected is not None:
        return injected
    from app.queue import default_queue

    return await default_queue()


def api_prefix_of(request: Request) -> str:
    """本次请求的业务前缀（拼 `status_url` 用；**不硬编码** `/api`）。

    ⚠️ 硬编码 `/api` 会在 `API_PREFIX` 被改（网关统一加一层前缀）时给出一个 404 的链接
    —— 而它出现在**成功响应**里，最难被发现。前缀只有一处真相
    （`app/api/config.api_prefix`），这里调它，改配置时自动跟上。
    """
    from app.api.config import api_prefix

    return api_prefix(getattr(request.app.state, "settings", None)) or ""


# ── 队列现状（**必须声明在 `/tasks/{job_id}` 之前**，理由见模块 docstring 第 4 条）──


@router.get(
    "/tasks/queue",
    response_model=QueueStatusResponse,
    summary="队列现状（积压 / 延迟任务 / 待处理死信）",
)
async def queue_status(
    request: Request,
    principal: Any = Depends(deps.require_principal),
) -> Any:
    """队列读数（`12.queue.md` §5「背压」的可观测出口）。

    ⚠️ 它**不是**自动扩缩容的输入（那要用延迟分位数，第 11 步的指标）：
    一个「每任务 5 分钟」的队列，积压 10 也完全正常。这个接口回答的是
    「现在队列里堆了多少、有没有死信躺着」—— 给人看，不是给程序看。
    """
    del principal  # 鉴权已由依赖完成；读数本身与身份无关
    queue = await queue_for(request)
    try:
        gauges = await queue.gauges()  # type: ignore[attr-defined]
    except QueueUnavailable as exc:
        raise ApiError(503, "queue_unavailable", f"任务队列暂时不可用：{exc}", log_level=logging.ERROR) from exc
    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None:
        metrics.gauge("shopsage_queue_depth", float(gauges.depth))
    return QueueStatusResponse(
        depth=int(gauges.depth),
        deferred=int(gauges.deferred),
        backlog=int(gauges.backlog),
        dead_letters=int(gauges.dead_letters),
        oldest_dead_letter_seconds=gauges.oldest_dead_letter_seconds,
        scale_hint=gauges.scale_hint(),
        kinds=list(JOB_KINDS),
    )


# ── 入队 ─────────────────────────────────────────────────────────────────────


@router.post(
    "/tasks",
    response_model=TaskAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交长任务（报表/Agent/知识库重建），立刻返回 job_id",
)
async def submit_task(
    body: TaskRequest,
    request: Request,
    principal: Any = Depends(deps.require_principal),
) -> Any:
    """入队一个长任务（`12.queue.md` §8 第 1 条：长任务秒级返回 job_id）。

    `session_id`（可选）只被 `agent` 任务用到：它决定 `thread_id` —— 与
    `POST /api/chat` **完全同一条派生规则**（`deps.thread_id_for`），
    于是「队列里跑的 Agent」与「HTTP 里跑的 Agent」落在同一个会话上。

    返回 202 + `job_id`；`created=False` 表示**幂等命中**（同一个幂等键之前已经投过，
    本次没有重复入队）—— 客户端据此知道「不要重复等一个新任务」。
    """
    actor = deps.actor_from_principal(
        principal,
        session_id=deps.resolve_session_id(body.session_id),
        request_id=str(getattr(request.state, "request_id", "")),
    )
    # 内部专属任务：**在这里判角色**（而不是在 schemas 里）——
    # 授权是路由的职责，而 schemas 只描述「请求长什么样」。
    if body.kind in INTERNAL_KINDS and not actor.internal:
        raise ApiError(
            403,
            "forbidden",
            f"{body.kind} 是内部操作：它会重建所有人的知识库索引，普通客户不能触发",
            detail={"kind": body.kind, "required": "read:all_orders"},
            log_level=logging.WARNING,
        )

    queue = await queue_for(request)
    try:
        # ⚠️ `enqueue_kwargs` **必须在 try 里**：它自己会做契约校验（例如 agent 任务
        # 缺 `message` 时抛 ValueError）—— 放在 try 外面，那个 ValueError 会一路冒到
        # 500（本仓实测踩到，见 runbook §10.1），而它本该是 422。
        kwargs = body.enqueue_kwargs(actor)
        outcome = await dispatch_enqueue(body.kind, queue=queue, **kwargs)
    except QueueUnavailable as exc:
        # 可用性问题：503 + Retry-After（不是 500 —— 那会让人以为代码坏了、去查代码）
        raise ApiError(
            503,
            "queue_unavailable",
            f"任务队列暂时不可用：{exc}",
            headers={"Retry-After": "30"},
            log_level=logging.ERROR,
        ) from exc
    except ValueError as exc:
        # 入参/幂等键的问题：422（调用方能自己修）
        raise ApiError(422, "invalid_task", str(exc), log_level=logging.WARNING) from exc

    logger.info(
        "任务已受理：kind=%s job_id=%s created=%s user=%s",
        body.kind,
        outcome.job_id,
        outcome.created,
        principal.user_id,
    )
    return TaskAcceptedResponse(
        job_id=outcome.job_id,
        kind=body.kind,
        status=outcome.status,
        created=bool(outcome.created),
        deferred_seconds=float(outcome.defer_by),
        poll_after_seconds=POLL_HINT_SECONDS,
        status_url=f"{api_prefix_of(request)}/tasks/{outcome.job_id}",
    )


# ── 查状态 ───────────────────────────────────────────────────────────────────


@router.get(
    "/tasks/{job_id}",
    response_model=TaskStatusResponse,
    summary="查询长任务状态与结果",
)
async def task_status(
    job_id: str,
    request: Request,
    principal: Any = Depends(deps.require_principal),
) -> Any:
    """查任务状态（含执行结果，如果已经完成且还在保留期内）。

    **越权按 404 处理**（理由见模块 docstring 第 3 条）。
    """
    queue = await queue_for(request)
    try:
        state = await queue.status(job_id)
        result = await _safe_result(queue, job_id) if state == "complete" else None
        # ⚠️ 归属判定**必须在任何状态下都做**（不只是 complete）：
        # 排队中的任务没有结果，但「别人正在跑什么」同样不该被读到。
        # `owner()` 从入队参数里读发起人（`RedisJobQueue.owner`），跑完之后回落到结果里的 `user_id`。
        owner = await _owner(queue, job_id, result)
    except QueueUnavailable as exc:
        raise ApiError(503, "queue_unavailable", f"任务队列暂时不可用：{exc}", log_level=logging.ERROR) from exc

    if owner is not None and int(owner) != int(principal.user_id) and not _is_internal(principal):
        logger.warning("任务查询越权（对外按 404）：job=%s owner=%s requester=%s", job_id, owner, principal.user_id)
        raise ApiError(404, "not_found_or_forbidden", "未找到该任务，或你没有访问它的权限")

    dead_letter = await _dead_letter_view(queue, job_id) if state == "dead_letter" else None
    return TaskStatusResponse(
        job_id=job_id,
        status=state,
        description=describe_state(state),
        result=result,
        dead_letter=dead_letter,
    )


async def _safe_result(queue: Any, job_id: str) -> dict[str, Any] | None:
    """取任务结果；取不到（过期 / 被清理）时返回 `None` 而不是抛错。

    ⚠️ 「结果没了」与「任务失败」是两件事：结果在 Redis 里只留
    `jobs.RESULT_TTL_SECONDS`（24 小时）。第二天来查会拿到 `complete` 而结果为空 ——
    如实返回「完成了但结果已过期」比抛 404 更准确（任务确实跑过）。
    """
    try:
        value = await queue.result(job_id)
    except Exception as exc:  # noqa: BLE001 - 结果可能已过期（ResultNotFound）或反序列化失败
        logger.info("任务结果不可读（可能已过期）：job_id=%s 原因=%s", job_id, exc)
        return None
    return dict(value) if isinstance(value, dict) else {"value": value}


async def _owner(queue: Any, job_id: str, result: dict[str, Any] | None) -> int | None:
    """这份任务属于谁：**先问队列的入队参数，再回落到结果里的 `user_id`**。

    两个来源都要，因为它们覆盖不同的时刻：

    | 时刻 | 队列里还有 `arq:job:*`？ | 有结果？ | 用哪个 |
    |---|---|---|---|
    | 排队中 / 执行中 | ✅ | ❌ | `queue.owner()`（入队参数） |
    | 已完成（24h 内） | ❌（已删） | ✅ | 结果里的 `user_id` |
    | 结果也过期了 | ❌ | ❌ | 都拿不到 → 不判（此时也**没有数据可泄漏**） |

    ⚠️ 第一版只从结果里读，于是「排队中的任务」的越权判定**静默失效**：
    只要拿到 job_id 就能一直读别人的任务状态，直到它跑完才被挡住
    （而那时数据已经出去了）。这是 runbook §10.1 里记的一个真实坑。
    """
    try:
        owner = await queue.owner(job_id)
    except Exception as exc:  # noqa: BLE001 - 归属读不到不该让状态查询 500
        logger.warning("读任务归属失败（%s）：%s", job_id, exc)
        owner = None
    if owner is not None:
        return int(owner)
    if not result:
        return None
    raw = result.get("user_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _is_internal(principal: Any) -> bool:
    """内部角色可以查任何任务（与 `deps.Actor.internal` 同一条判据）。"""
    from app.security.rbac import PERM_READ_ALL_ORDERS, has_permission

    return has_permission(str(getattr(principal, "role", "")), PERM_READ_ALL_ORDERS)


async def _dead_letter_view(queue: Any, job_id: str) -> DeadLetterView | None:
    """死信任务的失败现场（给运维用；**不回传 traceback 全文**）。"""
    store = getattr(queue, "_dead_letters", None)
    if store is None:
        return None
    try:
        entry = store.fetch(job_id)
    except Exception as exc:  # noqa: BLE001 - 死信表读不到不该让状态查询 500
        logger.warning("读死信记录失败（%s）：%s", job_id, exc)
        return None
    if entry is None:
        return None
    return DeadLetterView(
        attempts=int(entry.attempts),
        error_type=str(entry.error_type),
        error_message=str(entry.error_message)[:500],
        retryable=bool(entry.retryable),
        failed_at=entry.failed_at,
        replay_count=int(entry.replay_count),
        resolution=str(entry.resolution),
        replay_hint=f"uv run python -m scripts.queue_replay --job-id {entry.job_id}",
    )


__all__ = [
    "INTERNAL_KINDS",
    "POLL_HINT_SECONDS",
    "api_prefix_of",
    "queue_for",
    "queue_status",
    "router",
    "submit_task",
    "task_status",
]
