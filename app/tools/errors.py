"""工具层错误分类：**可恢复**（可重试/可重投）与**不可恢复**（立刻抛）分开。

`6.tools.md` 第 5 节把工具定位成「唯一副作用出口」，那么「调用失败了怎么办」就必须有明确答案：
第 12 步的队列（`12.queue.md`）要按异常类型决定「延迟重投」还是「直接进死信人工兜底」。
本模块只回答这一个问题，不碰业务逻辑 —— 与 `app/llm/errors.py` 是同一套口径
（那里管模型侧，这里管工具/数据库侧），两处的分类标准一致，队列才不用写两套判断。

三个刻意的设计决定
------------------

1. **安全异常不在这里包装**：`SQLSecurityError`（拦 SQL）与 `PermissionDenied`（拦身份）
   由 `app/security/` 定义，工具层**原样抛出**，不翻译成本模块的异常。
   理由：它们带着 `rule` / `missing` 字段，第 9 步的 FastAPI 要把「SQL 不合规」映射成 400、
   「越权」映射成 403；包一层就丢了这两个字段，错误响应会退化成「500 未知错误」。
2. **数据库错误的默认方向是「不可恢复」**：只有白名单里的 SQLSTATE 才判可重试
   （序列化失败、死锁、连接被断、库在重启）。未知错误重试 = 拿真实流量赌运气，
   而且写路径重试还可能造成重复副作用 —— 与 `is_retryable()` 在白名单口径上完全一致。
3. **重试判定与「谁来重试」解耦**：本模块只回答「这个错误能不能重试」，
   退避与次数由调用方（第 12 步队列 / 第 5 步 LLM 门面）决定。
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.exc import (
    DBAPIError,
    DataError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.exc import TimeoutError as SATimeoutError


class ToolError(RuntimeError):
    """工具层对外统一错误基类（调用方只需 catch 这一个）。"""


class PermanentToolError(ToolError):
    """不可恢复：**不要重试**，也不要重投队列 —— 报错给用户或走人工兜底。"""


class RetryableToolError(ToolError):
    """可恢复：连接抖动/死锁/库重启，稍后重试大概率能成功。"""


class ToolContractError(PermanentToolError):
    """入参或结果契约不满足（枚举值非法、字段不存在、条数对不上…）。

    与「业务规则不满足」的区别：契约错误是**调用方写错了**，业务拒绝是**请求本身合理但当前不允许**
    （如「该订单状态不支持退款」，由调用方按业务异常处理）。两者都不重试，所以同为 Permanent。
    """


class ToolNotAvailableError(PermanentToolError):
    """依赖的能力尚未就绪（典型：`rag/` 属第 10 步，现在还没落地）。

    单独一个类型是为了让报错**能照着修**：消息里会写明缺什么、什么时候会有、怎么绕开。
    把 `ModuleNotFoundError` 原样抛给调用方，等于让上游去猜「是环境问题还是代码问题」。
    """


# ── 数据库错误的可重试性判定（白名单）─────────────────────────────────────────
# SQLSTATE 取自 PostgreSQL 官方附录 A；只登记「重试确实有意义」的那几个：
RETRYABLE_SQLSTATES: Final[frozenset[str]] = frozenset(
    {
        # 并发冲突：事务被数据库主动回滚，重跑一遍就能过去
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        # 连接层瞬时故障：连接被中间设备/运维动作掐断（pool_pre_ping 挡不住执行中途的断连）
        "08000",  # connection_exception
        "08001",  # sqlclient_unable_to_establish_sqlconnection
        "08003",  # connection_does_not_exist
        "08004",  # sqlserver_rejected_establishment_of_sqlconnection
        "08006",  # connection_failure
        # 库侧容量/运维状态：等一等恢复正常
        "53300",  # too_many_connections
        "57P01",  # admin_shutdown
        "57P03",  # cannot_connect_now
    }
)

# 明确**不可**重试的异常类型：约束冲突、语法/对象不存在、数据类型不匹配。
# 这些重试 100 次结果一样，只会让「同一个错误」被犯 100 次。
_PERMANENT_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    IntegrityError,  # 23505 唯一冲突 / 23503 外键 / 23514 CHECK
    ProgrammingError,  # 42xxx：表/列/函数不存在、参数个数不对
    DataError,  # 22xxx：类型/范围/除零
)

# 明确可重试的异常类型：连接层与池层
_RETRYABLE_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    OperationalError,  # 连接被断、认证失败重连、语句被取消
    InterfaceError,  # 驱动层连接失效
    SATimeoutError,  # 连接池借不到连接（pool_timeout）
)


def _sqlstate(exc: BaseException) -> str:
    """取 PostgreSQL SQLSTATE（psycopg 把它放在 `exc.orig.sqlstate` 上）。

    取不到就返回空串：**宁可不重试**，也不要把「不知道是什么错」当成可恢复。
    """
    for candidate in (getattr(exc, "orig", None), exc):
        state = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(state, str) and state:
            return state
    return ""


def is_retryable_db_error(exc: BaseException) -> bool:
    """这个数据库错误值不值得重试。

    ⚠️ 判定顺序不能反：`IntegrityError` / `ProgrammingError` 都是 `DBAPIError` 的子类，
    先按「是不是 DBAPIError」放行会把「唯一冲突」误判成「可重试」——
    写路径上那就是**重复建单**的直接成因。所以先判具体类型，再看 SQLSTATE 白名单。
    """
    if isinstance(exc, _PERMANENT_EXCEPTIONS):
        return False
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, DBAPIError):
        return _sqlstate(exc) in RETRYABLE_SQLSTATES
    return False


def translate_db_error(exc: BaseException, *, action: str) -> ToolError:
    """把 SQLAlchemy/驱动异常翻译成本模块的两种对外错误。

    调用方写法：`raise translate_db_error(exc, action="执行只读查询") from exc` ——
    `from exc` 保住原始异常链，排障时还能看到 SQLSTATE 与原始报文。
    """
    state = _sqlstate(exc)
    detail = f"{type(exc).__name__}" + (f" [SQLSTATE {state}]" if state else "")
    if is_retryable_db_error(exc):
        return RetryableToolError(f"{action}遇到可恢复的数据库错误（{detail}）：稍后重试")
    return PermanentToolError(
        f"{action}失败且不可重试（{detail}）：{exc}"
        "。参数/表/约束类错误重试也没有意义，请先修调用方。"
    )


__all__ = [
    "RETRYABLE_SQLSTATES",
    "PermanentToolError",
    "RetryableToolError",
    "ToolContractError",
    "ToolError",
    "ToolNotAvailableError",
    "is_retryable_db_error",
    "translate_db_error",
]
