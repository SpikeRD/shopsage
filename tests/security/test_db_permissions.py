"""只读角色的真库验收（`4.security.md` §8 的只读角色验收项，需要真库）。

对应 `3.db-runbook.md` §5.3 决策记录第 5 条：「用只读 DSN 执行 `DELETE FROM orders` /
`CREATE TABLE t(...)` 必须报权限错误」。

**这条为什么必须打在真库上**：SQL 校验（`sql_guard`）是应用层的闸，能被绕过；
只读角色是**数据库层**的闸，绕过应用层也没用 —— 它的证据只能是「连上去试，数据库真的拒了」。
本模块与 `tests/security/test_sql_guard.py` 是**互补**关系，不是重复。
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.db.session import get_read_engine, get_write_engine

pytestmark = pytest.mark.db

READONLY_ROLE = "shopsage_readonly"


def _read_dsn() -> str:
    return getattr(get_settings(), "database_read_url", "") or ""


def test_read_url_is_configured_for_readonly_role():
    """`.env` 必须真的配了指向只读角色的 DSN —— 否则「只读」只在命名上成立。"""
    dsn = _read_dsn()
    if not dsn:
        pytest.skip(
            "DATABASE_READ_URL 未配置：先跑 `uv run python -m scripts.db_readonly`"
            "（见 docs/runbook/4.security-runbook.md §3）"
        )
    assert READONLY_ROLE in dsn, f"DATABASE_READ_URL 指向的不是只读角色：{dsn.split('@')[-1]}"
    assert dsn != get_settings().database_url, "读写用了同一个 DSN → 只读没有落地"


def test_settings_exposes_read_url_field_and_masks_it():
    """`database_read_url` 必须是 `Settings` 的正规字段，且**不能**在显示出口泄露密码。"""
    settings = get_settings()
    assert "database_read_url" in type(settings).model_fields
    dumped = settings.dump_for_logging()["database_read_url"]
    if dumped:
        assert "***" in dumped
        assert "ShopsageReadonly" not in dumped


def test_read_engine_is_separate_from_write_engine():
    """配了只读 DSN 之后，读引擎必须是与写引擎不同的对象（各自一套池）。"""
    if not _read_dsn():
        pytest.skip("DATABASE_READ_URL 未配置")
    assert get_read_engine() is not get_write_engine()


def test_read_engine_connects_as_readonly_role():
    """读引擎落地到的连接身份必须是只读角色（`current_user` 是最直接的证据）。"""
    if not _read_dsn():
        pytest.skip("DATABASE_READ_URL 未配置")
    with get_read_engine().connect() as conn:
        assert conn.exec_driver_sql("select current_user").scalar() == READONLY_ROLE


def test_read_engine_can_select():
    if not _read_dsn():
        pytest.skip("DATABASE_READ_URL 未配置")
    with get_read_engine().connect() as conn:
        rows = conn.exec_driver_sql("select count(*) from orders where user_id = 1").scalar()
    assert rows is not None


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("DELETE", "DELETE FROM orders"),
        ("UPDATE", "UPDATE orders SET status = 'x'"),
        ("INSERT", "INSERT INTO orders (order_id) VALUES (-1)"),
        ("TRUNCATE", "TRUNCATE TABLE orders"),
        ("CREATE TABLE", "CREATE TABLE t_should_fail (id int)"),
        ("DROP TABLE", "DROP TABLE orders"),
        ("CREATE ROLE", "CREATE ROLE r_should_fail"),
        ("ALTER TABLE", "ALTER TABLE orders ADD COLUMN evil int"),
    ],
)
def test_read_engine_denies_writes_at_database_layer(label: str, statement: str):
    """**只读角色的核心证据**：写操作在数据库层报权限错误，而不是靠应用层自觉。

    用 `psycopg.errors.InsufficientPrivilege`（SQLSTATE 42501）而非「任意异常」作为判据：
    要求「因为权限被拒」，而不是「碰巧报了个别的错」（例如表名写错也会报错，但那证明不了只读）。
    """
    if not _read_dsn():
        pytest.skip("DATABASE_READ_URL 未配置")

    import psycopg

    with get_read_engine().connect() as conn, pytest.raises(Exception) as exc:  # noqa: PT011
        conn.exec_driver_sql(statement)

    wrapped = getattr(exc.value, "orig", exc.value)
    assert isinstance(wrapped, psycopg.errors.InsufficientPrivilege), (
        f"{label} 的失败原因不是权限不足：{type(wrapped).__name__}: {wrapped}"
    )


def test_write_engine_still_works_for_its_own_purpose():
    """只读收口不能误伤写路径：写引擎仍能执行它该做的事（这里是只读查询，成本最低）。"""
    with get_write_engine().connect() as conn:
        assert conn.exec_driver_sql("select current_user").scalar() == "shopuser"


def test_readonly_role_has_no_superuser_or_bypassrls():
    """角色属性核查：只读角色不能带 SUPERUSER / BYPASSRLS（否则权限检查会被跳过）。"""
    if not _read_dsn():
        pytest.skip("DATABASE_READ_URL 未配置")
    with get_read_engine().connect() as conn:
        row = conn.exec_driver_sql(
            "select rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
            "from pg_roles where rolname = 'shopsage_readonly'"
        ).one()
    assert row == (False, False, False, False), f"只读角色属性不对：{row}"
