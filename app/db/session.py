"""数据库连接：引擎、连接池、会话工厂，以及读/写两个入口。

`3.db.md` 第 2 节的对外接口就在这里：`engine` / `SessionLocal` / `get_db` /
`get_read_engine` / `get_write_engine`。

两条设计口径：
1. **池参数显式设上限**（C1）：`pool_size` + `max_overflow` + `pool_pre_ping`。
2. **读写分离先是「能力」**：读/写各有一个引擎入口，默认指向同一个库；
   挂只读副本时只需给 `DATABASE_READ_URL`，业务代码不改。
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

# ── 连接池参数（C1 的落点）───────────────────────────────────────────────────
# 真实上限 = POOL_SIZE + MAX_OVERFLOW（每个进程各自一套池）。
# 估算：pool_size ≈ 每 worker 并发 SQL 数；max_overflow 是峰值临时额度。
# 4 worker × (5 + 10) = 60，仍低于 PostgreSQL 默认 max_connections=100（还要留给
# psql / DBeaver / 队列 worker），所以宁可排队也不要把库拖垮。
POOL_SIZE = 5
MAX_OVERFLOW = 10
POOL_TIMEOUT = 30  # 借不到连接时最多等 30s，然后抛 TimeoutError（快速失败，不无限挂住请求）
POOL_RECYCLE = 1800  # 连接最长寿命，防中间设备（NAT/防火墙）静默掐断空闲连接

# 本仓选定的驱动：psycopg3
_DBAPI = "psycopg"


def to_sqlalchemy_url(url: str) -> str:
    """把「libpq 形式的连接串」补成 SQLAlchemy 方言 URL。

    为什么必须显式补：SQLAlchemy 对裸 `postgresql://` 的默认驱动是 **psycopg2**
    （本机实测 `make_url(...).get_dialect().driver == "psycopg2"`），而本仓装的是
    psycopg3；直接喂给 `create_engine` 会报 `No module named 'psycopg2'`。
    `.env` 里保持不带方言的写法（psql / psycopg 原生连接串都能用），方言在代码里补。
    """
    if not url:
        raise ValueError(
            "database_url 为空：请在仓库根 .env 里配置 DATABASE_URL"
            "（本机无 Docker，host 要写 Linux VM 的 IP，见 docs/runbook/3.db-runbook.md §1）"
        )
    scheme, sep, rest = url.partition("://")
    if not sep:
        raise ValueError(f"连接串格式不对（缺 scheme://）：{url!r}")
    if "+" in scheme:  # 已经带方言（如 postgresql+psycopg）→ 尊重调用方，不再改写
        return url
    return f"{scheme}+{_DBAPI}://{rest}"


def make_engine(
    url: str,
    *,
    pool_size: int = POOL_SIZE,
    max_overflow: int = MAX_OVERFLOW,
    pool_timeout: int = POOL_TIMEOUT,
) -> Engine:
    """建引擎：池上限 + pre-ping + 会话级 UTC。

    参数化池大小是给测试用的（例如把池压到 1 来复现「排队超时」），业务代码用默认值。
    """
    return create_engine(
        to_sqlalchemy_url(url),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_recycle=POOL_RECYCLE,
        pool_pre_ping=True,  # 跨主机刚需：踢掉被 NAT 静默丢弃的死连接
        # psycopg3 直接吃 libpq 的 options；只影响本连接，不动库的全局参数
        connect_args={"options": "-c timezone=utc"},
    )


_settings = get_settings()


def _read_url() -> str:
    """读路径连接串：优先 `DATABASE_READ_URL`（挂只读副本时用），否则与写路径同库。

    `Settings` 是 `extra="allow"`，所以 `.env` 里的 `DATABASE_READ_URL` 会落在
    `__pydantic_extra__` 里，用 getattr 就能取到 —— 不必为了 db 模块去改 config 的字段表。
    """
    return getattr(_settings, "database_read_url", "") or _settings.database_url


# 写路径引擎（预定义 service 的写操作，以及 Alembic 迁移都走这里）
engine: Engine = make_engine(_settings.database_url)


@lru_cache(maxsize=1)
def get_write_engine() -> Engine:
    """写路径引擎。"""
    return engine


@lru_cache(maxsize=1)
def get_read_engine() -> Engine:
    """读路径引擎（Text-to-SQL 只读白名单走这里）。

    默认与写引擎同一个库；设了 `DATABASE_READ_URL` 就切到只读副本。
    """
    read_url = _read_url()
    if read_url == _settings.database_url:
        return engine  # 同库时不重复建池（少占一套连接预算）
    return make_engine(read_url)


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个会话，异常也要还回池。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def pool_status() -> dict[str, int]:
    """池现状（排障 / 压测断言用）。

    真实上限是 `pool_size + max_overflow`，所以直接把它算成 `capacity` 返回，
    免得调用方去摸 `pool._max_overflow` 这种私有属性。
    """
    pool = engine.pool
    return {
        "pool_size": pool.size(),
        "max_overflow": MAX_OVERFLOW,
        "capacity": pool.size() + MAX_OVERFLOW,
        "checked_out": pool.checkedout(),
        "in_pool": pool.checkedin(),
    }


__all__ = [
    "MAX_OVERFLOW",
    "POOL_RECYCLE",
    "POOL_SIZE",
    "POOL_TIMEOUT",
    "SessionLocal",
    "engine",
    "get_db",
    "get_read_engine",
    "get_write_engine",
    "make_engine",
    "pool_status",
    "to_sqlalchemy_url",
]
