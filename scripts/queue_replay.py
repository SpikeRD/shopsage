"""人工重放 / 排查死信队列（`12.queue.md` §6 第 5 步的运维工具）。

```powershell
uv run python -m scripts.queue_replay --list                    # 看有哪些待处理的死信
uv run python -m scripts.queue_replay --list --all              # 连已处理过的一起看
uv run python -m scripts.queue_replay --show <job_id>           # 看一条的完整现场（含 traceback）
uv run python -m scripts.queue_replay --job-id <job_id>         # 重放一条
uv run python -m scripts.queue_replay --all --limit 5           # 批量重放（先失败的先来）
uv run python -m scripts.queue_replay --all --dry-run           # 只看会重放什么，不动手
uv run python -m scripts.queue_replay --job-id <id> --rerun     # 显式再跑一次（新幂等键）
uv run python -m scripts.queue_replay --resolve <id> --as wont_fix   # 不重放，只标记已处理
```

## 四个刻意的行为

1. **默认 `--list` 只列不跑**。重放会真的执行任务（可能调模型、可能写库），
   所以「什么都不做」必须是默认动作 —— 一个 `python -m scripts.queue_replay`
   就把所有死信重跑一遍的工具，是最容易造成二次事故的那种工具。
2. **默认复用原幂等键**（`core.replay_plan`）。于是「点两次」只会跑一次，
   而写路径的副作用不会重复（A2）。真要再跑一遍必须显式 `--rerun`
   （它给键加 `retryN` 后缀，重放次数在 job_id 上是**看得见**的）。
3. **`--as` 的取值是一张白名单**（`replayed_ok` / `replayed_failed` / `wont_fix` / `manual_fix`）。
   自由文本的 resolution 会让「这条死信是怎么收场的」变成一次人工考古。
4. **`--dry-run` 也要求 `--job-id` 或 `--all`**：只给 `--dry-run` 时它需要先知道
   「要干跑哪一批」。批量时默认取 `--limit` 条待处理（先失败的先来）。

⚠️ 这个脚本**不启动 worker**：它只负责把任务重新投进队列（`enqueue`）。
   真正执行要有一个在跑的 `arq app.queue.worker.WorkerSettings` 进程 ——
   投完立刻 `--list` 会看到「已重放、等 worker 执行」（`replayed_job_id` 已写上）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.queue.backend import DEFAULT_QUEUE_NAME, RedisJobQueue
from app.queue.core import DeadLetterRecord, render_entry, replay_plan
from app.queue.jobs import JobSpec
from app.queue.store import PostgresDeadLetterStore

#: 允许的收场方式（白名单，见模块 docstring 第 3 条）
RESOLUTIONS: tuple[str, ...] = ("replayed_ok", "replayed_failed", "wont_fix", "manual_fix")

#: `--resolve` 的默认收场方式
DEFAULT_RESOLUTION: str = "manual_fix"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="死信队列的排查与人工重放")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true", help="列出死信（默认动作）")
    action.add_argument("--show", metavar="JOB_ID", help="显示一条死信的完整现场（含 traceback）")
    action.add_argument("--job-id", metavar="JOB_ID", help="重放指定的一条")
    action.add_argument("--resolve", metavar="JOB_ID", help="不重放，只把它标记为已处理")
    action.add_argument("--all", action="store_true", help="重放全部待处理的（可配 --limit）")
    parser.add_argument("--limit", type=int, default=10, help="批量/列表的上限（默认 10）")
    parser.add_argument("--include-resolved", action="store_true", help="列表里连已处理的也显示")
    parser.add_argument("--rerun", action="store_true", help="显式再跑一次（幂等键加 retryN 后缀）")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要做什么，不真的入队")
    parser.add_argument("--as", dest="resolution", choices=RESOLUTIONS, default=DEFAULT_RESOLUTION,
                        help=f"`--resolve` 的收场方式（默认 {DEFAULT_RESOLUTION}）")
    parser.add_argument("--queue-name", default=DEFAULT_QUEUE_NAME, help="队列名（默认与 worker 一致）")
    return parser


async def run(args: argparse.Namespace) -> int:
    store = PostgresDeadLetterStore()
    if not store.available():
        print("[FAIL] 死信表不可达：先跑 `uv run alembic upgrade head`（并检查 DATABASE_URL）")
        return 2

    if args.show:
        entry = store.fetch(args.show)
        if entry is None:
            print(f"[FAIL] 没有这条死信：{args.show}")
            return 2
        render_detail(entry)
        return 0

    if args.resolve:
        entry = store.fetch(args.resolve)
        if entry is None:
            print(f"[FAIL] 没有这条死信：{args.resolve}")
            return 2
        ok = store.resolve(args.resolve, resolution=args.resolution)
        print(f"{'已标记' if ok else '未改动'}：{args.resolve} → {args.resolution}")
        return 0 if ok else 1

    pending = store.pending(limit=args.limit, include_resolved=args.include_resolved)
    if not args.job_id and not args.all:
        render_list(pending, include_resolved=args.include_resolved)
        return 0

    targets = [store.fetch(args.job_id)] if args.job_id else pending
    entries = [entry for entry in targets if entry is not None]
    if args.job_id and not entries:
        print(f"[FAIL] 没有这条死信：{args.job_id}")
        return 2
    if not entries:
        print("没有待处理的死信 —— 人工兜底队列是空的（健康形态）")
        return 0

    print(f"将要重放 {len(entries)} 条（队列 {args.queue_name}）"
          f"{'（dry-run：不入队）' if args.dry_run else ''}：")
    for entry in entries:
        suffix = f"retry{entry.replay_count + 1}" if args.rerun else ""
        print(f"  {replay_plan(entry, key_suffix=suffix).describe()}")
    if args.dry_run:
        return 0

    from app.config import get_settings

    queue = await RedisJobQueue.open(
        str(getattr(get_settings(), "redis_url", "") or ""),
        queue_name=args.queue_name,
        dead_letters=store,
    )
    failures = 0
    try:
        for entry in entries:
            suffix = f"retry{entry.replay_count + 1}" if args.rerun else ""
            plan = replay_plan(entry, key_suffix=suffix)
            try:
                outcome = await queue.enqueue(
                    JobSpec(
                        kind=plan.kind,  # type: ignore[arg-type]
                        function=plan.function,
                        payload=plan.payload,
                        idempotency_key=plan.idempotency_key,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 一条失败不该中断整批
                failures += 1
                print(f"  [FAIL] {entry.job_id} 入队失败：{exc}")
                continue
            print(f"  {'已重投' if outcome.created else '幂等命中（不再重投）'}：{outcome.job_id}")
    finally:
        await queue.aclose()

    print(
        "\n下一步：确认有一个在跑的 worker（`uv run arq app.queue.worker.WorkerSettings`）；"
        "执行结果会写在任务结果里，成功后本脚本会看到该条死信被标成 replayed_ok。"
    )
    return 1 if failures else 0


def render_list(entries: list[DeadLetterRecord], *, include_resolved: bool) -> None:
    """打印待处理（或全部）死信。空列表是**好消息**，所以那句话要写得清楚。"""
    if not entries:
        print("没有待处理的死信 —— 人工兜底队列是空的（健康形态）")
        return
    scope = "全部" if include_resolved else "待处理"
    print(f"{scope}死信 {len(entries)} 条（按失败时间升序，先失败的先处理）：")
    print(f"{'job_id':<48} {'kind':<10} {'attempts':<9} {'error_type':<24} {'retryable':<10} {'failed_at':<21} error")
    for entry in entries:
        print(render_entry(entry))
    print("\n要重放：`--job-id <id>` 或 `--all`（先 `--dry-run` 看一眼）")


def render_detail(entry: DeadLetterRecord) -> None:
    """打印一条死信的完整现场（**含 traceback** —— 这是它在库里唯一的存在理由）。"""
    print(f"job_id      : {entry.job_id}")
    print(f"任务        : {entry.function}（kind={entry.kind}）")
    print(f"尝试次数    : {entry.attempts}")
    print(f"可重试      : {entry.retryable}（{'运气不好' if entry.retryable else '代码/参数有问题'}）")
    print(f"入参        : {entry.payload}")
    print(f"错误        : {entry.error_type}: {entry.error_message}")
    print(f"失败时间    : {entry.failed_at}")
    if entry.policy is not None:
        print(
            f"当时的策略  : attempts={entry.policy.attempts} base={entry.policy.base_delay}s "
            f"max={entry.policy.max_delay}s jitter={entry.policy.jitter}"
        )
    print(f"处理状态    : {entry.resolution or '（还没人处理）'} 重放次数={entry.replay_count}")
    if entry.replayed_job_id:
        print(f"上次重放    : {entry.replayed_job_id}")
    print("--- traceback ---")
    print(entry.error_traceback or "（没有记录）")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit <= 0:
        print("[FAIL] --limit 必须是正数")
        return 2
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:  # pragma: no cover - 人工中断
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())

__all__ = ["DEFAULT_RESOLUTION", "RESOLUTIONS", "build_parser", "main", "render_detail", "render_list", "run"]
