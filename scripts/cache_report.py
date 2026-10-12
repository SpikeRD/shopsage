r"""缓存运维工具：看现场 / 清指定范围 / 回收过期（`13.cache.md` §5 的失效策略运维入口）。

用法（仓库根执行）：

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uv run python -m scripts.cache_report                      # 现场：配置 / 存储 / 条目分布 / 自检
uv run python -m scripts.cache_report --scope customer:2    # 看某个身份存了什么（问题清单，不含答案）
uv run python -m scripts.cache_report --sample 5            # 抽样看几条（**含答案摘要**，本地工具）
uv run python -m scripts.cache_report --purge               # 只删**已过期**的条目（安全动作）
uv run python -m scripts.cache_report --invalidate-scope customer:2   # 主动失效（第二策略）
uv run python -m scripts.cache_report --purge-all --yes     # 清空（**危险动作**，必须显式确认）
```

## 三条口径

1. **默认只读**。不改任何数据 —— 与第 12 步 `scripts/queue_replay.py` 的默认动作
   （「只列不跑」）同一纪律：运维工具的第一个动词应该是「看」。
2. **「清空」需要 `--yes`**。`--purge-all` 的代价是「接下来一段时间所有问题都重新走
   检索 + 生成」，它不该是一次顺手能按的动作（`--purge` 只删已过期的，那个可以随便按）。
3. **答案正文默认不打印**。缓存里存的是给用户的回答，可能含订单号、地址这类 PII；
   要在本机看清楚，得显式加 `--sample N`（它仍然只打印前 60 字）。

## 为什么需要这个工具（而不是只靠 SQL / redis-cli）

因为**缓存的状态是分布式的、且会自己变化**：条目在 Postgres（或 Redis）里、
TTL 在应用层判、版本在第三张表里、降级状态在进程内存里。
「现在到底有多少条、哪些过期了、命中率怎么样」这四个问题的答案分散在四处 ——
这个脚本把它们拼成一张现场图（与第 11 步 `scripts/observability_demo.py` 同一思路）。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger("scripts.cache_report")


def _fmt_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:.1f}h"


def snapshot(cache: Any) -> dict[str, Any]:
    """拼一张现场图（**只读**）：配置 / 存储 / 版本 / 条目分布。

    每一项都从**已有的出口**取（`cache.health()` / `store.count()` /
    `corpus.current_revision()`），不在这里另写一套统计逻辑 ——
    两处统计必然漂移，而「缓存里到底有多少条」这种数字漂移起来没人能发现。
    """
    from app.cache.corpus import current_revision

    store_health = {}
    try:
        store_health = dict(cache.store.health())
    except Exception as exc:  # noqa: BLE001 - 现场图不该因为一项失败就整体失败
        store_health = {"error": f"{type(exc).__name__}: {exc}"}

    counts: dict[str, int] = {}
    for label, scope in (("all", None),):
        try:
            counts[label] = int(cache.store.count(scope=scope))
        except Exception:  # noqa: BLE001
            counts[label] = -1

    entries: list[Any] = []
    # 内存后端有 `all_entries()`、Postgres 有 `list_entries()` —— 两个名字都试
    # （工具不必知道后端是谁，但也不该为了统一而给内存实现凭空加一层上限参数）。
    lister = getattr(cache.store, "all_entries", None) or getattr(cache.store, "list_entries", None)
    if callable(lister):
        try:
            entries = list(lister())
        except Exception as exc:  # noqa: BLE001 - 现场图不该因为一项失败就整体失败
            logger.warning("列举条目失败（忽略）：%s", exc)

    return {
        "settings": cache.settings,
        "enabled": bool(cache.settings.enabled),
        "store": cache.store.name(),
        "degraded": bool(cache.degraded),
        "kv": type(cache.key_value).__name__ if cache.key_value is not None else "-",
        "store_health": store_health,
        "revision": current_revision(),
        "counts": counts,
        "entries": entries,
        "stats": dict(cache.semantic.stats),
        "result_store": cache.results.state(),
        "problems": cache.health().problems,
    }


def print_snapshot(state: dict[str, Any], *, sample: int = 0) -> None:
    settings = state["settings"]
    print("=" * 78)
    print("缓存现场")
    print("=" * 78)
    print(f"  开关          : {'开' if state['enabled'] else '关（CACHE_ENABLED=false）'}")
    print(
        f"  阈值/兜底线    : {settings.semantic_threshold} / {settings.semantic_floor}"
        f"（灰区 = 两者之间，判不命中）"
    )
    print(
        f"  TTL           : 普通 {settings.ttl_seconds}s / 政策 {settings.policy_ttl_seconds}s"
        f" / 强时效 {settings.time_sensitive_policy}"
        + (f"({settings.time_sensitive_ttl_seconds}s)" if settings.time_sensitive_policy == "short" else "")
    )
    print(f"  条目存储       : {state['store']}（降级={state['degraded']}）· 键值层 {state['kv']}")
    print(f"  知识库版本     : {state['revision']}（变了 → 全部旧条目失效）")
    print(f"  未过期条目     : {state['counts'].get('all', '?')}")
    health = state["store_health"]
    print(
        f"  存储自检       : {health.get('store', '?')}"
        f" 表={health.get('table') or '-'} 维度={health.get('dim') or '-'}"
        f"（期望 {health.get('expected_dim')}）"
    )
    print(f"  本轮进程统计   : {state['stats']}")
    print(f"  结果缓存       : {state['result_store']}")
    if state["problems"]:
        print(f"  ⚠️ 配置问题     : {'；'.join(state['problems'])}")
    else:
        print("  配置问题       : 无")

    entries = state["entries"]
    if entries:
        now = time.time()
        fresh = [entry for entry in entries if not entry.is_expired(now=now)]
        expired = len(entries) - len(fresh)
        policies: dict[str, int] = {}
        for entry in entries:
            policies[entry.policy] = policies.get(entry.policy, 0) + 1
        hit_total = sum(int(entry.hit_count) for entry in entries)
        print(
            f"  条目明细       : 共 {len(entries)}（未过期 {len(fresh)} / 已过期 {expired}）"
            f" · 分档 {policies} · 命中次数合计 {hit_total}"
        )
    if sample:
        print("\n  抽样（**含答案摘要**：本地工具，别复制到工单/聊天里）：")
        for entry in state["entries"][:sample]:
            print(
                f"    [{entry.policy}] {entry.scope} | fp={entry.fingerprint[:8]} | "
                f"rev={entry.revision} | age={_fmt_seconds(entry.age_seconds())} | "
                f"hits={entry.hit_count}\n"
                f"        Q: {entry.question[:60]}\n"
                f"        A: {entry.answer[:60]}…"
            )


def list_scope(cache: Any, scope: str, *, limit: int = 50) -> int:
    """列出某个身份的问题清单（**不打印答案**）。"""
    lister = getattr(cache.store, "all_entries", None) or getattr(cache.store, "list_entries", None)
    if not callable(lister):
        print("  该存储不支持列举。")
        return 0
    rows = [entry for entry in lister() if entry.scope == scope]
    print(f"scope={scope} 共 {len(rows)} 条：")
    for entry in rows[:limit]:
        flag = "已过期" if entry.is_expired() else "有效"
        print(
            f"  [{flag}] rev={entry.revision} age={_fmt_seconds(entry.age_seconds())} "
            f"hits={entry.hit_count} 「{entry.question[:50]}」"
        )
    return len(rows)


def health_probe(*, redis_url: str = "") -> list[tuple[str, str, str]]:
    """把 `/health` 的 cache 组件在**四种状态**下各打一次（`--health`）。

    为什么要在脚本里做这件事（而不是写在 runbook 里让人手敲）：
    这四态是「缓存这件事有没有被看见」的全部出口，而它们**很难手工构造**
    （尤其第三态：要先建 app、再把 `app.state.cache` 摘掉）。
    与第 11 步 `scripts/observability_demo.py` 把「指标系列对账」做成场景是同一个理由：
    **能一键复跑的证据才是证据**。

    ⚠️ 第三态要注意一个坑：`create_app(cache=None)` 的含义是「**自己去建一个**」，
    不是「不装缓存」—— 所以「摘掉缓存」只能在 app 建好之后做
    （`app.state.cache = None`）。第一版按「传 None = 不装」写，结果打印出 `ok`，
    而它看起来像「skipped 分支坏了」。
    """
    from contextlib import ExitStack

    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.cache.api import build_cache
    from app.cache.settings import CacheBackendSettings, CacheConfig, CacheSettings
    from app.graph import GraphRuntime

    rows: list[tuple[str, str, str]] = []

    def probe(label: str, cache: Any, *, unwire: bool = False) -> None:
        app = create_app(
            runtime=GraphRuntime(llm=lambda *a, **k: {}, search=lambda **_: []),
            cache=cache,
            probe_timeout_value=0.05,
        )
        with ExitStack() as stack:
            client = stack.enter_context(TestClient(app))
            if unwire:
                app.state.cache = None
            body = client.get("/health").json()
        entry = next((c for c in body["components"] if c["name"] == "cache"), None)
        rows.append((label, entry["status"], entry["detail"]) if entry else (label, "缺失", ""))

    settings = CacheSettings()
    probe(
        "正常（注入了内存缓存）",
        build_cache(
            config=CacheConfig(cache=settings, backend=CacheBackendSettings(redis_url=redis_url))
        ),
    )
    probe(
        "配置写反（阈值 < 兜底线）",
        build_cache(
            config=CacheConfig(
                cache=CacheSettings(semantic_threshold=0.5, semantic_floor=0.9),
                backend=CacheBackendSettings(redis_url=redis_url),
            )
        ),
    )
    probe(
        "降级（Redis 不可达）",
        build_cache(
            config=CacheConfig(
                cache=CacheSettings(lookup_timeout_seconds=0.3),
                backend=CacheBackendSettings(redis_url="redis://127.0.0.1:1/0", instance="probe"),
            )
        ),
    )
    probe("未装配（app 建好后摘掉）", None, unwire=True)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="缓存运维工具（默认只读）")
    parser.add_argument("--scope", default="", help="只看某个身份的条目（`role:user_id`）")
    parser.add_argument("--sample", type=int, default=0, help="抽样打印 N 条（含答案摘要）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--purge", action="store_true", help="只删**已过期**的条目（安全）")
    group.add_argument("--purge-all", action="store_true", help="清空全部条目（**危险**，需 --yes）")
    group.add_argument("--invalidate-scope", default="", help="主动失效某个身份的缓存（第二策略）")
    parser.add_argument("--yes", action="store_true", help="确认危险动作")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（给脚本用）")
    parser.add_argument(
        "--health", action="store_true", help="把 /health 的 cache 组件在四种状态下各打一次"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    if args.health:
        print("=" * 78)
        print("/health 的 cache 组件（四态）")
        print("=" * 78)
        for label, status, detail in health_probe():
            print(f"  {label:24s} status={status}")
            print(f"      {detail}")
        return 0

    from app.cache.api import build_cache

    cache = build_cache()

    if args.purge_all:
        if not args.yes:
            print("拒绝执行：`--purge-all` 会让接下来一段时间**所有**问题重新走检索 + 生成。")
            print("要真做请加 `--yes`；只想清掉已经过期的用 `--purge`。")
            return 2
        removed = cache.invalidate(allow_purge=True)
        print(f"已清空：条目 {removed['entries']} 条、结果缓存 {removed['results']} 条")
        return 0

    if args.purge:
        from app.cache.invalidate import purge_expired

        removed = purge_expired(cache.store)
        print(f"已回收过期条目 {removed} 条（未过期的**一条没动**）")
        return 0

    if args.invalidate_scope:
        role, _, user = args.invalidate_scope.partition(":")
        try:
            user_id = int(user)
        except ValueError:
            print(f"scope 形态必须是 `role:user_id`，收到 {args.invalidate_scope!r}")
            return 2
        from app.cache.invalidate import invalidate_scope

        removed = invalidate_scope(cache.store, user_id=user_id, role=role)
        print(f"已让 scope={args.invalidate_scope} 的 {removed} 条缓存失效（行保留，仅标记失效）")
        return 0

    state = snapshot(cache)
    if args.json:
        import json
        from dataclasses import asdict, is_dataclass

        payload = {
            key: (asdict(value) if is_dataclass(value) else value)
            for key, value in state.items()
            if key != "entries"
        }
        print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))
        return 0

    print_snapshot(state, sample=args.sample)
    if args.scope:
        print()
        list_scope(cache, args.scope)
    print("\n提示：`--purge` 只删已过期的；`--purge-all --yes` 才是清空（危险）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
