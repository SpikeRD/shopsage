"""连接串规范化：把 libpq 形式补成 SQLAlchemy 方言 URL。

单独成模块的原因：`alembic/env.py` 需要这个转换，但**不能** import `app.db.session`
（那会在 import 时就建好连接池；Alembic 只想建自己的短连接，池只会白占连接预算）。

依赖方向：`urls` ← `session`、`urls` ← `alembic/env.py`，`urls` 不 import 任何本项目模块。
"""

from __future__ import annotations

# 本仓选定的驱动：psycopg3（不是 psycopg2）
DBAPI = "psycopg"


def to_sqlalchemy_url(url: str) -> str:
    """`postgresql://u:p@h/db` → `postgresql+psycopg://u:p@h/db`。

    为什么必须显式补：SQLAlchemy 对裸 `postgresql://` 的默认驱动是 **psycopg2**
    （实测 `make_url("postgresql://u:p@h/db").get_dialect().driver == "psycopg2"`），
    而本仓装的是 psycopg3 → 直接喂 `create_engine` 会报 `No module named 'psycopg2'`。
    已带方言（如 `postgresql+psycopg://`）时原样返回，尊重调用方。
    """
    if not url:
        raise ValueError(
            "database_url 为空：请在仓库根 .env 里配置 DATABASE_URL"
            "（本机无 Docker，host 要写 Linux VM 的 IP，见 docs/runbook/3.db-runbook.md §1）"
        )
    scheme, sep, rest = url.partition("://")
    if not sep:
        raise ValueError(f"连接串格式不对（缺 scheme://）：{url!r}")
    if "+" in scheme:
        return url
    return f"{scheme}+{DBAPI}://{rest}"


__all__ = ["DBAPI", "to_sqlalchemy_url"]
