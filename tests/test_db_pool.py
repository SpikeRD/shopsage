"""连接池上限与泄漏的测试（C1）。

两条最有价值的断言：
1. **并发借连接时，同时借出的数量不超过 `pool_size + max_overflow`** —— 池真的在限流；
2. **用完都还回池**（`checkedout() == 0`）—— 没有连接/session 泄漏。

另外复现一次「池打满后的排队超时」，确认失败模式是**快速失败**而不是无限挂住请求。
需要真库（池的另一端就是数据库）。
"""

from __future__ import annotations

import concurrent.futures as cf
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SATimeoutError

from app.db import session as db_session
from app.db.session import engine, make_engine

pytestmark = pytest.mark.db


def test_pool_never_checks_out_more_than_capacity():
    """45 个并发去抢 15 个连接：借出峰值必须 <= 池上限，且结束后全部归还。"""
    cap = db_session.POOL_SIZE + db_session.MAX_OVERFLOW
    lock = threading.Lock()
    peak = 0
    done = 0

    def hold() -> None:
        nonlocal peak, done
        with engine.connect() as conn:  # 借出 → 持有 → with 结束自动还回
            conn.execute(text("select 1"))
            with lock:
                peak = max(peak, engine.pool.checkedout())
                done += 1

    with cf.ThreadPoolExecutor(max_workers=cap * 3) as pool:
        for future in [pool.submit(hold) for _ in range(cap * 3)]:
            future.result(timeout=60)  # 借不到连接会等 pool_timeout，超时则抛 TimeoutError

    assert done == cap * 3, "有并发任务没跑完（可能卡在等连接上）"
    assert peak <= cap, f"连接借出峰值 {peak} 超过池上限 {cap}"
    assert peak > 1, "峰值只有 1，说明根本没有并发 —— 这条测试就没证明到池上限"
    assert engine.pool.checkedout() == 0, "有连接没还回池：典型连接/session 泄漏"
    assert engine.pool.checkedin() <= cap


def test_pool_exhaustion_fails_fast_with_timeout():
    """池压到 1 且不许溢出：第 2 个并发请求必须**快速失败**（TimeoutError），而不是无限挂住。"""
    tiny = make_engine(db_session.to_sqlalchemy_url(_current_url()), pool_size=1, max_overflow=0, pool_timeout=1)
    release = threading.Event()
    acquired = threading.Event()

    def hold_forever() -> None:
        with tiny.connect() as conn:
            conn.execute(text("select 1"))
            acquired.set()
            release.wait(timeout=10)  # 占着唯一的连接不放

    try:
        with cf.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(hold_forever)
            assert acquired.wait(timeout=10), "第一个连接没借到"

            with pytest.raises(SATimeoutError) as exc:
                with tiny.connect() as conn:
                    conn.execute(text("select 1"))
            assert "QueuePool limit" in str(exc.value)
            assert tiny.pool.checkedout() == 1  # 只有一个连接被借出（就是那个占着的）
    finally:
        release.set()
        tiny.dispose()


def test_connections_are_returned_after_session_use(seeded: None):
    """`get_db` 这类会话用法：用完（含异常路径）必须把连接还给池。"""
    from app.db.session import SessionLocal

    before = engine.pool.checkedout()
    with SessionLocal() as db:
        db.execute(text("select 1"))
    assert engine.pool.checkedout() == before

    # 异常路径也不能泄漏
    with pytest.raises(RuntimeError):
        with SessionLocal() as db:
            db.execute(text("select 1"))
            raise RuntimeError("模拟请求处理中抛异常")
    assert engine.pool.checkedout() == before


def _current_url() -> str:
    from app.config import get_settings

    return get_settings().database_url
