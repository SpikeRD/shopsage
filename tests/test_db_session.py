"""连接串方言与连接池参数的测试（**不需要数据库**，纯逻辑）。

这两条覆盖最常翻车的两处：
- 裸 `postgresql://` 在 SQLAlchemy 里的默认驱动是 psycopg2（而本仓装 psycopg3）→ 必须由代码补方言；
- 连接池必须有显式上限（C1）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from app.db import session as db_session
from app.db.urls import to_sqlalchemy_url

pytestmark = pytest.mark.db


# ── 连接串方言 ────────────────────────────────────────────────────────────────


def test_plain_url_defaults_to_psycopg2_in_sqlalchemy():
    """先钉住「不补方言会出事」这个前提：裸 postgresql:// 的默认驱动是 psycopg2。

    这条不是测我们的代码，而是把「为什么必须补方言」变成可执行的证据：
    哪天 SQLAlchemy 改了默认驱动，这条会红，提醒我们重看 session 的注释与手册。
    """
    url = make_url("postgresql://user:pw@host:5432/db")
    assert url.get_dialect().driver == "psycopg2"


def test_to_sqlalchemy_url_adds_psycopg_dialect():
    assert (
        to_sqlalchemy_url("postgresql://shopuser:shoppass@192.168.167.130:5432/shopsage")
        == "postgresql+psycopg://shopuser:shoppass@192.168.167.130:5432/shopsage"
    )


def test_to_sqlalchemy_url_keeps_existing_dialect():
    """已经带方言的连接串原样返回（尊重调用方，例如有人显式要用 asyncpg）。"""
    for given in (
        "postgresql+psycopg://u:p@h/db",
        "postgresql+asyncpg://u:p@h/db",
        "postgresql+psycopg2://u:p@h/db",
    ):
        assert to_sqlalchemy_url(given) == given


def test_password_special_chars_must_be_percent_encoded():
    """密码里含 `@` `:` `/` 时必须 URL 编码 —— SQLAlchemy 不做自动转义，不编码就解析错。

    这条把「连接串是 URL，不是随手拼的字符串」钉成可执行事实：
    · 未编码的 `@` 会让 host 解析错（下面第一个断言就是当时的真实表现）；
    · `%40` 编码后一切正常。
    对应手册里「密码含百分号请用 .env 并注意编码」的那条坑。
    """
    unencoded = make_url("postgresql://u:p@ss@host:5432/db")
    assert unencoded.host != "host", "SQLAlchemy 居然自动处理了未编码的 @ —— 请重看这条测试的前提"

    encoded = make_url("postgresql://u:p%40ss@host:5432/db")
    assert encoded.host == "host"
    assert encoded.password == "p@ss"
    assert encoded.database == "db"


@pytest.mark.parametrize("bad", ["", "   ", "not-a-url", "//host/db"])
def test_bad_url_raises_with_actionable_message(bad: str):
    """空串/畸形串要 fail-fast，并且报错里要告诉人「去哪儿配」。"""
    with pytest.raises(ValueError) as exc:
        to_sqlalchemy_url(bad)
    assert "DATABASE_URL" in str(exc.value) or "scheme" in str(exc.value)


def test_session_module_uses_same_conversion():
    """`session` 与 `alembic/env.py` 用的是同一个转换函数，不会两处漂移。"""
    assert db_session.to_sqlalchemy_url is to_sqlalchemy_url or (
        db_session.to_sqlalchemy_url("postgresql://u:p@h/db") == to_sqlalchemy_url("postgresql://u:p@h/db")
    )


# ── 连接池参数（C1）────────────────────────────────────────────────────────────


def test_pool_limits_are_explicit():
    """池上限必须显式配置：真实上限 = pool_size + max_overflow。"""
    status = db_session.pool_status()
    assert status["pool_size"] == db_session.POOL_SIZE
    assert status["max_overflow"] == db_session.MAX_OVERFLOW
    assert status["capacity"] == db_session.POOL_SIZE + db_session.MAX_OVERFLOW


def test_engine_pool_matches_config():
    """引擎上真的挂上了 QueuePool，而不是默认池/NullPool。"""
    pool = db_session.engine.pool
    assert type(pool).__name__ == "QueuePool"
    assert pool.size() == db_session.POOL_SIZE
    assert pool._max_overflow == db_session.MAX_OVERFLOW  # noqa: SLF001 - 校验配置值，非业务逻辑


def test_pre_ping_enabled():
    """pool_pre_ping 是跨主机场景的刚需（VMware NAT 会静默丢空闲连接）。"""
    assert db_session.engine.pool._pre_ping is True  # noqa: SLF001


def test_no_connections_checked_out_at_import():
    """只 import 不该借连接（惰性连接：import 期不占数据库连接预算）。"""
    assert db_session.engine.pool.checkedout() == 0


def test_pool_size_can_be_overridden_for_tests():
    """测试要能把池压小（复现排队超时），说明池参数没有写死在引擎里。"""
    small = db_session.make_engine(
        "postgresql://u:p@127.0.0.1:5432/db", pool_size=1, max_overflow=0, pool_timeout=1
    )
    try:
        assert small.pool.size() == 1
        assert small.pool._max_overflow == 0  # noqa: SLF001
        assert small.pool._timeout == 1  # noqa: SLF001
    finally:
        small.dispose()
