"""只读 SQL 执行器（C1 的执行侧落点，`6.tools.md` 第 5 节的两条核心技术点）。

Agent **不直接碰数据库**：它把 SQL 交给本函数，本函数负责「校验 → 只读引擎 → 截断 → 审计」。

四道处理，缺一不可
------------------

1. **二次校验（纵深防御）**：即使 `agents/data.py` 已经调过 `validate_sql`，这里**再验一遍**。
   理由不是重复劳动：工具会被 `mcp/`（第 15 步）与其他代码路径**直接调用**，
   而那些路径不经过 agent；安全校验放在「入口」而不是「调用者的自觉」上，才谈得上纵深防御。
2. **只读引擎**：强制走 `get_read_engine()`（第 4 步落地的 `shopsage_readonly` 角色）。
   于是「写操作」在**两层**同时被挡：SQL 守卫（静态）+ 数据库权限（运行时）。
   第 4 步给的是「库层写不进去」的证据，本函数是那条只读连接的第一个业务消费者。
3. **行数硬上限 + 截断标记**：多取一行做探针（`上限 + 1`），够上限就置 `truncated=True`
   并把多出来的那行丢掉。这是 C1「限制返回行数」的执行侧落点 ——
   校验层能拦「写死的 LIMIT 999999」，但拦不住 `LIMIT :n`（值在运行期才定），
   所以执行侧必须自己兜住：**绑定参数里的超大 LIMIT 会被就地夹到「上限 + 1」**。
4. **审计**：校验失败也留痕（谁在什么时候试图执行 `DROP TABLE`）。见 `app/tools/audit.py`。

返回什么
--------

`(rows, meta)` 二元组（与 `6.tools.md` 第 7 节的用例签名一致）：

* `rows`：`list[dict]`，键是列名；值保持驱动原样（`Decimal` / `datetime` / `str`…），
  **不在这一层做 JSON 化** —— 脱敏（`security.masking`）与出网序列化是各自的职责，
  在这里顺手转成字符串会让下游拿不到数值（图表要算、金额要比）。
* `meta`：行数、`truncated`、列名、落到的物理表、生效上限、耗时、审计 id。

⚠️ `6.tools.md` 第 7 节的示例写的是 `execute_readonly_query("SELECT * FROM orders")`。
实测按本仓的守卫规则，这条 SQL 会被 `missing_where` 拦下（没有 WHERE 的整表查询一律拒绝），
所以「截断」的验证用例必须带上 WHERE —— 见 `docs/runbook/6.tools-runbook.md` §4 与 §10.1 坑 1。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Final

import sqlglot
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlglot import exp

from app.db.session import get_read_engine
from app.observability.instrument import record_sql_query
from app.security import policy
from app.security.sql_guard import DIALECT, ValidationResult, validate_sql
from app.tools.audit import audit_tool_call
from app.tools.errors import ToolContractError, translate_db_error

# 审计里的工具名（与 `6.tools.md`／`15.mcp.md` 里的 `sql_query` 对齐，便于日志按工具归并）
TOOL_NAME: Final[str] = "sql_query"

# 默认行数上限 = 守卫的上限（`policy.MAX_ROWS`）。调用方可以**调低**（例如「只取 5 行画图」），
# 但不能调高：绝对上限只有一处真相，见 `_resolve_rows_cap`。
DEFAULT_MAX_ROWS: Final[int] = policy.MAX_ROWS
ABSOLUTE_MAX_ROWS: Final[int] = policy.MAX_ROWS


def _resolve_rows_cap(max_rows: int | None) -> int:
    """把调用方给的行数上限夹到 `[1, ABSOLUTE_MAX_ROWS]`。

    为什么要夹：`max_rows` 是**调用方参数**，而调用方可能是被 LLM 影响的代码路径。
    「允许调用方放宽上限」等于把 C1 的上限变成建议值 —— 所以上限只能变严、不能变宽。
    """
    if max_rows is None:
        return DEFAULT_MAX_ROWS
    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        raise ToolContractError(f"max_rows 必须是整数，收到 {type(max_rows).__name__}")
    if max_rows < 1:
        raise ToolContractError(f"max_rows 必须 >= 1，收到 {max_rows}")
    return min(max_rows, ABSOLUTE_MAX_ROWS)


def _outmost_select(sql: str) -> exp.Expression | None:
    """取最外层 SELECT（UNION 取最后一个分支，与 `sql_guard._outmost_select` 同口径）。

    解析失败返回 `None`：本函数只是**加固**用的（找绑定参数形式的 LIMIT），
    真正的「解析不了就拒绝」在 `validate_sql` 里，不在这里重复判一次。
    """
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:  # noqa: BLE001 - 解析失败的处置权归 validate_sql
        return None
    if isinstance(tree, exp.Select):
        return tree
    selects = list(tree.find_all(exp.Select))
    return selects[-1] if selects else None


def limit_placeholder_name(sql: str) -> str | None:
    """最外层 `LIMIT` 若写成占位符（`:n`），返回它的绑定参数名，否则 `None`。

    实测（sqlglot 30.19.0）：`LIMIT :n` 与 `LIMIT %(n)s` 都解析成 `exp.Placeholder`，
    `.this` 就是参数名；字面量则是 `exp.Literal`。
    """
    outmost = _outmost_select(sql)
    if outmost is None:
        return None
    limit = outmost.args.get("limit")
    if limit is None:
        return None
    expression = getattr(limit, "expression", None)
    if isinstance(expression, exp.Placeholder):
        return str(expression.this)
    return None


def clamp_bound_limit(
    sql: str, params: Mapping[str, Any] | None, ceiling: int
) -> tuple[dict[str, Any], bool]:
    """把「绑定进 `LIMIT :n` 的超大值」夹到 `ceiling`，返回 `(参数, 是否夹过)`。

    为什么必须做：`validate_sql` 对占位符 LIMIT 只能**声明**上限（`ValidationResult.limit`），
    SQL 文本里的 `LIMIT :n` 原样保留 —— 守卫看不穿运行期才定下来的值。
    如果不管它，一条 `LIMIT 1000000000` 仍会让数据库真去准备十亿行（C1 要防的正是这个），
    而我们只在客户端 `fetchmany` 截断，等于「带宽保住了、库还是被打爆」。

    夹到 `ceiling = 上限 + 1` 而不是 `上限`：多留一行给 `truncated` 探针用（见模块 docstring 第 3 条）。
    非整数/取不到的值**不动**（交给数据库按类型错误拒绝，那是不可恢复错误）。
    """
    bound: dict[str, Any] = dict(params or {})
    name = limit_placeholder_name(sql)
    if name is None or name not in bound:
        return bound, False

    value = bound[name]
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return bound, False
    try:
        number = int(value)
    except (TypeError, ValueError):
        return bound, False

    if number <= ceiling:
        return bound, False
    bound[name] = ceiling
    return bound, True


def execute_readonly_query(
    sql: str,
    params: Mapping[str, Any] | None = None,
    *,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    caller: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """校验并执行一条只读查询，返回 `(rows, meta)`。

    参数：
        sql: 单条 SELECT（守卫会拒绝多语句/写语句/无 WHERE/超上限）。
        params: 绑定参数（`text()` 的 `:name` 形式）；**永远用参数绑定，不要拼字符串**。
        max_rows: 本次结果的行数上限（只能调低，见 `_resolve_rows_cap`）。
        caller: 审计字段「谁在调用」，必填（见 `app/tools/audit.py`）。

    异常：
        `SQLSecurityError`：SQL 未过校验（**原样抛出**，带 `rule` 字段，第 9 步映射成 400）；
        `ToolContractError`：参数形状不合法；
        `RetryableToolError` / `PermanentToolError`：数据库侧执行失败（分类见 `errors.py`）；
        `PermissionDenied`：只读角色没有该表的 SELECT 权限（第 4 步的授权没跑）。
    """
    rows_cap = _resolve_rows_cap(max_rows)

    # 审计包住**整个**调用（含校验失败的那条路径）：
    # 「谁试图执行 DROP TABLE」和「谁成功查了订单」是同等重要的两条证据。
    with audit_tool_call(
        TOOL_NAME, caller=caller, sql=sql, params=params, max_rows=rows_cap
    ) as handle:
        # ① 二次校验（纵深防御：工具会被 mcp/ 等路径直接调用，不依赖调用者先验过）
        validation: ValidationResult = validate_sql(sql, max_rows=rows_cap)

        # ② 占位符 LIMIT 夹取（校验层看不穿的那部分上限，由执行层兜住）
        bound, bound_clamped = clamp_bound_limit(validation.sql, params, rows_cap + 1)

        # ③ 只读引擎 + 流式游标 + 「多取一行」探针
        #
        # 第 11 步：埋点用 `with` 包住**执行这一段**（而不是包住整个函数）——
        # `SQL_DURATION` 的语义是「数据库花了多久」，而校验/夹取/审计都是我们自己的 CPU 时间。
        # 把校验算进去会让「库慢」与「守卫复杂」混成一个数字，而两者的处置完全不同。
        # ⚠️ SQL 文本**不进埋点**（它可能含业务数据/字面量），只进审计（`6.tools.md` 的落点）。
        with record_sql_query(tool=TOOL_NAME) as call:
            started = time.perf_counter()
            try:
                with get_read_engine().connect() as conn:
                    # stream_results：服务端游标，逐批取 —— 避免「为了拿 200 行先把结果集全读进内存」。
                    # 它和第 4 条的 fetchmany 上限是**两层**保护：一层防内存，一层防语义。
                    stream = conn.execution_options(stream_results=True)
                    result = stream.execute(text(validation.sql), bound)
                    columns = [str(name) for name in result.keys()]
                    fetched = result.mappings().fetchmany(rows_cap + 1)
            except SQLAlchemyError as exc:
                # **失败的 SQL 也要埋点**：只埋成功的查询会让「库挂了/慢查询被 kill」
                # 在看板上表现为「这段时间没有数据」—— 而「没有数据」与「一切正常」
                # 在图上长得一样（这是监控最经典的骗局）。
                # 异常会穿透 `_ToolCall.__exit__` 并自动被 span 记成 ERROR（见其说明）。
                raise translate_db_error(exc, action=f"执行只读查询（{TOOL_NAME}）") from exc

            duration_ms = int((time.perf_counter() - started) * 1000)
            truncated = len(fetched) > rows_cap
            rows = [dict(mapping) for mapping in fetched[:rows_cap]]
            # 记 `rows` 与 `truncated` 两个属性，而不是只记耗时：
            # 「这条查询慢了 4 秒」的下一步永远是「它取回了几行」——
            # 取回 200 行（被截断）与取回 1 行是两条完全不同的故事（前者要改 SQL，后者要查索引）。
            call.set_attributes({"rows": len(rows), **({"degraded": True} if truncated else {})})
            call.outcome("truncated" if truncated else "rows")

        meta: dict[str, Any] = {
            "audit_id": handle.audit_id,
            "row_count": len(rows),
            "truncated": truncated,
            "columns": columns,
            "tables": list(validation.tables),
            "declared_limit": validation.limit,
            "limit_clamped": validation.limit_clamped,  # 守卫侧夹的（SQL 里的字面量）
            "bound_limit_clamped": bound_clamped,  # 执行侧夹的（绑定参数里的超大值）
            "rows_cap": rows_cap,
            "duration_ms": duration_ms,
        }

        # 审计字段：结果行数 + 本次特有的判定（截断/落表/是否夹过上限）
        handle.rows = len(rows)
        handle.details.update(
            {
                "truncated": truncated,
                "tables": list(validation.tables),
                "rows_cap": rows_cap,
                "bound_limit_clamped": bound_clamped,
            }
        )
        return rows, meta


__all__ = [
    "ABSOLUTE_MAX_ROWS",
    "DEFAULT_MAX_ROWS",
    "TOOL_NAME",
    "clamp_bound_limit",
    "execute_readonly_query",
    "limit_placeholder_name",
]
