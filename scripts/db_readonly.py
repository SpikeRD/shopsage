"""只读角色 bootstrap（第 4 步 security 落地项；`3.db-runbook.md` §5.3 决策记录的第 3 条）。

**为什么是一个独立脚本，而不是 Alembic 迁移**：

1. 迁移文件进 Git，而 `CREATE ROLE ... PASSWORD '<明文>'` **绝不能**进 Git；
2. 角色与授权是**实例级**对象（不属于某个库的 schema 版本），把它塞进版本化迁移，
   会让「谁在什么时候改了什么权限」和「表结构变更」混在一条历史线里；
3. 口令要能被轮换（`--rotate-password`），迁移的 downgrade/upgrade 不是干这个用的。

**做什么**（全部幂等，可反复跑）：

1. 建 `shopsage_readonly` 角色（`LOGIN`、非 superuser、`NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`）；
2. `GRANT CONNECT` + `GRANT USAGE ON SCHEMA public` + **8 张业务表** `SELECT`；
3. `REVOKE` 写权限（`INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER`）与
   `public` schema 上的 `CREATE` —— **显式**收回，不依赖「PostgreSQL 15+ 默认就不给」这个前提；
4. 打印一条可直接粘进 `.env` 的 `DATABASE_READ_URL`（**不含密码**，密码要你自己填）；
5. `--downgrade-superuser`：把应用角色从 SUPERUSER 降级（可选，理由见下）。

**为什么要降级 superuser（可选但建议）**：superuser **绕过一切权限检查**。
如果不降级，那么「只读角色写不进去」这条验收虽然是真的，但同库的应用角色仍能一把梭
`DROP DATABASE`；更糟的是「用只读 DSN 验证越权」这件事在超级用户面前没有意义
（超管连接永远是权限正确的，问题只出在别的角色上）。本脚本**默认不动**既有角色，
需要时显式加 `--downgrade-superuser`。降级前会打印将要撤销的属性，方便你先确认。
⚠️ 降级后 `shopuser` 仍保留 `CREATEDB`，所以 Alembic/建库流程不受影响；
但**别把这条当成生产实践** —— 生产应使用「每个用途一个最小权限角色」，
本仓是开发环境，用一个角色图省事。

用法（仓库根执行）：

```powershell
# 生成口令并用它建角色（口令只经过环境变量，不落盘、不进 Git）
$env:SHOPSAGE_READONLY_PASSWORD = -join ((48..57)+(97..122) | Get-Random -Count 32 | % {[char]$_})
uv run python -m scripts.db_readonly --downgrade-superuser
# 把打印出来的 DATABASE_READ_URL（自己补上密码）写进 .env，然后复跑验证
uv run python -m scripts.db_readonly --verify
```
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql

from app.config import get_settings
from scripts.db_check import BUSINESS_TABLES

READONLY_ROLE = "shopsage_readonly"
READONLY_PASSWORD_ENV = "SHOPSAGE_READONLY_PASSWORD"

# 只读角色需要的授权（顺序即执行顺序；每条都写成可重复执行的形式）
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _admin_dsn() -> str:
    """用**写**角色连接（建角色、授权都需要管理权限）。"""
    dsn = get_settings().database_url
    if not dsn:
        raise SystemExit("DATABASE_URL 未配置：请先在仓库根 .env 里填好（见 3.db-runbook.md §1）")
    return dsn


def _readonly_dsn(password: str) -> str:
    """拼出只读角色的连接串：保留 host/port/db，只换 user 与 password。"""
    parts = urlsplit(_admin_dsn())
    userinfo = f"{READONLY_ROLE}:{quote(password, safe='')}"
    host = parts.hostname or "localhost"
    netloc = f"{userinfo}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _mask(dsn: str) -> str:
    """把连接串里的密码段掩码成 ***，用于打印。"""
    return re.sub(r"(://[^:/@]+:)[^@]*(@)", r"\1***\2", dsn)


def role_grants_summary(tables: list[str]) -> str:
    return f"{READONLY_ROLE} ← SELECT on {', '.join(tables)}"


def regrant(role: str = READONLY_ROLE) -> list[str]:
    """**只重放授权**（不动角色、不需要口令），返回已授 SELECT 的表清单。

    为什么必须有这个模式：**表级授权会随表一起消失**。`alembic downgrade base` 会 `DROP TABLE`，
    而 PostgreSQL 的 `GRANT SELECT ON TABLE orders` 是挂在**表对象**上的（OID），
    表一删授权就没了 —— 随后 `alembic upgrade head` 建回来的是全新的表，
    只读角色于是「看起来配好了、其实一张表都读不了」。

    实测踩过：跑完 `tests/test_db_migrations.py::test_migration_round_trip_is_repeatable`
    （它内部会 downgrade→upgrade 两轮）之后，`tests/test_db_queries.py` 全线
    `permission denied for table orders` 而红。所以「建表之后要重放授权」必须是个
    **可重复调用的一等操作**，而不是靠人记得再跑一次 bootstrap。
    """
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        for table in sorted(BUSINESS_TABLES):
            cur.execute(
                sql.SQL("GRANT SELECT ON TABLE {tbl} TO {role}").format(
                    tbl=sql.Identifier(table), role=sql.Identifier(role)
                )
            )
            for privilege in WRITE_PRIVILEGES:
                cur.execute(
                    sql.SQL("REVOKE {priv} ON TABLE {tbl} FROM {role}").format(
                        priv=sql.SQL(privilege),
                        tbl=sql.Identifier(table),
                        role=sql.Identifier(role),
                    )
                )
    return sorted(BUSINESS_TABLES)


def bootstrap(password: str, *, downgrade_superuser: bool = False) -> str:
    """建角色 + 授权 + 收回写权限（幂等）。返回只读 DSN（未落盘）。"""
    readonly_dsn = _readonly_dsn(password)

    with psycopg.connect(_admin_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        # ① 角色：存在则改口令，不存在则建。属性每次都显式钉一遍（不允许「历史遗留的 superuser」）。
        cur.execute(
            sql.SQL(
                "DO $$ BEGIN "
                "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_lit}) THEN "
                "    ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; "
                "  ELSE "
                "    CREATE ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; "
                "  END IF; "
                "END $$;"
            ).format(role=sql.Identifier(READONLY_ROLE), role_lit=sql.Literal(READONLY_ROLE))
        )
        # 口令单独一条语句：psycopg 的参数绑定在 ALTER ROLE 里不被支持，只能用 Literal（值仍安全转义）
        cur.execute(
            sql.SQL("ALTER ROLE {role} WITH PASSWORD {pw}").format(
                role=sql.Identifier(READONLY_ROLE), pw=sql.Literal(password)
            )
        )
        print(f"[1/5] 角色就绪：{READONLY_ROLE}（LOGIN / NOSUPERUSER / NOBYPASSRLS）")

        # ② 连接与 schema 使用权限
        db_name = get_settings().database_url.rsplit("/", 1)[-1]
        cur.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {db} TO {role}").format(
                db=sql.Identifier(db_name), role=sql.Identifier(READONLY_ROLE)
            )
        )
        cur.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {role}").format(role=sql.Identifier(READONLY_ROLE))
        )
        print(f"[2/5] CONNECT on {db_name} + USAGE on schema public 已授权")

        # ③ 8 张业务表的 SELECT（逐表授权，顺便把「白名单表」变成可核对的清单）
        for table in sorted(BUSINESS_TABLES):
            cur.execute(
                sql.SQL("GRANT SELECT ON TABLE {tbl} TO {role}").format(
                    tbl=sql.Identifier(table), role=sql.Identifier(READONLY_ROLE)
                )
            )
        print(f"[3/5] SELECT 已授权：{', '.join(sorted(BUSINESS_TABLES))}")

        # ④ 显式收回写权限（不依赖「新版 PG 默认就不给」）+ 收回建表权限
        for table in sorted(BUSINESS_TABLES):
            cur.execute(
                sql.SQL("REVOKE {privs} ON TABLE {tbl} FROM {role}").format(
                    privs=sql.SQL(", ").join(sql.SQL(p) for p in WRITE_PRIVILEGES),
                    tbl=sql.Identifier(table),
                    role=sql.Identifier(READONLY_ROLE),
                )
            )
        # ⚠️ 权限类型的**作用域**不同（实测踩过，别凭直觉写）：
        #    · `CREATE` 是 **schema** 级权限 → `REVOKE CREATE ON SCHEMA`
        #    · `TEMP`（临时表）是 **database** 级权限 → `REVOKE TEMP ON DATABASE`
        #    写成 `REVOKE CREATE, TEMP ON SCHEMA ...` 会直接报
        #    `invalid privilege type TEMP for schema`，而且**整条语句失败**（CREATE 也没撤掉）。
        cur.execute(
            sql.SQL("REVOKE CREATE ON SCHEMA public FROM {role}").format(
                role=sql.Identifier(READONLY_ROLE)
            )
        )
        cur.execute(
            sql.SQL("REVOKE CREATE, TEMP ON DATABASE {db} FROM {role}").format(
                db=sql.Identifier(db_name), role=sql.Identifier(READONLY_ROLE)
            )
        )
        print("[4/5] 已收回：INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER + schema CREATE")

        # ⑤ 可选：给应用角色降级（见模块 docstring 的理由）
        if downgrade_superuser:
            app_role = urlsplit(_admin_dsn()).username or ""
            if app_role:
                cur.execute("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = %s", (app_role,))
                row = cur.fetchone()
                if row is None:
                    print(f"[5/5] 跳过降级：找不到应用角色 {app_role}")
                elif row[0] or row[2]:
                    print(f"[5/5] 应用角色 {app_role} 降级前属性：superuser={row[0]} createdb={row[1]} createrole={row[2]}")
                    try:
                        cur.execute(
                            sql.SQL("ALTER ROLE {role} WITH NOSUPERUSER NOCREATEROLE").format(
                                role=sql.Identifier(app_role)
                            )
                        )
                    except psycopg.errors.FeatureNotSupported as exc:
                        # 实测：PostgreSQL 16 会拒绝「超级用户摘掉自己的 SUPERUSER」——
                        # `permission denied to alter role / DETAIL: The bootstrap user must have the SUPERUSER attribute.`
                        # PostgreSQL 的权限检查要求执行者**在本次改动后仍**满足条件，而摘掉自己的
                        # SUPERUSER 恰好让它不再满足。这不影响只读角色的落地（步骤 1–4 已提交），
                        # 但意味着「应用角色仍然是超级用户」这个残余风险要靠别的手段收掉。
                        print(f"[5/5] ⚠️  降级未生效：{str(exc).strip().splitlines()[0]}")
                        print(f"      DETAIL: {getattr(exc, 'diag', None) and exc.diag.message_detail or ''}")
                        print("      收尾办法见本手册 §「应用角色仍是超级用户」一条（用另一个超级用户执行，或开发环境重置时直接换最小权限角色）")
                    else:
                        print(f"[5/5] {app_role} → NOSUPERUSER NOCREATEROLE（保留 CREATEDB，Alembic 不受影响）")
                else:
                    print(f"[5/5] {app_role} 已是非超级用户，无需降级")
        else:
            print("[5/5] 跳过降级（要降级请加 --downgrade-superuser）")

    return readonly_dsn


def verify() -> int:
    """用 `.env` 里的 `DATABASE_READ_URL` 实测「读得到、写不了」。"""
    read_url = getattr(get_settings(), "database_read_url", "") or ""
    if not read_url:
        print(
            "DATABASE_READ_URL 未配置：把下面这行补上密码后写进仓库根 .env，再重跑本命令\n"
            f"  DATABASE_READ_URL={_mask(_readonly_dsn('<口令>'))}",
            file=sys.stderr,
        )
        return 2

    print(f"用只读 DSN 连接：{_mask(read_url)}")
    with psycopg.connect(read_url, autocommit=True) as conn, conn.cursor() as cur:
        identity = cur.execute("SELECT current_user, session_user, current_database()").fetchone()
        print(f"  current_user={identity[0]}  session_user={identity[1]}  db={identity[2]}")

        # ⚠️ 必须**逐表**试读：`GRANT SELECT` 是挂在表对象上的，表被 DROP 重建后授权会一起消失。
        # 只抽查一张表会漏掉「别的表没授权」这种状态（实测踩过：迁移回环之后只有部分表读不了）。
        unreadable: list[str] = []
        readable = 0
        for candidate in sorted(BUSINESS_TABLES):
            try:
                cur.execute(
                    sql.SQL("SELECT count(*) FROM {tbl}").format(tbl=sql.Identifier(candidate))
                ).fetchone()
                readable += 1
            except psycopg.errors.InsufficientPrivilege:
                unreadable.append(candidate)
        if unreadable:
            print(f"  ❌ 这 {len(unreadable)} 张表读不了（授权丢了？跑 --regrant-only）：{', '.join(unreadable)}")
        else:
            print(f"  ✅ {readable}/{len(BUSINESS_TABLES)} 张业务表全部可读")

        # 逐条尝试写操作，**每条都在独立事务里**，避免一条失败影响后续判定
        attempts = [
            ("DELETE", "DELETE FROM orders"),
            ("UPDATE", "UPDATE orders SET status = 'x'"),
            ("INSERT", "INSERT INTO orders (order_id) VALUES (-1)"),
            ("CREATE TABLE", "CREATE TABLE t_should_fail (id int)"),
            ("DROP TABLE", "DROP TABLE orders"),
            ("CREATE ROLE", "CREATE ROLE r_should_fail"),
        ]
        failures = 0
        for label, statement in attempts:
            with psycopg.connect(read_url, autocommit=True) as c2, c2.cursor() as cur2:
                try:
                    cur2.execute(statement)
                except psycopg.errors.InsufficientPrivilege as exc:
                    print(f"  ✅ {label:12} 被拒：{str(exc).strip().splitlines()[0][:88]}")
                    failures += 1
                except psycopg.Error as exc:
                    print(f"  ⚠️ {label:12} 报的是别的错：{type(exc).__name__}: {str(exc)[:80]}")
                else:
                    print(f"  ❌ {label:12} **居然成功了** —— 只读角色没落地！")

    # 端到端再确认一次：**应用自己的读引擎**是不是真的落在只读连接上。
    # 这一段才是 `4.security.md` §8 说的「get_read_engine() 真正落到只读连接」——
    # 前面的 psycopg 直连证明的是「这个角色写不进去」，两者不是同一件事。
    engine_ok = False
    try:
        from app.db.session import get_read_engine, get_write_engine

        read_engine = get_read_engine()
        with read_engine.connect() as conn:
            current_user = conn.exec_driver_sql("select current_user").scalar()
        engine_ok = current_user == READONLY_ROLE
        same_engine = read_engine is get_write_engine()
        flag = "✅" if engine_ok else "❌"
        print(
            f"  {flag} get_read_engine().current_user = {current_user}"
            f"（与写引擎同一个对象？{same_engine}，应为 False）"
        )
    except Exception as exc:  # noqa: BLE001 - 引擎不可用时只报告，不影响前面的直连结论
        print(f"  ⚠️ 读引擎检查跳过：{type(exc).__name__}: {exc}")

    print()
    if failures == len(attempts) and not unreadable and engine_ok:
        print(
            f"验收通过：{readable}/{len(BUSINESS_TABLES)} 张表可读，"
            f"{failures}/{len(attempts)} 条写操作全部被数据库拒绝（权限错误），"
            f"且应用读引擎落在 {READONLY_ROLE} 上。"
        )
        return 0
    print(
        f"验收未通过：可读 {readable}/{len(BUSINESS_TABLES)}，写操作被拒 {failures}/{len(attempts)}，"
        f"读引擎就位={engine_ok}。",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建/更新只读角色 shopsage_readonly 并验证")
    parser.add_argument("--verify", action="store_true", help="只做验证（用 .env 的 DATABASE_READ_URL）")
    parser.add_argument(
        "--regrant-only",
        action="store_true",
        help="只重放 8 张表的 SELECT 授权（**迁移 downgrade/upgrade 之后必跑**；不需要只读口令）",
    )
    parser.add_argument(
        "--downgrade-superuser",
        action="store_true",
        help="顺带把应用角色降级为 NOSUPERUSER NOCREATEROLE（理由见模块 docstring）",
    )
    parser.add_argument("--rotate-password", action="store_true", help="显式表示「这次是轮换口令」")
    args = parser.parse_args(argv)

    if args.regrant_only:
        tables = regrant()
        print(f"已重放授权：{role_grants_summary(tables)}")
        return 0

    if args.verify:
        return verify()

    password = os.getenv(READONLY_PASSWORD_ENV, "")
    if not password:
        print(
            f"缺少环境变量 {READONLY_PASSWORD_ENV}（口令只从环境变量读，不落盘、不进 Git）：\n"
            "  PowerShell: $env:SHOPSAGE_READONLY_PASSWORD = -join ((48..57)+(97..122) | Get-Random -Count 32 | % {[char]$_})\n"
            "  然后重跑本命令",
            file=sys.stderr,
        )
        return 2
    if len(password) < 16:
        print(f"{READONLY_PASSWORD_ENV} 至少 16 字符（当前 {len(password)}）", file=sys.stderr)
        return 2

    readonly_dsn = bootstrap(password, downgrade_superuser=args.downgrade_superuser)
    print()
    print("把下面这行（**自己补上口令**）写进仓库根 .env，然后复跑 `--verify`：")
    print(f"  DATABASE_READ_URL={_mask(readonly_dsn)}")
    if args.rotate_password:
        print("（口令轮换完成：所有持有旧 DSN 的进程需要重启才会用上新口令）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
