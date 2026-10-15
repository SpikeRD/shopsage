"""需求 2 取证脚本（变更 002 / T16）：账号与口令**确实在数据库里**，且口令是散列不是明文。

一次性取证脚本，不是测试套件。跑法：

    uv run python -m scripts.evidence_accounts

它只读、不写、不打印任何口令明文 —— 只打印散列算法的前缀，用来证明
「库里存的不是明文」。
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Account
from app.db.session import SessionLocal

#: 期望的散列标识。实测值形如 `argon2$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>`
#: （`app/security/auth.hash_password` 用的是 Argon2id，外面还包了一层 `argon2$` 前缀）。
#: ⚠️ 判据用 `argon2id` 而不是 `$argon2`：第一版写成了后者，于是**全部 4 行都被误判成异常**
#: —— 这正是「判据写错会把好数据报成坏数据」的现场。
ARGON2_MARKER = "argon2id"


def main() -> int:
    with SessionLocal() as db:
        rows = db.execute(select(Account).order_by(Account.account_id)).scalars().all()
        print(f"accounts 表行数 = {len(rows)}")
        print()
        header = f"{'id':<4}{'identifier':<16}{'role':<20}{'subject':<14}{'status':<10}password_hash"
        print(header)
        print("-" * len(header))
        for row in rows:
            subject = f"{row.subject_type}#{row.subject_id}"
            digest = str(row.password_hash)
            print(
                f"{row.account_id:<4}{row.identifier:<16}{row.role:<20}"
                f"{subject:<14}{row.status:<10}{digest[:16]}…（共 {len(digest)} 字符）"
            )
        print()

        not_argon2 = [row.identifier for row in rows if ARGON2_MARKER not in str(row.password_hash)]
        print(f"口令散列算法：{'全部为 Argon2id' if not not_argon2 else f'异常项 {not_argon2}'}")
        print(f"明文口令行（必须为空）: {[row.identifier for row in rows if str(row.password_hash) in {'admin-demo-pass', 'customer-demo-pass', 'employee-demo-pass'}]}")
        print(f"唯一标识约束：{len({row.identifier for row in rows})} 个不同标识 / {len(rows)} 行")

        ok = len(rows) > 0 and not not_argon2
        print()
        print("VERDICT =", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
