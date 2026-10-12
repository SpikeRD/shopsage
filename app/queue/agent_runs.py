"""任务实现：三个长任务**真正干活**的地方（`12.queue.md` §6 第 2 步）。

分工（这条边界是第 7、8 步一路保下来的）：

| 层 | 管什么 | 不管什么 |
|---|---|---|
| `app/queue/core.py` | 重试分级、死信兜底、重放 | 任务怎么算 |
| **本模块** | **三个任务各自的业务动作** | 怎么入队、怎么重试 |
| `app/queue/worker.py` | arq 进程、注册、把内核结论翻成 arq 动作 | 业务动作 |

三个任务刻意选了三种**不同形态**的长活，因为它们要证明的事情不同：

1. **`run_report`（报表生成）** —— 典型的「读 + 算 + 用模型写摘要」：
   演示「API 秒回 job_id、worker 慢慢算」；它的离线实现只需注入假模型与假查询；
2. **`run_agent_turn`（长链路 Agent）** —— `12.queue.md` §4 第 2 条「任务 = 图的一次执行」：
   在 worker 里跑 `graph.ainvoke` 那一套（这里用 `run_turn` 包一层，理由见下），
   状态进 checkpoint，失败从断点重放（A1+A2 结合）；
3. **`rebuild_knowledge_base`（知识库重建）** —— 长、贵、**幂等**（整库重算）：
   它是「重试额度给得最大」的那种任务（`jobs.DEFAULT_POLICIES`），
   也是「重放两次结果一样」这条性质的样板。

## 为什么 `run_agent_turn` 用 `run_turn` 而不是裸 `ainvoke`

`8.graph-runbook.md` §1.2 的第 3 条差异已经解释过：裸 `ainvoke` 也能跑，
但「重放 + 兜底」需要一个**回合边界**来放。队列这一层更在意的是另外两个东西：

* `Turn.exhausted` —— 重放预算用尽（这一轮「没做到」而不是「答完了」）。
  队列据此决定「这次算成功还是失败」：**没做到**就该重试/进死信，
  否则用户拿到一句兜底话术、而队列以为一切正常；
* `Turn.visited` / `Turn.replayed` —— 第 11 步的埋点要它们。

⚠️ 因此 `run_agent_turn` 的返回值里带 `exhausted`，并且**它决定成功与否**：
`exhausted=True` 时任务抛 `RetryableToolError`（交给内核按策略重试），
理由见 `_resolve_runner` 之后的那段。

## 注入点：三个任务都能离线跑

每个任务都接受一个可注入的 runner（`llm` / `graph_factory` / `ingest`）。
`enqueue_*` 的入参只带**可 JSON 化的数据**（payload），runner 是**进程内**的依赖 ——
所以「离线验队列语义」不需要真模型、真库、真 Redis（第 7、8 步的同一条纪律）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final, Protocol

from app.queue.core import PermanentTaskError, RetryTask
from app.tools.errors import RetryableToolError

logger = logging.getLogger(__name__)

#: 报表摘要最多写多少字（进 payload 与结果，都是要落 Redis 的东西）
REPORT_SUMMARY_CHARS: Final[int] = 400

#: 报表最多取多少行（与 `data` 节点的 `DEFAULT_ROW_LIMIT` 同量级；
#: 队列里的任务是**离线批处理**，没有用户在等，所以可以比交互路径大一些）
REPORT_ROW_LIMIT: Final[int] = 200

#: 知识库重建的默认目录（与 `scripts/load_knowledge_base.py` 同一个源）
DEFAULT_KB_DIR: Final[str] = "docs/kb"


class QueryRunner(Protocol):
    """报表任务取数用的注入点（真实现 = `app.tools.sql_executor.execute_readonly_query`）。"""

    def __call__(self, sql: str, params: Mapping[str, Any] | None = ..., **kwargs: Any) -> Any: ...


class AgentRunner(Protocol):
    """Agent 长任务的注入点（真实现 = 走 `app.graph` 的一轮）。"""

    def __call__(self, payload: Mapping[str, Any]) -> Awaitable[dict[str, Any]]: ...


class IngestRunner(Protocol):
    """知识库重建的注入点（真实现 = `scripts/load_knowledge_base`）。"""

    def __call__(self, payload: Mapping[str, Any]) -> Awaitable[dict[str, Any]]: ...


# ── ① 报表生成 ───────────────────────────────────────────────────────────────


async def run_report(payload: Mapping[str, Any], *, llm: Any = None, query: Any = None) -> dict[str, Any]:
    """生成一份报表（`kind="report"`）。

    payload（都是**可 JSON 化**的普通数据，因为要落 Redis）：

    | 键 | 必填 | 说明 |
    |---|---|---|
    | `user_id` | ✅ | 行级过滤的依据（`WHERE user_id = ...` 由程序拼，不由模型拼） |
    | `title` | ✅ | 报表标题 |
    | `sql` / `params` | ❌ | 数据来源；缺省用「该用户最近订单」这条固定查询 |
    | `summary_chars` | ❌ | 摘要长度上限 |

    ⚠️ 「**不要**让模型决定取哪些数据」是第 4 步 `sql_guard` 定的口径在队列里的复述：
    报表是长任务，但**长不等于可以放宽安全边界**。这里 SQL 由调用方给（缺省用内置查询），
    模型只负责把已经取到的行写成一段摘要。
    """
    from app.security.masking import mask_pii

    data = dict(payload or {})
    user_id = data.get("user_id")
    if user_id is None:
        # 不可恢复：没有 user_id 就不知道「按谁过滤」，重试 100 次也不会变好
        raise PermanentTaskError("报表任务缺少 user_id：行级过滤的依据只能来自已验签的身份")
    title = str(data.get("title") or "订单报表")
    sql = str(data.get("sql") or DEFAULT_REPORT_SQL)
    params = dict(data.get("params") or {})
    params.setdefault("uid", int(user_id))
    params.setdefault("n", int(data.get("row_limit") or REPORT_ROW_LIMIT))

    fetcher = query if query is not None else _default_query()
    rows, meta = _split_query_result(await _maybe_await(fetcher(sql, params)))
    logger.info("报表取数完成：user=%s 行数=%s 标题=%s", user_id, len(rows), title)

    summary = await _summarize(
        rows=rows,
        title=title,
        llm=llm,
        limit=int(data.get("summary_chars") or REPORT_SUMMARY_CHARS),
    )
    return {
        "title": title,
        "user_id": int(user_id),
        "row_count": len(rows),
        # ⚠️ 出参里的行**必须脱敏**：这份结果会进 Redis（24h）并被 API 回给调用方。
        # 第 4 步的 `mask_pii` 是对客输出的唯一口径，队列不能绕开它。
        "rows": _masked(rows)[:20],
        "summary": summary,
        "truncated": bool(meta.get("truncated", False)),
        "audit_id": str(meta.get("audit_id") or ""),
    }


#: 缺省报表查询（**固定字面量**：不接受模型生成，见 `run_report` 的说明）
DEFAULT_REPORT_SQL: Final[str] = (
    "SELECT order_id, status, total_amount, created_at FROM orders WHERE user_id = :uid "
    "ORDER BY created_at DESC LIMIT :n"
)


async def _summarize(*, rows: Sequence[Mapping[str, Any]], title: str, llm: Any, limit: int) -> str:
    """把已取到的行写成一段摘要（**模型只做措辞，不做取数**）。"""
    from app.agents.agent_llm import build_system_prompt, generate_json

    masked = _masked(rows)
    if not masked:
        return f"{title}：这段时间没有数据。"
    messages = [
        {
            "role": "system",
            "content": build_system_prompt(
                "报表助手：把已经取到的数据行写成一段给业务方看的中文摘要。"
                "只能使用给定的数据，不得推测、不得添加没有出现过的数字。",
                output_schema='{"summary": "一段中文摘要"}',
            ),
        },
        {"role": "user", "content": f"标题：{title}\n数据行：{masked[:REPORT_ROW_LIMIT]}"},
    ]
    result = await generate_json(messages, llm=llm, agent="report")
    text = " ".join(str(result.get("summary") or "").split())
    if not text:
        # 模型给了空摘要：**不抛错**（报表的数据部分仍然有价值），如实说明
        return f"{title}：共 {len(masked)} 行（本次没能生成摘要）。"
    return text[:limit]


def _masked(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按**对客口径**脱敏一组行（第 4 步的 `mask_pii` 是唯一实现）。

    队列里跑的是内部批处理，但**结果会出网**（API 的 `GET /api/tasks/{id}` 会把它回给
    发起人），所以口径必须与对客路径一致 —— 「内部任务所以不用脱敏」是最容易漏的一处。
    """
    from app.security.masking import mask_pii

    return mask_pii([dict(row) for row in rows]).rows


