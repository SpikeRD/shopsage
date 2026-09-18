# queue — 异步任务 / 重试 / 延迟任务 / 死信队列（Arq）

## 1. 职责

承载**长任务**（报表生成、批量分析、知识库重建、长链路 Agent），与 API 解耦：
API 秒级返回任务 id，worker 异步执行，失败自动重试，重试仍失败进死信队列人工兜底。

## 2. 对外接口

```python
from app.queue.tasks import enqueue_report, enqueue_agent_run
from app.queue.worker import WorkerSettings

job = await enqueue_report(user_id=1, params={...})   # 返回 job_id，API 立刻返回
```

## 3. 依赖

- `arq`（基于 Redis 的异步任务队列）
- `redis`（任务存储 + 延迟队列 + 结果）
- `graph.md`（Agent 长任务以图的方式跑，checkpoint 存进度）
- 读 `config` 的 `redis_url`

## 4. 关键设计决策

1. **API 与 worker 分离**：API 只入队、秒回；worker 是独立进程，可单独扩缩、单独重启（架构第 8 节）。
2. **任务 = 图的一次执行**：Agent 长任务在 worker 里跑 `graph.ainvoke`，节点状态进 checkpoint，失败从断点重放（A1+A2 结合）。
3. **重试分级**：可恢复错误（网络/限流）退避重试 N 次；不可恢复（参数错误）不重试直接进死信。
4. **死信队列人工兜底**：超过重试上限的任务进 `dead_letter`，带完整错误上下文，支持人工重放/排查，保证「压测下零任务丢失」。

## 5. 核心技术点（要懂为什么）

- **Arq 的 `defer_until` 实现延迟任务**：任务不进立即执行队列，而是带时间戳，worker 到点才取，用于「延迟重试」「定时报表」。
- **`_job_id` 与幂等**：任务入队带业务幂等 key，worker 重复投递/重放不会重复执行副作用。
- **为什么重试上限 + 死信缺一不可**：只重试不退避会打爆下游；只重试不死信会无限循环；死信把「失败」变成「可见、可人工兜底的事件」而非静默丢失。
- **背压**：队列积压可作为 worker 扩容信号（架构第 8 节「背压与削峰」）。

## 6. 开发步骤

1. 写 `WorkerSettings`：redis 连接、任务注册、重试参数。
2. 写 `tasks.py`：`enqueue_report` / `enqueue_agent_run`（内部调 `graph.ainvoke`）。
3. 写死信处理：超限任务写 `dead_letter` 队列 + 落库记录。
4. 写 `scripts/worker.py` 启动入口（`arq worker`）。
5. 写重放脚本：从死信/断点重放失败任务。

## 7. 测试用例

```python
def test_long_task_returns_job_id():     # API 入队后立刻返回 job_id，不阻塞
def test_retry_then_dead_letter():       # 连续失败 N 次 → 进死信队列，不丢失
def test_replay_from_dead_letter():      # 从死信重放，最终成功
def test_delayed_task_runs_on_time():    # defer_until 到期才执行
def test_idempotent_enqueue():           # 同一幂等 key 不重复执行
```

## 8. 验收标准

- [ ] 长任务秒级返回 job_id，worker 异步执行，API 不阻塞。
- [ ] 重试上限后进死信，零任务丢失，可人工重放（A1）。
- [ ] 与 `graph.md` 断点重放协同：中断后从断点恢复（A2）。
- [ ] 证据链接：`PR#__` / `压测报告#__`
