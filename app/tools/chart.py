"""图表工具：返回**结构化图表规范（spec）**，不返回图片（`6.tools.md` 第 5 节第 3 条）。

为什么是 spec 而不是 PNG/HTML：

* 后端不需要装绘图库（matplotlib 那套要字体、要沙箱线程安全，还会把镜像撑大几百 MB）；
* 前端（第 18 步 Web）可以按自己的组件库渲染、响应式重画、做交互；
* spec 是 JSON，**同一份结果既能给前端画图，也能给第 15 步的 MCP 客户端消费**。

本模块是纯函数：不碰数据库、不调 LLM、不读配置。它的输入就是
`sql_executor` / `db.queries` 出来的行（`list[dict]`），输出是可 `json.dumps` 的 dict。

两个容易踩的点，本模块直接处理掉
--------------------------------

1. **数据库的值不是 JSON 值**：psycopg 给回来的是 `Decimal`（金额列）与 `datetime`（timestamptz），
   直接 `json.dumps` 会 `TypeError`。所以出口统一过 `_jsonable()`（`Decimal → float`、
   时间 → ISO-8601 字符串）。⚠️ 这是**实测**撞到的：不是理论风险，是第一次把行喂给图表就会炸。
2. **图表载荷要最小化**：只带 `x` 与 `series` 声明的列，其余列一律不进 spec。
   图表数据经常被前端缓存、贴进工单、打进日志，把整行（可能含手机号/地址）带出去
   等于给「脱敏出口」开了个后门。列不是你声明要画的，就不该出现在图里。

「没有数据」与「参数写错」是两件事
----------------------------------

* **没有数据行**（`rows` 为空）→ 返回一张空图 + `meta.warnings`，不抛异常：
  这是**数据状况**，调用方应当回答「没有可展示的数据」，而不是报错；
* **列名写错、图表类型不存在、饼图配了多个系列** → 抛 `ToolContractError`：
  这是**调用方写错了**，越早炸越好（第 7 步的 agent 会把报错回喂给 LLM 让它改）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from app.tools.audit import audit_tool_call
from app.tools.errors import ToolContractError

TOOL_NAME: Final[str] = "build_chart"

# spec 的版本号：前端与 MCP 客户端据此判断字段含义，将来改结构时不必靠「猜版本」
CHART_SCHEMA: Final[str] = "shopsage.chart/v1"

# 支持的图表类型（前端渲染分支的白名单；新增类型要同时改前端）
CHART_TYPES: Final[tuple[str, ...]] = ("bar", "line", "pie", "scatter", "table")

# 单张图最多画多少个点：图不是数据导出通道，200 个点以上的柱状图人眼也读不出来
DEFAULT_MAX_POINTS: Final[int] = 50
ABSOLUTE_MAX_POINTS: Final[int] = 200

# 标题来自 LLM，长度要有界（避免把整段 prompt 塞进图表标题）
MAX_TITLE_CHARS: Final[int] = 120


def _jsonable(value: Any) -> Any:
    """把数据库里常见的值收敛成 JSON 原生类型。

    * `Decimal`（NUMERIC 金额）→ `float`：图表只需要比例关系；
      ⚠️ 精确金额的比对**不要**用图表的这份数据，回原始 `rows`（那里还是 Decimal）。
    * `datetime` / `date` / `time` → ISO-8601 字符串：前端 `new Date(...)` 能直接解析。
    * 其余未知类型 → `str()`，而不是抛错：图表不该因为一个没见过的类型整体失败。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    return str(value)


def _as_rows(rows: Sequence[Mapping[str, Any]] | Mapping[str, Any]) -> list[dict[str, Any]]:
    """入参归一成 `list[dict]`：既接受 `list[dict]`，也接受单独一行（`dict`）。"""
    if isinstance(rows, Mapping):
        return [dict(rows)]
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise ToolContractError(
            f"rows 必须是「行字典」的列表（或单个字典），收到 {type(rows).__name__}"
        )
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ToolContractError(
                f"rows[{index}] 不是映射/字典，收到 {type(row).__name__}："
                "图表的数据源应当是 app.tools.sql_executor.execute_readonly_query 的 rows"
            )
        out.append(dict(row))
    return out


def _check_field(field: Any, rows: list[dict[str, Any]], *, what: str) -> str:
    """字段名必须是字符串，且**真的出现在数据里**（有数据行时才查）。"""
    if not isinstance(field, str) or not field.strip():
        raise ToolContractError(f"{what} 必须是非空字符串（列名），收到 {field!r}")
    name = field.strip()
    if rows and not any(name in row for row in rows):
        available = sorted({key for row in rows for key in row})
        raise ToolContractError(
            f"{what} {name!r} 不在数据里，可用列：{', '.join(available)}"
            "（列名写错是最常见的图表问题，所以在有数据时直接拒绝）"
        )
    return name


def _resolve_series(
    y: str | None, series: Sequence[str] | None
) -> list[str]:
    """`y` 与 `series` 二选一（都传或都不传都是调用方的错）。"""
    if y is not None and series is not None:
        raise ToolContractError("y 与 series 只能传一个：单系列用 y，多系列用 series")
    if y is not None:
        return [y]
    if series is None:
        raise ToolContractError("必须指定要画的数值列：单系列用 y=<列名>，多系列用 series=[<列名>, ...]")
    if isinstance(series, (str, bytes)) or not isinstance(series, Sequence) or not series:
        raise ToolContractError("series 必须是非空的列名列表，例如 series=['paid', 'refunded']")
    return list(series)