def _split_query_result(result: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把取数结果归一成 `(rows, meta)`。

    `app.tools.sql_executor.execute_readonly_query` 返回 `(rows, meta)` 二元组，
    而假实现（测试/演示）最自然的写法是「直接返回 rows」—— 两种都吃，
    省得每个调用点都写一遍判型（那种「两处都知道」的代码最容易漏掉一处）。
    """
    if isinstance(result, tuple) and len(result) == 2:
        rows, meta = result
        return [dict(r) for r in (rows or [])], dict(meta or {})
    return [dict(r) for r in (result or [])], {}


# ── ② Agent 长任务（任务 = 图的一次执行）─────────────────────────────────────


async def run_agent_turn(payload: Mapping[str, Any], *, graph_factory: Any = None) -> dict[str, Any]:
    """在 worker 里跑一轮 Agent（`kind="agent"`，`12.queue.md` §4 第 2 条）。

    | payload 键 | 必填 | 说明 |
    |---|---|---|
    | `thread_id` | ✅ | checkpoint 的隔离键；**必须来自鉴权结果**（这里是队列，没有鉴权 —— 所以由入队的调用方负责，见 §5.3） |
    | `message` | ✅ | 用户这一轮说的话 |
    | `user_id` / `role` | ✅ | 进图 state 的身份（`inject_identity`） |
    | `replay_budget` | ❌ | 透传给 `run_turn` 的断点重放预算 |

    **返回值里的 `exhausted` 决定这次任务算不算成功**：`run_turn` 的语义是
    「永远给你一个 Turn」（它把失败兜底成一句实话，用户不会看到 500）——
    但对**队列**来说，「兜底话术」不等于「任务成功」。所以这里把
    `exhausted=True`（重放预算用尽）翻成 `RetryableToolError`，
    交给内核按策略重试 → 重试用尽 → 进死信。这条翻译是本文件最要紧的一处：
    少了它，「重放修不好的失败」会静默地变成「成功的任务」，而死信队列永远是空的。
    """
    from app.agents.state import inject_identity
    from app.graph import run_turn

    data = dict(payload or {})
    thread_id = str(data.get("thread_id") or "").strip()
    message = str(data.get("message") or "").strip()
    if not thread_id:
        raise PermanentTaskError("Agent 任务缺少 thread_id：checkpoint 的隔离键不能由队列自己编")
    if not message:
        raise PermanentTaskError("Agent 任务缺少 message：没有输入的一轮没有意义")
    if data.get("user_id") is None:
        raise PermanentTaskError("Agent 任务缺少 user_id：身份只能来自已验签的结果")

    state = {"messages": [{"role": "user", "content": message}], **inject_identity(int(data["user_id"]), str(data.get("role") or "customer"))}

    factory = graph_factory if graph_factory is not None else _default_graph_factory()
    async with factory() as graph:
        turn = await run_turn(
            graph,
            state,
            thread_id=thread_id,
            replay_budget=int(data.get("replay_budget") or 1),
        )
    if turn.exhausted:
        raise RetryableToolError(
            "Agent 这一轮重放预算用尽，没有拿到结论（队列按可恢复错误重投）："
            f"thread={thread_id} 路径={'→'.join(turn.visited)}"
        )
    if turn.pending_approval:
        # 挂起等人工批准**不是失败**：任务成功结束，等待批准通道把它推下去。
        # 明确区分出来（而不是让它走 exhausted 分支）是为了让死信队列不被
        # 「等人批准」的噪音灌满。
        logger.info("Agent 任务停在人工审批闸门：thread=%s", thread_id)
    return {
        "thread_id": thread_id,
        "answer": turn.answer,
        "blocked": bool(turn.blocked),
        "exhausted": bool(turn.exhausted),
        "pending_approval": turn.pending_approval,
        "visited": list(turn.visited),
        "replayed": list(turn.replayed),
    }


# ── ③ 知识库重建（幂等长任务）───────────────────────────────────────────────


async def rebuild_knowledge_base(payload: Mapping[str, Any], *, ingest: Any = None) -> dict[str, Any]:
    """整库重建向量索引（`kind="kb_rebuild"`）。

    | payload 键 | 必填 | 说明 |
    |---|---|---|
    | `source_dir` | ❌ | 语料目录，缺省 `docs/kb` |
    | `force` | ❌ | 是否连没变的文档也重算（默认 False：内容哈希没变就跳过） |

    **为什么它值得 5 次重试**：整库重建是长任务（几分钟）、贵（每次全文 embedding），
    而它是**幂等**的（重跑一遍得到同一份索引）—— 幂等 + 贵，意味着「宁可多等几轮，
    也别让它落到人工队列里」。这是 `jobs.DEFAULT_POLICIES` 给它 `attempts=5` 的理由。
    """
    data = dict(payload or {})
    source_dir = str(data.get("source_dir") or DEFAULT_KB_DIR)
    runner = ingest if ingest is not None else _default_ingest()
    stats = await _maybe_await(runner({"source_dir": source_dir, "force": bool(data.get("force"))}))
    result = dict(stats or {})
    result.setdefault("source_dir", source_dir)
    logger.info("知识库重建完成：%s", result)
    return result


# ── 默认实现（生产路径；测试与演示**不**走这里）──────────────────────────────


def _default_query() -> Any:
    from app.tools.sql_executor import execute_readonly_query

    return execute_readonly_query


def _default_graph_factory() -> Any:
    """生产图的工厂：真 checkpointer + 真模型（`GraphRuntime()` 的默认值）。"""
    from app.graph import GraphRuntime, compile_graph, open_checkpointer
    from app.graph.checkpointer import CheckpointBackend

    backend: CheckpointBackend = "postgres"

    import contextlib

    @contextlib.asynccontextmanager
    async def factory() -> Any:
        async with open_checkpointer(backend) as saver:
            yield compile_graph(saver, GraphRuntime())

    return factory


def _default_ingest() -> Any:
    """生产的知识库重建实现：`app/rag/ingest.ingest_path`（第 10 步的入口）。

    适配层只有一行（把 payload 展开成入参、把 dataclass 报告翻成 dict），
    但它必须存在：`rebuild_knowledge_base` 的契约是「吃 payload、吐 JSON 化的 dict」，
    而第 10 步的入口吃的是 `root` 关键字参数 —— 两者之间不该让任一方妥协。
    """

    async def ingest(payload: Mapping[str, Any]) -> dict[str, Any]:
        from app.rag.ingest import ingest_path

        report = await ingest_path(str(payload.get("source_dir") or DEFAULT_KB_DIR), force=bool(payload.get("force")))
        return dict(report.as_dict())

    return ingest


async def _maybe_await(value: Any) -> Any:
    """`value` 是等待对象就 await（同步/异步 runner 都能注入）。"""
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


#: 任务的入参契约（CLI/API 的自检用：缺哪个键会在**入队前**就报出来）
REQUIRED_PAYLOAD_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "report": ("user_id",),
    "agent": ("thread_id", "message", "user_id"),
    "kb_rebuild": (),
}


def check_payload(kind: str, payload: Mapping[str, Any]) -> None:
    """入队前的入参自检（**fail-fast 在 API 层**，而不是等 worker 里抛 PermanentTaskError）。

    两种做法的区别不在「能不能拦住」，而在**谁承担代价**：
    在 API 层拦 = 调用方立刻拿到 422 并知道缺什么；在 worker 里拦 = 一个任务白跑一趟、
    进死信、再由人工去看「原来少传了 user_id」。后者把一次请求形状错误变成了运维事件。
    """
    missing = [key for key in REQUIRED_PAYLOAD_KEYS.get(kind, ()) if not str(payload.get(key) or "").strip()]
    if missing:
        raise ValueError(f"{kind} 任务缺少必填参数：{', '.join(missing)}")


__all__ = [
    "DEFAULT_KB_DIR",
    "DEFAULT_REPORT_SQL",
    "REPORT_ROW_LIMIT",
    "REPORT_SUMMARY_CHARS",
    "REQUIRED_PAYLOAD_KEYS",
    "check_payload",
    "rebuild_knowledge_base",
    "run_agent_turn",
    "run_report",
]
