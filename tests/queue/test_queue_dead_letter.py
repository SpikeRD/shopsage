"""死信表（Postgres）的用例：**离线部分始终跑，真库部分打 `db` 标记**。

`12.queue.md` §6 第 3 步要的是「超限任务写 `dead_letter` 队列 + 落库记录」，
而「落库」这件事只有连真库才算验过 —— 所以本文件分两层：

* **离线层**（默认跑）：`to_row` / `to_record` 的往返、`retryable` 的两种取值、
  payload 原样保留、`resolution` 不覆盖 —— 全是纯函数与内存 store；
* **真库层**（`@pytest.mark.db`）：真写一行、真读回来、真标记已处理。
  连不上库时 `tests/conftest.py` 会自动 skip 并说明原因（不会让套件变红）。

⚠️ 真库层的**第一条**用例就抓过一个真实的坑：`sqlalchemy.insert(...)` 返回的是
通用 `Insert`，它**没有** `on_conflict_do_update()`（那是 PostgreSQL 方言的 API）——
而那行代码在离线用例里永远跑不到（内存 store 不走 SQL）。这就是「落库要有真库用例」
的直接理由（见 runbook §10.1）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.queue.core import DeadLetterRecord, PermanentTaskError, RetryableToolError, make_dead_letter_record
from app.queue.jobs import FUNC_KB_REBUILD, FUNC_REPORT, RetryPolicy
from app.queue.store import DEFAULT_PAGE, PostgresDeadLetterStore, to_record, to_row

# ── 离线：行的组装与还原 ─────────────────────────────────────────────────────


def test_row_round_trip_keeps_every_field():
    """`to_row` → `to_record` 必须**逐字段**还原（重放依赖它：payload 要原样喂回）。"""
    entry = make_dead_letter_record(
        job_id="q_report:u2:abc",
        function=FUNC_REPORT,
        kind="report",
        payload={"user_id": 2, "title": "本月", "nested": {"a": [1, 2]}},
        exc=RetryableToolError("下游 503"),
        attempts=3,
        policy=RetryPolicy(attempts=3, base_delay=2.0, max_delay=120.0, jitter=0.2),
        retryable=True,
        user_id=2,
    )
    row = to_row(entry)
    back = to_record(row)
    assert back.job_id == entry.job_id
    assert back.payload == entry.payload  # 嵌套结构也要一样（JSONB 的往返）
    assert back.attempts == 3
    assert back.error_type == "RetryableToolError"
    assert back.retryable is True
    assert back.policy is not None and back.policy.base_delay == 2.0
    assert back.user_id == 2
    assert "RetryableToolError" in back.error_traceback


def test_row_marks_permanent_failures_as_not_retryable():
    """不可恢复的失败 `retryable=False` —— 它决定人工看到记录时的第一个判断。"""
    entry = make_dead_letter_record(
        job_id="q_x",
        function=FUNC_KB_REBUILD,
        kind="kb_rebuild",
        payload={},
        exc=PermanentTaskError("目录不存在"),
        attempts=1,
        policy=RetryPolicy(attempts=1, base_delay=0.0, max_delay=0.0, jitter=0.0),
        retryable=False,
    )
    row = to_row(entry)
    assert row["retryable"] is False
    assert row["policy"]["attempts"] == 1


def test_to_record_tolerates_an_empty_policy():
    """`policy` 为空时不能炸（老记录/人工插入的行没有这个字段）。"""
    record = to_record(
        {
            "job_id": "q_y",
            "function": FUNC_REPORT,
            "kind": "report",
            "payload": {},
            "attempts": 1,
            "policy": None,
            "failed_at": datetime.now(timezone.utc),
        }
    )
    assert record.policy is None
    assert record.replay_key == "y"  # 去掉了 `q_` 前缀（重放要用原键）


def test_replay_key_strips_only_the_prefix():
    """重放键 = job_id 去掉 `q_` 前缀（不是「截前两个字符」——那依赖前缀恰好两个字符）。"""
    assert DeadLetterRecord(job_id="q_abc", function="f", kind="report").replay_key == "abc"
    assert DeadLetterRecord(job_id="abc", function="f", kind="report").replay_key == "abc"


# ── 真库：写一行、读回来、标已处理 ───────────────────────────────────────────


@pytest.fixture
def store() -> PostgresDeadLetterStore:
    return PostgresDeadLetterStore()


@pytest.fixture
def clean_smoke_rows():
    """清掉本文件自己的测试行（**不碰别的行**：演示/生产留下的死信要留着看）。"""
    from sqlalchemy import text

    from app.db.session import engine

    prefix = "q_test:runbook:%"

    def _clean() -> None:
        with engine.begin() as conn:
            conn.execute(text("delete from queue_dead_letters where job_id like :p"), {"p": prefix})

    _clean()
    yield prefix
    _clean()


@pytest.mark.db
def test_postgres_store_round_trip(store, clean_smoke_rows):
    """真库往返：写 → 读 → 标记已处理，三次都成功。"""
    job_id = "q_test:runbook:roundtrip"
    entry = make_dead_letter_record(
        job_id=job_id,
        function=FUNC_REPORT,
        kind="report",
        payload={"user_id": 2, "title": "本月"},
        exc=RetryableToolError("下游 503"),
        attempts=3,
        policy=RetryPolicy(attempts=3, base_delay=2.0, max_delay=120.0, jitter=0.2),
        retryable=True,
        user_id=2,
    )
    assert store.record(entry) is True, "第一次写入应当是新建"
    assert store.record(entry) is False, "第二次写入应当命中唯一键（只更新现场）"

    got = store.fetch(job_id)
    assert got is not None
    assert got.payload == {"user_id": 2, "title": "本月"}
    assert got.attempts == 3
    assert got.retryable is True
    assert got.policy is not None and got.policy.attempts == 3

    assert store.resolve(job_id, resolution="replayed_ok", replayed_job_id="q_test:runbook:replay") is True
    resolved = store.fetch(job_id)
    assert resolved is not None
    assert resolved.resolution == "replayed_ok"
    assert resolved.replay_count == 1
    assert resolved.replayed_job_id == "q_test:runbook:replay"
    assert resolved.resolved_at is not None


@pytest.mark.db
def test_postgres_store_does_not_overwrite_the_resolution(store, clean_smoke_rows):
    """重复写入**不能**抹掉「人工已处理」的状态（否则死信会重新冒出来）。

    这是 `ON CONFLICT DO UPDATE` 的列白名单（`_REWRITABLE`）在真库上的验证：
    它允许覆盖失败现场，但**不允许**覆盖 `resolution` / `replay_count` / `replayed_job_id`。
    """
    job_id = "q_test:runbook:nooverwrite"
    first = make_dead_letter_record(
        job_id=job_id,
        function=FUNC_REPORT,
        kind="report",
        payload={"user_id": 2},
        exc=RetryableToolError("第一次失败"),
        attempts=3,
        policy=RetryPolicy(),
        retryable=True,
    )
    store.record(first)
    store.resolve(job_id, resolution="wont_fix")

    # 同一条死信被第二次失败写入（attempts 变大、错误变了）
    second = make_dead_letter_record(
        job_id=job_id,
        function=FUNC_REPORT,
        kind="report",
        payload={"user_id": 2},
        exc=PermanentTaskError("第二次失败"),
        attempts=1,
        policy=RetryPolicy(attempts=1),
        retryable=False,
    )
    store.record(second)

    got = store.fetch(job_id)
    assert got is not None
    assert got.error_type == "PermanentTaskError"  # 失败现场被更新了
    assert got.attempts == 1
    assert got.resolution == "wont_fix"  # ← 人工状态**没被**抹掉（本用例的重点）
    assert got.replay_count == 1


@pytest.mark.db
def test_postgres_store_pending_excludes_resolved(store, clean_smoke_rows):
    """`pending()` 默认不返回已处理的：重放脚本的默认行为是「把待处理的都跑一遍」。"""
    for suffix in ("a", "b"):
        store.record(
            make_dead_letter_record(
                job_id=f"q_test:runbook:{suffix}",
                function=FUNC_REPORT,
                kind="report",
                payload={},
                exc=PermanentTaskError("x"),
                attempts=1,
                policy=RetryPolicy(attempts=1),
                retryable=False,
            )
        )
    store.resolve("q_test:runbook:a", resolution="wont_fix")

    pending_ids = {entry.job_id for entry in store.pending(limit=DEFAULT_PAGE)}
    assert "q_test:runbook:b" in pending_ids
    assert "q_test:runbook:a" not in pending_ids
    all_ids = {entry.job_id for entry in store.pending(limit=DEFAULT_PAGE, include_resolved=True)}
    assert {"q_test:runbook:a", "q_test:runbook:b"} <= all_ids


@pytest.mark.db
def test_postgres_store_reports_availability(store):
    """`available()` 在库可达时为 True（`/health` 与 worker 自检都用它）。"""
    assert store.available() is True


@pytest.mark.db
async def test_dead_letter_end_to_end_against_the_real_table(clean_smoke_rows):
    """端到端：任务失败 → **真库**里有一行 → 重放成功 → 那一行被标成 `replayed_ok`。

    这条把三层串起来（内核 → 桥 → 真库），是本步「零任务丢失 + 可人工重放」的
    最强证据。它用内存队列跑任务（快、可控），但死信走**真表**。
    """
    from app.queue.core import replay_plan
    from app.queue.jobs import JobSpec
    from app.queue.tasks import enqueue_report
    from tests.queue.fake_queue import MemoryJobQueue

    store = PostgresDeadLetterStore()
    queue = MemoryJobQueue(dead_letters=store)
    queue.runners[FUNC_REPORT] = queue.make_runner(FUNC_REPORT, lambda payload: {"title": payload.get("title")})

    class Boom(RuntimeError):
        """「没见过的异常」= 不可恢复（重试无意义）。"""

    queue.inject_failure(FUNC_REPORT, Boom("代码有 bug（不可恢复）"))
    outcome = await enqueue_report(user_id=2, title="真库死信", idempotency_key="test:runbook:e2e", queue=queue)
    await queue.run_until_idle()
    assert await queue.status(outcome.job_id) == "dead_letter"

    row = store.fetch(outcome.job_id)
    assert row is not None, "真库里应当有一行（这是「零任务丢失」的落点）"
    assert row.kind == "report"
    assert row.payload["user_id"] == 2
    assert "Boom" in row.error_type

    plan = replay_plan(row)
    await queue.enqueue(
        JobSpec(kind="report", function=plan.function, payload=plan.payload, idempotency_key=plan.idempotency_key)
    )
    await queue.run_until_idle()
    closed = store.fetch(outcome.job_id)
    assert closed is not None
    assert closed.resolution == "replayed_ok"
    assert closed.replay_count == 1
