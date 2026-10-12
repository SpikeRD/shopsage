"""人工重放 / 排查死信队列（`12.queue.md` §6 第 5 步的运维工具）。

```powershell
uv run python -m scripts.queue_replay --list                    # 看有哪些【待处理】的死信
uv run python -m scripts.queue_replay --list --all              # 连【已处理】的一起看（--include-resolved 同义）
uv run python -m scripts.queue_replay --show <job_id>           # 看一条的完整现场（含 traceback）
uv run python -m scripts.queue_replay --job-id <job_id>         # 重放一条
uv run python -m scripts.queue_replay --all --limit 5           # 批量重放（先失败的先来）
uv run python -m scripts.queue_replay --all --dry-run           # 只看会重放什么，不动手
uv run python -m scripts.queue_replay --job-id <id> --rerun     # 显式再跑一次（新幂等键）
uv run python -m scripts.queue_replay --resolve <id> --as wont_fix   # 不重放，只标记已处理
```

## `--all` 是**修饰**，不是动作（这里改过一次，值得记下来）

第一版把 `--all` 放进 argparse 的互斥组（当成「重放动作」的第二种），于是
`--list --all` 直接报 `argument --all: not allowed with argument --list` ——
而「列出全部、包括已处理的」恰恰是这个参数组合最自然的读法。

修正后的语义分两层：

```
动作（五选一）：--list（默认） / --show <id> / --job-id <id> / --resolve <id> / 什么都不给（= --list）
修饰：--all（= --include-resolved）
       与「列表」一起 → 连已处理的也列出来
       与「没有动作」一起 → 仍然是列表（**不会**变成「重放全部」）
       与「重放」一起 → 重放全部待处理的（配 --limit）
```

⚠️ 关键的一条不变式：**「重放」永远需要显式指定范围**（`--job-id <id>` 或 `--all`
且必须有重放意图）。一个手滑的 `--all` 就把整批死信真跑一遍，是最容易造成二次事故的形态。

## 四个刻意的行为

1. **默认只列不跑**。重放会真的执行任务（可能调模型、可能写库），
   所以「什么都不做」必须是默认动作。
2. **默认复用原幂等键**（`core.replay_plan`）。于是「点两次」只会跑一次，
   而写路径的副作用不会重复（A2）。真要再跑一遍必须显式 `--rerun`
   （它给键加 `retryN` 后缀，重放次数在 job_id 上是**看得见**的）。
3. **`--as` 的取值是一张白名单**（`replayed_ok` / `replayed_failed` / `wont_fix` / `manual_fix`）。
   自由文本的 resolution 会让「这条死信是怎么收场的」变成一次人工考古。
4. **`--dry-run` 会把它要跑的范围先打出来**：批量的默认范围是 `--limit` 条待处理
   （先失败的先来），单条就是 `--job-id` 指定的那一条。

⚠️ 这个脚本**不启动 worker**：它只负责把任务重新投进队列（`enqueue`）。
   真正执行要有一个在跑的 `arq app.queue.worker.WorkerSettings` 进程 ——
   投完立刻 `--list` 会看到它已经不在待处理里（`replayed_job_id` 已写上），
   但任务的**执行结果**要等 worker 跑完才出现在 `GET /api/tasks/{job_id}`。
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
    # ⚠️ 「动作」互斥，但 `--all` **不是动作**（第一版把它塞进了这个互斥组，
    # 于是 `--list --all` 直接报 `argument --all: not allowed with argument --list` ——
    # 而那恰恰是最自然的读法：「列出全部，包括已处理的」）。
    #
    # 修正后的语义分两层：
    #   动作（五选一）：--list（默认）/ --show <id> / --job-id <id> / --resolve <id> / 无
    #   修饰：`--all` = 「包括已处理的」（列表）或「全部待处理」（重放）；`--include-resolved` 是它的同义别名
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true", help="列出死信（默认动作）")
    action.add_argument("--show", metavar="JOB_ID", help="显示一条死信的完整现场（含 traceback）")
    action.add_argument("--job-id", metavar="JOB_ID", help="重放指定的一条")
    action.add_argument("--resolve", metavar="JOB_ID", help="不重放，只把它标记为已处理")
    parser.add_argument(
        "--all",
        action="store_true",
        help="范围修饰：与 --list 一起 = 连已处理的也列出来；与 --dry-run 一起 = 批量重放全部待处理",
    )
    parser.add_argument("--limit", type=int, default=10, help="批量/列表的上限（默认 10）")
    parser.add_argument(
        "--include-resolved",
        action="store_true",
        help="`--all` 的同义写法（列表里连已处理的也显示）",
    )
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

    # `--all` 与 `--include-resolved` 是同一个意思（前者是自然读法，后者说得更清楚）。
    # ⚠️ 它的含义**取决于动作**：列表时 = 「连已处理的也列出来」；重放时 = 「全部待处理」。
    # （第一版把 `--all` 当成「重放动作」，于是 `--list --all` 报参数冲突 —— 见模块 docstring。）
    include_resolved = bool(args.all or args.include_resolved)

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

    # 只有 `--all` 而没有任何动作时：**默认是列表**（不是重放）。
    # 「让重放成为默认动作」是最容易造成二次事故的一种设计（一个手滑的
    # `--all` 就会把整批死信真跑一遍），所以重放必须显式指定范围。
    if args.list or not (args.job_id or args.all):
        pending = store.pending(limit=args.limit, include_resolved=include_resolved)
        render_list(pending, include_resolved=include_resolved)
        return 0

    # 重放：`--job-id <id>` 单条，或 `--all` 全部待处理
    if args.job_id:
        entry = store.fetch(args.job_id)
        entries = [entry] if entry is not None else []
        if not entries:
            print(f"[FAIL] 没有这条死信：{args.job_id}")
            return 2
    else:
        entries = store.pending(limit=args.limit)
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
    """打印待处理（或全部）死信。空列表是**好消息**，所以那句话要写得清楚。

    `include_resolved=True`（`--all` / `--include-resolved`）时**多一列 `resolution`** ——
    否则「这行是还没人处理的，还是已经重放成功的」只能靠肉眼对比行数，那正是
    最容易被误读的一处（`--list --all` 的报告里混着两类记录）。
    """
    if not entries:
        print("没有待处理的死信 —— 人工兜底队列是空的（健康形态）")
        return
    pending = [entry for entry in entries if not entry.resolution]
    if include_resolved:
        print(f"全部死信 {len(entries)} 条（其中待处理 {len(pending)} 条；按失败时间升序，先失败的先处理）：")
        # reso 那一列写在**最前面**（而不是最后）：`render_entry` 是一个定宽的一行摘要，
        # 「已处理 / 待处理」在它后面接一列会对不齐（中文列宽 vs 字符宽度），
        # 而对不齐的表格会让人读错行 —— 这正是「--list --all 混着两类记录」最怕的事。
        print(f"{'状态':<12} " + f"{'job_id':<48} {'kind':<10} {'attempts':<9} {'error_type':<24} {'retryable':<10} {'failed_at':<21} error")
        for entry in entries:
            mark = entry.resolution or "—（待处理）"
            print(f"{mark:<12} " + render_entry(entry))
    else:
        print(f"待处理死信 {len(entries)} 条（按失败时间升序，先失败的先处理）：")
        print(f"{'job_id':<48} {'kind':<10} {'attempts':<9} {'error_type':<24} {'retryable':<10} {'failed_at':<21} error")
        for entry in entries:
            print(render_entry(entry))
    if pending:
        print("\n要重放：`--job-id <id>`（单条）或 `--all --dry-run`（批量，先干跑）")
    else:
        print("\n这些都已经处理过了（`resolution` 非空）—— 没有待重放的")


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