def _to_number(value: Any, *, field: str, index: int, chart_type: str) -> float | None:
    """值 → 数值；`None` 原样保留（NULL 是数据事实，不是错误）。

    非数值且非空 → 契约错误：把 `'N/A'` 静默当成 0 画出一条假曲线，比报错危险得多。
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool 是 int 的子类，但画成 0/1 没有意义
        raise ToolContractError(f"rows[{index}][{field!r}] 是布尔值，图表需要数值列")
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise ToolContractError(
                f"{chart_type} 图的系列列 {field!r} 必须是数值："
                f"rows[{index}] 是 {value!r}（先在 SQL 里聚合/过滤，或改用 table 类型）"
            ) from exc
    raise ToolContractError(f"rows[{index}][{field!r}] 类型不支持：{type(value).__name__}")


def build_chart_spec(
    rows: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    x: str,
    y: str | None = None,
    series: Sequence[str] | None = None,
    chart_type: str = "bar",
    title: str | None = None,
    max_points: int = DEFAULT_MAX_POINTS,
    sort_by: str | None = None,
    descending: bool = False,
    caller: str | None = None,
) -> dict[str, Any]:
    """把查询结果组装成结构化图表 spec。

    参数：
        rows: 行数据（`list[dict]` 或单行 `dict`）。
        x: X 轴/分类列名（必填）。
        y: 单系列时的数值列名。
        series: 多系列时的数值列名列表（与 `y` 二选一）。
        chart_type: `bar` / `line` / `pie` / `scatter` / `table` 之一。
        title: 图表标题（可省略，默认「<系列> / <X>」）。
        max_points: 最多画多少个点（只能调低，绝对上限 `ABSOLUTE_MAX_POINTS`）。
        sort_by: 按哪一列排序（`x` 或某个系列列名）；`None` = 保持 SQL 的顺序。
        descending: 排序方向（默认升序；做「Top N」时传 True）。
        caller: 审计字段「谁在调用」（必填）。

    返回：可直接 `json.dumps` 的 spec（含 `schema` 版本号、`meta` 审计与质量信息）。
    """
    with audit_tool_call(
        TOOL_NAME,
        caller=caller,
        rows_count=len(rows) if isinstance(rows, Sequence) and not isinstance(rows, str) else None,
        x=x,
        y=y,
        series=series,
        chart_type=chart_type,
        max_points=max_points,
    ) as handle:
        if chart_type not in CHART_TYPES:
            raise ToolContractError(
                f"不支持的图表类型 {chart_type!r}，只能是：{', '.join(CHART_TYPES)}"
            )
        if isinstance(max_points, bool) or not isinstance(max_points, int) or max_points < 1:
            raise ToolContractError(f"max_points 必须是 >= 1 的整数，收到 {max_points!r}")
        points_cap = min(max_points, ABSOLUTE_MAX_POINTS)

        data_rows = _as_rows(rows)
        x_field = _check_field(x, data_rows, what="x")
        fields = [_check_field(name, data_rows, what="series 列名") for name in _resolve_series(y, series)]

        if chart_type == "pie" and len(fields) != 1:
            raise ToolContractError(
                f"饼图只能有 1 个系列，收到 {len(fields)} 个（{', '.join(fields)}）："
                "多个系列请改用 bar，或分别画两张饼图"
            )
        if sort_by is not None and sort_by not in ("x", *fields):
            raise ToolContractError(
                f"sort_by 只能是 'x' 或系列列名之一（{', '.join(fields)}），收到 {sort_by!r}"
            )

        warnings: list[str] = []
        if not data_rows:
            warnings.append("没有数据行：图表为空（这是数据状况，不是调用错误）")

        points = data_rows[:points_cap]
        truncated_points = len(data_rows) > points_cap
        if truncated_points:
            warnings.append(
                f"数据有 {len(data_rows)} 行，超过单图上限 {points_cap} 点，只画前 {points_cap} 行"
                "（图表不是数据导出通道；需要全量请直接看查询结果或改用分页）"
            )

        null_values = 0
        data: list[dict[str, Any]] = []
        for index, row in enumerate(points):
            item: dict[str, Any] = {x_field: _jsonable(row.get(x_field))}
            for name in fields:
                if name not in row:
                    null_values += 1
                    item[name] = None
                    continue
                number = _to_number(row[name], field=name, index=index, chart_type=chart_type)
                if number is None:
                    null_values += 1
                item[name] = number
            data.append(item)

        if sort_by is not None:
            key = x_field if sort_by == "x" else sort_by
            data.sort(
                key=lambda item: (item.get(key) is None, item.get(key)),
                reverse=descending,
            )
        if null_values:
            warnings.append(f"{null_values} 个单元格是空值（NULL）：图上会缺这些点，未做 0 填充")

        resolved_title = (title or f"{' / '.join(fields)} · {x_field}").strip().replace("\n", " ")
        if len(resolved_title) > MAX_TITLE_CHARS:
            resolved_title = resolved_title[:MAX_TITLE_CHARS] + "…"

        spec: dict[str, Any] = {
            "schema": CHART_SCHEMA,
            "type": chart_type,
            "title": resolved_title,
            "x": {"field": x_field, "label": x_field},
            "series": [{"name": name, "field": name, "type": chart_type} for name in fields],
            "data": data,
            "meta": {
                "audit_id": handle.audit_id,
                "caller": handle.caller,
                "source_rows": len(data_rows),
                "points": len(data),
                "truncated_points": truncated_points,
                "null_values": null_values,
                "sorted_by": sort_by,
                "descending": bool(descending) if sort_by else False,
                "warnings": warnings,
            },
        }

        handle.rows = len(data)
        handle.details.update(
            {"chart_type": chart_type, "points": len(data), "truncated_points": truncated_points}
        )
        return spec


__all__ = [
    "ABSOLUTE_MAX_POINTS",
    "CHART_SCHEMA",
    "CHART_TYPES",
    "DEFAULT_MAX_POINTS",
    "build_chart_spec",
]
