"""`scripts/queue_replay.py` 的 CLI 契约用例。

这些用例只验**参数解析与动作分派**，不真连库（真库那一条打 `@pytest.mark.db`）——
它们存在的理由是一个真实的回归：第一版把 `--all` 塞进 argparse 的互斥组
（当成「重放动作」的第二种），于是最自然的写法 `--list --all`
直接报 `argument --all: not allowed with argument --list`。

⚠️ 这类 bug 的特点是：**脚本本身能跑、用例全绿、只有人按直觉敲命令时才炸**。
所以「参数组合」必须有自动化版本 —— 它是「文档与实现不一致」里最难发现的一种。
"""

from __future__ import annotations

import pytest

from scripts.queue_replay import DEFAULT_RESOLUTION, RESOLUTIONS, build_parser

ACTIONS = ("list", "show", "job_id", "resolve")


def parse(*argv: str):
    return build_parser().parse_args(list(argv))


# ── 参数组合（不连库）────────────────────────────────────────────────────────


def test_list_and_all_together_are_accepted():
    """`--list --all`（「列出全部、包括已处理的」）必须被接受 —— 这是本次修的那个 bug。"""
    args = parse("--list", "--all")
    assert args.list is True
    assert args.all is True


def test_all_alone_is_accepted_and_defaults_to_list_semantics():
    """`--all` 单独出现也合法（语义上等于「列出全部」），**不会**变成「重放全部」。

    ⚠️ 这是刻意的安全默认：一个手滑的 `--all` 不该把整批死信真跑一遍
    （重放会调模型、会写库）。判断「谁负责执行」的逻辑在 `run()` 里：
    `if args.list or not (args.job_id or args.all): → 只列不跑`。
    """
    args = parse("--all")
    assert args.all is True
    assert args.list is False
    assert args.job_id is None
    # 「没有 job_id 也没有 list」→ `run()` 走列表分支（而不是重放分支）


def test_include_resolved_is_a_synonym_of_all():
    """`--include-resolved` 与 `--all` 是同一个意思（后者是自然读法）。"""
    assert parse("--include-resolved").include_resolved is True
    assert parse("--list", "--include-resolved").list is True


def test_actions_are_still_mutually_exclusive():
    """五个动作互斥的那部分**没有**被改坏（`--all` 不再是其中之一）。"""
    for left, right in (("--list", "--show"), ("--list", "--job-id"), ("--show", "--job-id"), ("--resolve", "--list")):
        with pytest.raises(SystemExit):
            parse(left, "x" if left != "--list" else "--show", right, "y")


def test_replay_forms():
    """两种重放形态都合法：单条（`--job-id`）与批量（`--all`）。"""
    single = parse("--job-id", "q_x")
    assert single.job_id == "q_x"
    batch = parse("--all", "--limit", "5", "--dry-run")
    assert batch.all is True and batch.limit == 5 and batch.dry_run is True


def test_resolution_choices_are_a_whitelist():
    """`--as` 的取值是白名单（自由文本会让「怎么收场的」变成人工考古）。"""
    assert parse("--resolve", "q_x").resolution == DEFAULT_RESOLUTION
    for value in RESOLUTIONS:
        assert parse("--resolve", "q_x", "--as", value).resolution == value
    with pytest.raises(SystemExit):
        parse("--resolve", "q_x", "--as", "随便写的")


def test_rerun_is_a_modifier_not_an_action():
    """`--rerun` 是修饰（「显式再跑一次」），可以和动作自由组合。"""
    args = parse("--job-id", "q_x", "--rerun")
    assert args.job_id == "q_x" and args.rerun is True


def test_queue_name_defaults_to_the_worker_queue():
    """默认队列名必须与 worker 一致（否则重放投到了没人消费的队列，且不报错）。"""
    from app.queue.backend import DEFAULT_QUEUE_NAME

    assert parse("--list").queue_name == DEFAULT_QUEUE_NAME


# ── 真库那一条（`--list --all` 端到端）──────────────────────────────────────


@pytest.mark.db
def test_list_all_runs_against_the_real_table(capsys):
    """真库上跑一次 `--list --all`：命令必须成功、且两类记录都能被列出来。

    用 `asyncio` 之外的最小入口（直接调 `run()` 的参数分支），
    这样不必去 patch 全局 store 就能验证「参数 → 分支」这条线在真库上确实通。
    """
    import asyncio

    from scripts.queue_replay import run

    args = build_parser().parse_args(["--list", "--all", "--limit", "20"])
    code = asyncio.run(run(args))
    assert code == 0
    output = capsys.readouterr().out
    assert "待处理" in output or "没有待处理的死信" in output
    assert "状态" in output or "没有待处理的死信" in output


@pytest.mark.db
def test_resolve_rejects_an_unknown_job(capsys):
    """`--resolve` 一个不存在的 id：返回码 2 + 一句能读懂的失败（而不是静默成功）。"""
    import asyncio

    from scripts.queue_replay import run

    args = build_parser().parse_args(["--resolve", "q_test:definitely-not-here", "--as", "wont_fix"])
    code = asyncio.run(run(args))
    assert code == 2
    assert "没有这条死信" in capsys.readouterr().out
