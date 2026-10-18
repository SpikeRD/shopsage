# 模块 12 · queue（异步任务 / 重试分级 / 死信）

## 目的

把"几秒钟到几分钟"的活（报表、Agent 回合、知识库重建）移出请求路径：**秒级入队返回 `job_id`**，worker 慢慢跑，失败按分级重试，最终失败落**业务库**的死信表，可人工重放。

## 关键结论

1. **三种任务**：`report` / `agent` / `kb_rebuild`（`app/queue/jobs.py:43-54`），函数名 `shopsage.run_report` / `shopsage.run_agent_turn` / `shopsage.rebuild_knowledge_base`。
2. **幂等键 → job_id**：`derive_key()`（`app/queue/jobs.py:253`）+ `job_id_for()`（:277，前缀 `q_`）。同一键重投**不会跑两遍**，入队结果用 `EnqueueOutcome.created` 一个字段同时回答"投进去了吗 / 是不是重复"。
3. **重试分级**：`DEFAULT_POLICIES`（`app/queue/jobs.py:169`）按函数给不同 `RetryPolicy`；`MAX_ATTEMPTS = max(attempts) + 1`（`app/queue/worker.py:81`）。
4. **死信落 Postgres 而不是留 Redis**：表 `queue_dead_letters`（迁移 `20261011_7164220823a3`），存储实现 `PostgresDeadLetterStore`（`app/queue/store.py:124`）。原因：Redis 里的失败记录会随 TTL 消失，而"失败到最后一步"必须能被人查到。
5. **`bridge_job`**（`app/queue/worker.py:169`）把内核的 `execute_job` 结果（`JobOutcome`）翻成 arq 的动作；`classify(exc)`（`app/queue/core.py:162`）决定"重试还是终态"。
6. **可注入**：`create_app(queue=...)` 不传时走 `REDIS_URL` 的默认队列；传内存队列可以**离线**验完 202 + 幂等（`app/api/main.py:229-232`）。
7. **API 面**：`POST /api/tasks` → 202 + `job_id`（`app/api/tasks.py:139`）；`GET /api/tasks/{job_id}`（:216）；`GET /api/tasks/queue`（:101）。**`POST /api/tasks` 必须在 `LIMITED_SCOPES` 里登记**，否则完全不限流（`app/api/middleware.py:63-68` 的长注释）。

## 实际用法

```python
# 入队（API 里只剩这三行）
from app.queue import enqueue_report, enqueue_agent_run, enqueue_kb_rebuild
outcome = await enqueue_report(queue, payload, idempotency_key="...")

# worker 入口（容器里就是这条命令）
# docker-compose.yml:88 → arq app.queue.worker.WorkerSettings
```

| 场景 | 表/类 | 依据 |
|---|---|---|
| 任务状态 | `JobState = queued/deferred/in_progress/complete/not_found/unknown/dead_letter` | `app/queue/backend.py:64` |
| 结果保留 | `RESULT_TTL_SECONDS = 86400` | `app/queue/jobs.py:183` |
| 任务超时 | `JOB_TIMEOUT_SECONDS = 300` | `app/queue/jobs.py:179` |
| 人工重放 | `replay_plan()` | `app/queue/core.py:486` |
| 死信巡检 | `dead_letter_watch()` 每 60s | `app/queue/worker.py:321` |

## 待办 / 已知边界

- ⚠️ `queue_depth` 等指标的**采集点**在 `record_queue_gauges()`（`app/queue/worker.py:351`），只在 worker 里跑 —— 若 worker 未起，队列深度指标为空。这是设计，不是 bug，但排障时容易误判"队列没积压"。
- `docs/runbook/12.queue-runbook.md` 1620 行、`docs/modules/12.queue.md` 44 行（后者极简，信息量低于前者一个数量级）。

## 坑

| 坑 | 后果 | 依据 |
|---|---|---|
| 任务函数名与 worker 实现不一致 | 入队成功但永远执行不到 | `docs/runbook/17.docker-ops.md:115-117`（所以 api 与 worker 共用一个镜像） |
| 只登记 `scope.py` 不登记 `LIMITED_SCOPES` | 额度配了但不生效 | `app/api/middleware.py:69-76` |
| 默认队列按事件循环缓存 | 同一进程起第二个 app → `RuntimeError: Event loop is closed` | `app/api/main.py:277-284` |

## 来源

- `docs/modules/12.queue.md`（44 行）、`docs/runbook/12.queue-runbook.md`（1620 行）
- `docs/problem-solving.md` A1/A2
- 代码：`app/queue/*.py`；测试：`tests/queue/*.py`
