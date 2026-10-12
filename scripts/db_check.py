"""数据库现场核查脚本（第 3 步的验收工具）。

用法（在仓库根执行）：
    uv run python -m scripts.db_check              # 默认：表清单 + 行数 + 结构摘要
    uv run python -m scripts.db_check --minimal     # 简洁输出（只 表数/总行数/版本）
    uv run python -m scripts.db_check --check       # 断言模式：不达最小演示量就退出码 1

它只做**只读**查询，可以随时在演示环境上跑；也是 `tests/test_db_seed.py` 的手工版。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy import Engine, text

from app.db.session import engine

# 最小演示量（3.db.md §7 的 test_seed_data_counts 用同一套口径）
MIN_COUNTS: dict[str, int] = {
    "users": 5,
    "products": 10,
    "orders": 20,
    "order_items": 20,
    "refunds": 3,
    "stock_movements": 5,
    "tickets": 5,
    "employees": 3,
}

#: **业务表**（架构第 5 节的 Schema）：种子数据与「8 张业务表」这条口径都按它算。
BUSINESS_TABLES: tuple[str, ...] = tuple(MIN_COUNTS)

#: 第 12 步（queue）与第 13 步（cache）的**基础设施表**：它们必须存在（迁移跑过），
#: 但**不属于业务数据** —— 「库里没有死信」「缓存是空的」都是健康的形态，不是缺数据。
#:
#: 为什么单独一份而不是塞进 `MIN_COUNTS`：`MIN_COUNTS` 的长度被三处当成
#: 「业务表数量」的判据（本脚本、`tests/test_db_migrations.py`、`tests/conftest.py` 的
#: 种子装置）。混进队列表与缓存表会让「业务表数量对不对」这条检查失去意义。
INFRA_TABLES: tuple[str, ...] = ("queue_dead_letters", "cache_semantic_entries", "cache_corpus_versions")

#: 迁移必须建出来的全部表（业务 + 基础设施）
REQUIRED_TABLES: tuple[str, ...] = BUSINESS_TABLES + INFRA_TABLES


@dataclass(frozen=True)
class Snapshot:
    """一次现场快照：表、行数、迁移版本、约束统计。"""

    tables: tuple[str, ...]
    counts: dict[str, int]
    revision: str | None
    constraints: dict[str, int]

    @property
    def missing_tables(self) -> tuple[str, ...]:
        """迁移应当建出来、但库里没有的表（业务 + 基础设施）。"""
        return tuple(t for t in REQUIRED_TABLES if t not in self.tables)

    @property
    def below_minimum(self) -> dict[str, tuple[int, int]]:
        """{表: (实际, 最小要求)}，只列不达标的。"""
        return {
            t: (self.counts.get(t, 0), minimum)
            for t, minimum in MIN_COUNTS.items()
            if self.counts.get(t, 0) < minimum
        }


def snapshot(eng: Engine | None = None) -> Snapshot:
    """读一次现场（只读，不开事务写）。"""
    eng = eng or engine
    with eng.connect() as conn:
        tables = tuple(
            r[0]
            for r in conn.execute(
                text(
                    "select table_name from information_schema.tables"
                    " where table_schema = 'public' order by table_name"
                )
            )
        )
        counts: dict[str, int] = {}
        for table in REQUIRED_TABLES:
            if table in tables:
                counts[table] = conn.execute(text(f'select count(*) from "{table}"')).scalar_one()

        revision = None
        if "alembic_version" in tables:
            revision = conn.execute(text("select version_num from alembic_version")).scalar_one_or_none()

        constraints = {
            contype: total
            for contype, total in conn.execute(
                text(
                    "select c.contype, count(*) from pg_constraint c"
                    " join pg_class r on r.oid = c.conrelid"
                    " join pg_namespace n on n.oid = r.relnamespace"
                    " where n.nspname = 'public' group by 1 order by 1"
                )
            )
        }
    return Snapshot(tables=tables, counts=counts, revision=revision, constraints=dict(constraints))


def render(snap: Snapshot, *, minimal: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"迁移版本 : {snap.revision or '(未迁移)'}")
    lines.append(f"表数量   : {len(snap.tables)}  ->  {', '.join(snap.tables)}")
    if minimal:
        lines.append(f"业务表行数合计: {sum(snap.counts.values())}")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"{'表':<18}{'行数':>8}{'最小要求':>10}  状态")
    for table in BUSINESS_TABLES:
        actual = snap.counts.get(table, 0)
        minimum = MIN_COUNTS[table]
        status = "OK" if actual >= minimum else f"不足 (缺 {minimum - actual})"
        if table not in snap.tables:
            status = "缺表！"
        lines.append(f"{table:<18}{actual:>8}{minimum:>10}  {status}")

    # 基础设施表（第 12 步 queue）：只报**存在性**，不报行数 ——
    # 「没有死信」是健康形态（见 INFRA_TABLES 的注释）。
    lines.append("")
    for table in INFRA_TABLES:
        actual = snap.counts.get(table, 0)
        exists = "存在" if table in snap.tables else "缺表！"
        lines.append(f"(基础设施) {table:<18}{actual:>8}{'—':>10}  {exists}")

    lines.append("")
    # contype: p=主键 f=外键 c=CHECK u=唯一
    labels = {"p": "主键", "f": "外键", "c": "CHECK", "u": "唯一", "x": "排他"}
    pretty = ", ".join(f"{labels.get(k, k)}={v}" for k, v in sorted(snap.constraints.items()))
    lines.append(f"约束统计 : {pretty}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据库现场核查（只读）")
    parser.add_argument("--minimal", action="store_true", help="只输出表数与总行数")
    parser.add_argument(
        "--check",
        action="store_true",
        help="断言模式：缺表或行数不达最小演示量时以退出码 1 结束（供 CI / 验收使用）",
    )
    args = parser.parse_args(argv)

    snap = snapshot()
    print(render(snap, minimal=args.minimal))

    if not args.check:
        return 0

    problems: list[str] = []
    if snap.missing_tables:
        problems.append(f"缺少业务表: {', '.join(snap.missing_tables)}（先跑 alembic upgrade head）")
    problems += [f"{t} 只有 {a} 行，要求 >= {m}" for t, (a, m) in snap.below_minimum.items() if t not in snap.missing_tables]
    if snap.revision is None:
        problems.append("alembic_version 为空：库没被迁移过（不要用 create_all 绕过迁移）")

    if problems:
        print("\n[FAIL] 验收不通过：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(
        f"\n[OK] {len(BUSINESS_TABLES)} 张业务表 + {len(INFRA_TABLES)} 张基础设施表齐全、"
        "行数达标、迁移版本已就位。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
