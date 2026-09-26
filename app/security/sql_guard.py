"""SQL 静态守卫（D1 静态层 / C1）：**用 AST 校验，不用字符串匹配**。

`4.security.md` 第 4 节第 1 条的落点：正则挡不住大小写变形、注释插入、换行绕过、
子查询与 UNION 变体；`sqlglot` 解析成语法树之后，「是不是只读 SELECT」「查了哪些表」
「WHERE 在哪一层」「LIMIT 是多少」都变成**结构判断**，而不是猜字符串。

本模块只做「拦截」，不做「生成 SQL」；它不连数据库，纯函数、可离线单测。

拦截清单（缺一不可，`4.security.md` 第 4 节第 3 条讲的就是这个组合）：
1. 多语句（`SELECT 1; DROP TABLE users`）—— 只按第一条解析是**静默放行**，必须显式拦；
2. 非只读语句（DROP/DELETE/UPDATE/INSERT/TRUNCATE/ALTER/GRANT…，「黑名单」）；
3. 读写不分离的写入形态（`SELECT ... INTO t`、`WITH x AS (DELETE ...)`）；
4. 表不在白名单、列不在白名单（「白名单」，防「查了不该查的表/列」）；
5. 带 FROM 却不带 WHERE（全表扫描：既防越权拖库，也防拖垮库）；
6. 恒真条件（`WHERE 1=1` / `WHERE TRUE`）—— 看着像过滤，其实等于没过滤；
7. 危险函数（`pg_read_file` / `dblink` / `pg_sleep`…）；
8. `LIMIT` 缺失的字面值上界超出 `MAX_ROWS`（C1：单条查询不返回百万行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import re

import sqlglot
from sqlglot import exp

from app.security import policy

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注
    from collections.abc import Iterable

# 解析方言：本仓跑 PostgreSQL 16。显式写死是**故意的** —— 方言会影响 AST 形状
# （尤其反引号/双引号与 `::` 类型转换），不同方言解析出的树不一样，不写就会随默认值漂移。
DIALECT: Final[str] = "postgres"

# 匹配 SQL 文本结尾处的 `LIMIT <裸整数>`（收紧 C1 上限时就地改写用；占位符不匹配）
_LIMIT_INT_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\blimit\s+(\d+)\s*;?\s*$")

# 「标量聚合」：无论命中多少行都只返回一个值，因此结果集长度与行数无关。
# 其余聚合（`array_agg` / `string_agg` / `json_agg` …）会把多行内容塞进一行 ——
# 结果同样只有一行，但**内容是**多行数据，C1 的行数上限恰恰要拦这个，所以不在集合里。
_SCALAR_AGGREGATES: Final[tuple[type[exp.Expression], ...]] = (
    exp.Count,
    exp.Sum,
    exp.Min,
    exp.Max,
    exp.Avg,
)


class SQLSecurityError(Exception):
    """SQL 未通过安全校验。`4.security.md` 第 7 节的用例全部依赖这个异常类型。

    携带 `rule` 字段是为了让「拦截率」可统计：测试与线上日志都按规则名归并，
    而不是靠匹配报错文本（文本一改，统计就废）。
    """

    def __init__(self, message: str, *, rule: str, sql: str = "") -> None:
        super().__init__(message)
        self.rule = rule
        self.sql = sql


@dataclass(frozen=True)
class ValidationResult:
    """校验结论。通过校验时拿到的是「可执行 SQL + 审计信息」。"""

    sql: str  # 可直接执行的 SQL 文本（超出上限时 LIMIT 已就地收紧）
    tables: tuple[str, ...]  # 实际引用的物理表（含子查询/CTE 里的）——审计要的
    columns: tuple[str, ...]  # 实际引用的列
    limit: int | None  # 生效的行数上限；None = 该查询无 LIMIT 需求（如主键等值查询）
    limit_clamped: bool = False  # 是否因为超过 MAX_ROWS 被就地收紧
    notes: tuple[str, ...] = field(default=())  # 给人看的判读提示


# ── 公开接口 ─────────────────────────────────────────────────────────────────


def validate_sql(
    sql: str,
    *,
    allow_tables: Iterable[str] | None = None,
    protected_columns: dict[str, frozenset[str]] | None = None,
    read_only: bool = True,
    require_where: bool = True,
    max_rows: int = policy.MAX_ROWS,
) -> ValidationResult:
    """校验一条 SQL 是否可安全执行；不安全就抛 `SQLSecurityError`。

    参数：
        allow_tables: 表白名单；`None` = 用 `policy.ALLOWED_TABLES`（**默认拒绝**）。
        protected_columns: 受保护（高敏）列表；`None` = 用 `policy.PROTECTED_COLUMNS`。
            表内列出的列被拒绝，其余列放行（理由见 `policy.PROTECTED_COLUMNS` 注释）。
        read_only: 强制只读。传 `False` 也只是「允许读写语句不被本条拦」，
            危险语句黑名单**始终生效**（不存在「关掉安全检查」的开关）。
        require_where: 带 FROM 的 SELECT 必须带 WHERE（防全表扫描）。对客路径永远为 True。
        max_rows: 单条查询行数上限（C1）。

    返回：`ValidationResult`（含可执行 SQL 与审计信息）。
    """
    if not sql or not sql.strip():
        raise SQLSecurityError("SQL 为空", rule="empty_sql")

    tables_allow = frozenset(t.lower() for t in (allow_tables or policy.ALLOWED_TABLES))
    protected = policy.PROTECTED_COLUMNS if protected_columns is None else protected_columns

    # ① 多语句：`sqlglot.parse_one` 只返回第一条、**静默丢掉后面**的语句，
    #    所以 `SELECT 1; DROP TABLE users` 用它解析「看起来是干净的 SELECT」。
    #    实测（sqlglot 30.19.0）：parse() 得到 2 条；parse_one() 直接返回第一条。
    statements = _parse_statements(sql)
    if len(statements) > 1:
        raise SQLSecurityError(
            f"拒绝多语句 SQL：解析出 {len(statements)} 条语句，只允许单条 SELECT",
            rule="multiple_statements",
            sql=sql,
        )
    root = statements[0]

    # ② 只读：语句类型必须是 SELECT（黑名单）
    if not isinstance(root, exp.Select) and not isinstance(root, exp.Union):
        raise SQLSecurityError(
            f"拒绝非查询语句：只允许 SELECT，收到 {type(root).__name__.upper()}",
            rule="not_read_only",
            sql=sql,
        )
    if _has_write_node(root):
        raise SQLSecurityError("拒绝读写混用语句（如 SELECT ... INTO / WITH ... AS (DELETE ...)）", rule="write_in_read", sql=sql)

    # ③ 白名单：表 + 列（默认拒绝）
    ctx = _QueryScope.build(root, protected)
    _check_tables(ctx, tables_allow, sql)
    _check_star(ctx, root, sql)
    _check_columns(ctx, root, sql)

    # ④ 危险函数（含各层 SELECT 的投影：`pg_sleep` / `pg_read_file` 这类即使没有表也要拦）
    _check_functions(root, sql)
    _check_select_projections(root, sql)

    # ⑤ WHERE / 全表扫描 / 恒真条件（逐层查，子查询与 UNION 分支都要查）。
    # ⚠️ 必须在 ⑥ 的 LIMIT 检查**之前**：LIMIT 那一步有一条「主键等值查询可免 LIMIT」的豁免，
    #    而 `WHERE 1=1 OR user_id = 5` 恰好长得像等值查询 —— 先跑豁免就会把恒真条件放过去
    #    （实测踩过：这条顺序错了，`WHERE 1=1` 系列全部 ALLOW）。
    if require_where:
        _check_row_filters(root, sql)

    # ⑥ LIMIT（C1）
    limit, limit_clamped, out_sql = _check_limit(root, sql, max_rows)

    return ValidationResult(
        sql=out_sql,
        tables=ctx.physical_tables(),
        columns=tuple(sorted({c.name.lower() for c in root.find_all(exp.Column)})),
        limit=limit,
        limit_clamped=limit_clamped,
        notes=(
            "只读 + 白名单 + WHERE + LIMIT 四道校验全过",
            f"生效行数上限 {limit}" if limit is not None else "无 LIMIT（按主键等值查询放行）",
        ),
    )


# ── 内部：解析 ───────────────────────────────────────────────────────────────


def _parse_statements(sql: str) -> list[exp.Expression]:
    """解析成语句列表；语法错误也归为「不安全」（校验不了的一律不放行）。"""
    try:
        statements = sqlglot.parse(sql, read=DIALECT)
    except Exception as exc:  # noqa: BLE001 - sqlglot 的异常类型不稳定，统一转成安全异常
        raise SQLSecurityError(f"SQL 解析失败（校验不了的一律拒绝）：{exc}", rule="parse_error", sql=sql) from exc

    parsed = [s for s in statements if s is not None]
    if not parsed:
        raise SQLSecurityError("SQL 解析结果为空", rule="empty_sql", sql=sql)
    return parsed


_WRITE_TYPES: Final[tuple[type[exp.Expression], ...]] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,  # 解析器认不出的语句（如 GRANT/VACUUM/COPY）都落在这里 → 一律拒绝
    exp.Into,  # `SELECT ... INTO t`：sqlglot 在 root.sql() 里把它渲染成 CREATE TABLE AS
)


def _has_write_node(root: exp.Expression) -> bool:
    """树里是否藏着任何写节点（含 CTE 体内的 DELETE/UPDATE）。"""
    return any(root.find(t) is not None for t in _WRITE_TYPES)


def _function_name(fn: exp.Func) -> str:
    """取出函数名（小写）。

    ⚠️ **不能用 `fn.sql_name()`**：sqlglot 对「它不认识的函数」返回的是类名 `ANONYMOUS`，
    而不是真实函数名 —— 实测 `pg_read_file('/etc/passwd')` 解析成 `exp.Anonymous`，
    `sql_name()` 给出 `'ANONYMOUS'`。照着它写黑名单会**全部失效**（本条是实测踩出来的：
    危险函数集里每一条都被判成 `missing_limit` 而不是 `forbidden_function`）。
    真正的函数名在 `exp.Anonymous.this` 上（也可能带 `pg_catalog.` 前缀，要去掉再比对）。
    """
    if isinstance(fn, exp.Anonymous):
        name = str(fn.this or "")
    else:
        # 已知函数：类名即函数名（`exp.Sum` → `sum`、`exp.Count` → `count`）
        name = type(fn).__name__
    return name.rsplit(".", 1)[-1].lower()


def _check_functions(root: exp.Expression, sql: str) -> None:
    """危险函数黑名单：能读文件、能外联、能放大资源的函数一律拒绝。"""
    for fn in root.find_all(exp.Func):
        name = _function_name(fn)
        if name in policy.FORBIDDEN_FUNCTIONS:
            raise SQLSecurityError(
                f"拒绝调用危险函数 {name}()（文件/外联/资源放大类）",
                rule="forbidden_function",
                sql=sql,
            )


def _check_select_projections(root: exp.Expression, sql: str) -> None:
    """逐层检查 SELECT 投影里的裸列引用（无 FROM 时列校验没有归属表，会漏掉）。

    为什么需要这一层：下面这些查询**没有 FROM**，`_QueryScope.tables` 是空的，
    列校验会直接放行 ——
    · `SELECT pg_sleep(60)` / `SELECT pg_read_file('/etc/passwd')`：资源放大与任意文件读取；
    · `SELECT o.phone ...`（表别名写错、或引用子查询里被挡掉的列）：限定名归不到任何表，
      却因为这一层没查而漏过。
    所以这里只做一件事：**带限定名的列必须能归到本次查询的表/CTE**，否则拒绝。
    """
    known = {t.lower() for t in _collect_tables(root)} | set(_collect_ctes(root))
    for select in [root, *root.find_all(exp.Select)]:
        if not isinstance(select, exp.Select):
            continue
        for column in select.find_all(exp.Column):
            qualifier = (column.table or "").lower()
            if not qualifier or qualifier in known:
                continue
            raise SQLSecurityError(
                f"拒绝无法归属的表限定名 {qualifier}.{column.name}："
                "它不是本次查询里的表或 CTE（写错别名、或引用了被挡掉的列都属此列）",
                rule="column_not_allowed",
                sql=sql,
            )


# ── 内部：白名单 ─────────────────────────────────────────────────────────────


@dataclass
class _QueryScope:
    """一次校验里「有哪些表、哪些别名、哪些列是高敏列」的汇总。"""

    ctes: dict[str, str | None]  # CTE 名 → 它背后的物理表名（推不出时为 None）
    tables: dict[str, str]  # 别名（小写）→ 物理表名
    protected_columns: dict[str, frozenset[str]]  # 表 → 该表上**被禁止**引用的高敏列

    @classmethod
    def build(
        cls, root: exp.Expression, protected_columns: dict[str, frozenset[str]]
    ) -> "_QueryScope":
        return cls(
            ctes=_collect_ctes(root),
            tables=_collect_tables(root),
            protected_columns=protected_columns,
        )

    def physical_tables(self) -> tuple[str, ...]:
        """本次查询真正落到的物理表（去重排序，供审计/日志）。"""
        return tuple(sorted(set(self.tables.values())))

    def allows_column(self, column: exp.Column) -> bool:
        """该列是否被允许引用（含表名归属判定）。

        三种情况：
        - 带限定名（`users.phone` / CTE 名）：先定位到物理表，再查该表的高敏列；
        - 不带限定名（`phone`）：只要**任何**在作用域内的表把它标成高敏列就拒绝
          —— 少写表名前缀不能成为绕过手段；
        - 限定名不是本次查询的表/CTE（例如 `secret.phone`）：归不了因 → 拒绝。
        """
        name = column.name.lower()
        qualifier = (column.table or "").lower()

        if not qualifier:
            targets = set(self.tables.values())
            if not targets:
                return True  # `SELECT 1` 这类没有表的查询：无列可查
            return not any(self._is_protected(table, name) for table in targets)

        if qualifier in self.ctes:
            backing = self.ctes[qualifier]
            # 推不出底表的 CTE（聚合/JOIN 而来）→ 逐个候选表判，任一表视为高敏就拒绝
            if backing is None:
                return not any(self._is_protected(t, name) for t in set(self.tables.values()))
            return not self._is_protected(backing, name)

        target = self.tables.get(qualifier)
        if target is None:
            return False
        return not self._is_protected(target, name)

    def _is_protected(self, table: str, column: str) -> bool:
        """该列在这张表上是否属于「受保护列」（PII / 审计字段）。"""
        return column in self.protected_columns.get(table, frozenset())


def _collect_ctes(root: exp.Expression) -> dict[str, str | None]:
    """CTE 名 → 背后的物理表：`WITH recent AS (SELECT * FROM orders ...)` → `recent -> orders`。

    推不出底表（聚合、多表 JOIN、再套子查询）时记 `None`，列校验随之放宽到「全局允许列名」。
    """
    mapping: dict[str, str | None] = {}
    for cte in root.find_all(exp.CTE):
        alias = (cte.alias or "").lower()
        if not alias:
            continue
        # 先排除「嵌套 CTE 的名字」与「CTE 定义体自己那个同名节点」，剩下的才是真实底表
        inner_tables = {
            t.name.lower()
            for t in cte.find_all(exp.Table)
            if t.name.lower() != alias and t.name.lower() not in mapping
        }
        mapping[alias] = next(iter(inner_tables)) if len(inner_tables) == 1 else None
    return mapping


def _is_cte_reference(table: exp.Table, ctes: dict[str, str | None]) -> bool:
    """该 `exp.Table` 节点是不是「引用 CTE」而不是「访问物理表」。

    两步判断，缺一不可：
    1. 名字是某个 CTE 名；
    2. 它**不在任何 CTE 定义体内** —— 在定义体里的同名节点是被访问的物理表
       （实测 `WITH orders AS (SELECT ... FROM orders)` 时内外都叫 orders，只看名字必然判错）。
    """
    if table.name.lower() not in ctes:
        return False
    return table.find_ancestor(exp.CTE) is None


def _collect_tables(root: exp.Expression) -> dict[str, str]:
    """本次查询出现的物理表：别名/表名（小写）→ 表名（小写）。

    跳过两类「不是本次查询要访问的表」：引用 CTE 的名字节点、派生表别名（不是 `exp.Table`）。
    """
    ctes = _collect_ctes(root)
    tables: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        if _is_cte_reference(table, ctes):
            continue
        name = table.name.lower()
        if not name:
            continue
        tables[(table.alias or name).lower()] = name
        tables.setdefault(name, name)
    return tables


def _check_tables(ctx: _QueryScope, tables_allow: frozenset[str], sql: str) -> None:
    unknown = sorted(set(ctx.tables.values()) - tables_allow)
    if unknown:
        raise SQLSecurityError(
            f"拒绝访问白名单外的表：{', '.join(unknown)}（允许的表：{', '.join(sorted(tables_allow))}）",
            rule="table_not_allowed",
            sql=sql,
        )


def _check_star(ctx: _QueryScope, root: exp.Expression, sql: str) -> None:
    """`SELECT *` 打在**含高敏列的表**上要拒绝。

    为什么必须单独拦一条：`SELECT *` 展开前没有 `exp.Column` 节点，列校验无从下手 ——
    实测 `SELECT * FROM users WHERE user_id = 1` 能一路过关，把手机号/地址原样带出去。
    「少写几个列名」不能成为绕过列白名单的手段，所以对这些表要求显式列名。
    （`SELECT * FROM orders` 这类不含高敏列的表不受影响，日常查询不会被误伤。）
    """
    for star in root.find_all(exp.Star):
        if isinstance(star.find_ancestor(exp.Count), exp.Count):
            continue  # `COUNT(*)` 不返回任何列的内容，不算泄露
        select = star.find_ancestor(exp.Select)
        if select is None:
            continue
        # `t.*` 形态：sqlglot 里带限定名的星号挂在 Column 下面，表名在 Column.table 上
        column = star.find_ancestor(exp.Column)
        qualifier = (column.table or "").lower() if column is not None else ""
        targets: set[str | None] = (
            {ctx.tables.get(qualifier)} if qualifier else set(ctx.tables.values())
        )
        hit = sorted(t for t in targets if t and ctx.protected_columns.get(t))
        if hit:
            raise SQLSecurityError(
                f"拒绝在含高敏列的表上使用 SELECT *（{', '.join(hit)}）"
                "：请显式列出所需列，高敏列只能经 app/db/queries.py 出口 + 脱敏返回",
                rule="star_on_protected_table",
                sql=sql,
            )


def _check_columns(ctx: _QueryScope, root: exp.Expression, sql: str) -> None:
    bad = sorted(
        {
            f"{c.table}.{c.name}" if c.table else c.name
            for c in root.find_all(exp.Column)
            if not ctx.allows_column(c)
        }
    )
    if bad:
        raise SQLSecurityError(
            f"拒绝访问高敏列：{', '.join(bad)}（只能经 app/db/queries.py 出口 + 脱敏返回）",
            rule="column_not_allowed",
            sql=sql,
        )


# ── 内部：WHERE / 恒真条件 ───────────────────────────────────────────────────


def _has_table_source(select: exp.Select) -> bool:
    """该 SELECT 是否真的「查了表」（有 FROM 或 JOIN）。

    单独成函数的理由：sqlglot 里 FROM 的键名是 `from_`（带下划线），写成 `"from"` 会**永远返回 None**，
    症状不是报错而是「所有无 WHERE 的查询都被当成 `SELECT 1` 放行」——这种静默失效必须集中一处兜住。
    """
    return select.args.get("from_") is not None or bool(select.args.get("joins"))


def _check_row_filters(root: exp.Expression, sql: str) -> None:
    """逐层检查每个「带 FROM 的 SELECT」是否带**有效** WHERE。"""
    for select in [root, *root.find_all(exp.Select)]:
        if not isinstance(select, exp.Select):
            continue
        if not _has_table_source(select):
            continue  # `SELECT 1` / `SELECT now()`：没有表，无过滤可言
        where = select.args.get("where")
        if where is None:
            raise SQLSecurityError(
                "拒绝无 WHERE 的整表查询（防越权拖库 + 防全表扫描拖垮库）",
                rule="missing_where",
                sql=sql,
            )
        if not _is_row_filter(where.this):
            raise SQLSecurityError(
                "拒绝无效的 WHERE：整条条件对每一行都为真（如 1=1、TRUE、user_id=5 OR TRUE），等于没有过滤",
                rule="trivial_where",
                sql=sql,
            )


def _is_row_filter(condition: exp.Expression | None) -> bool:
    """这条 WHERE 条件是否「真的能筛掉行」。

    递归口径（每个算子都按自己的语义定规则，不追求做常量折叠 —— 那是优化器的活）：

    | 形态 | 判定 | 例子 |
    |---|---|---|
    | 布尔/数字字面量、NULL、参数 | 恒真（或与列无关）→ 不是过滤 | `TRUE`、`1`、`NULL` |
    | 列引用 | 可能是过滤 → 是 | `is_active` |
    | 比较运算 | 两侧都恒真的才是恒真 | `2>1` 恒真；`user_id=5` 是过滤 |
    | `AND` | **两侧都必须**是过滤 | `1=1 AND x=1` 挡掉 |
    | `OR` | **必须两侧都是**过滤：`A OR TRUE` 恒真；`A OR B` 里 A、B 各自都能筛行才是有效过滤 | `1=1 OR x=5` 挡掉 |
    | 其它（`IN` / `LIKE` / `BETWEEN` / `EXISTS` …） | 只要引用了列，就认为能筛行 | `user_id IN (1,2)` |
    """
    if condition is None:
        return False
    if isinstance(condition, exp.Column):
        return True
    if isinstance(condition, exp.Paren):
        return _is_row_filter(condition.this)
    if isinstance(condition, exp.Not):
        return _is_row_filter(condition.this)
    if isinstance(condition, exp.Boolean):
        return not bool(condition.this)  # `WHERE FALSE` 确实能筛掉所有行
    if isinstance(condition, (exp.Literal, exp.Null, exp.Placeholder, exp.Parameter)):
        return False
    if isinstance(condition, (exp.And, exp.Or)):
        return _is_row_filter(condition.left) and _is_row_filter(condition.right)
    if isinstance(condition, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return _is_row_filter(condition.left) or _is_row_filter(condition.right)
    return any(True for _ in condition.find_all(exp.Column))


def _is_trivially_true(condition: exp.Expression | None) -> bool:
    """（已废弃）恒真判定，现由 `_is_row_filter` 取代。

    保留这个说明而不是直接删掉，是因为两者的差别值得留一笔：早期版本只做「树里有没有
    两侧都不含列的比较」一个判断，对 `user_id = 5 OR TRUE` 这种「一半是过滤、一半恒真」
    的写法会误判 —— 递归的 `_is_row_filter` 才把 AND / OR 的语义各自说清。
    """
    return not _is_row_filter(condition)


# ── 内部：LIMIT（C1）────────────────────────────────────────────────────────


def _check_limit(root: exp.Expression, sql: str, max_rows: int) -> tuple[int | None, bool, str]:
    """检查最外层 LIMIT：字面量超上限直接拒；参数占位符则收紧到 max_rows。"""
    outmost = _outmost_select(root)
    limit_node = outmost.args.get("limit")
    if limit_node is None:
        # 没有 LIMIT：只放行两种「结果集天然受限」的形态，其余一律要求带 LIMIT。
        # 注意：走到这里时 WHERE 已经过了恒真检查（见 validate_sql 的 ⑤→⑥ 顺序），
        # 所以下面的「等值查询」豁免不会被 `WHERE 1=1` 钻空子。
        if _is_single_row_lookup(outmost):
            return None, False, sql
        raise SQLSecurityError(
            f"拒绝没有 LIMIT 的查询：单条查询最多返回 {max_rows} 行（C1）",
            rule="missing_limit",
            sql=sql,
        )

    expr = limit_node.expression
    literal = _literal_int(expr)
    if literal is None:
        # `LIMIT :n` / `LIMIT ?`：值在运行期才定 → 强制收紧到 max_rows（由调用方执行的 SQL 带上）
        return max_rows, True, _clamp_sql(sql, max_rows)

    if literal > max_rows:
        raise SQLSecurityError(
            f"拒绝超出行数上限的查询：LIMIT {literal} > {max_rows}（C1）",
            rule="limit_too_large",
            sql=sql,
        )
    return literal, False, sql


def _outmost_select(root: exp.Expression) -> exp.Select:
    """最外层 SELECT：UNION 取最后一个分支（它的 LIMIT 才是结果集上限）。"""
    if isinstance(root, exp.Select):
        return root
    selects = list(root.find_all(exp.Select))
    return selects[-1] if selects else root  # type: ignore[return-value]


def _literal_int(expr: exp.Expression | None) -> int | None:
    if isinstance(expr, exp.Literal) and expr.is_int:
        try:
            return int(expr.this)
        except (TypeError, ValueError):
            return None
    return None


def _is_single_row_lookup(select: exp.Select) -> bool:
    """是否为「结果集天然 ≤1 行」的查询，从而可以不带 LIMIT。

    三条豁免，都是有意的（否则会逼着业务代码到处写 `LIMIT 1` 噪音）：

    1. **不查表**：`SELECT 1` / `SELECT now()` 没有 FROM，结果就是一行。
    2. **主键等值查询**：`WHERE order_id = :id` 这类等值比较只可能命中 0/1 行。
       `app/db/queries.py::get_order_for_customer` 也是同口径（`expect_limit=False`）。
    3. **纯标量聚合**：`SELECT count(*) / sum / min / max / avg ...` 返回一行。
       带 `GROUP BY` 时性质被破坏，豁免不成立。
       ⚠️ 而且只在这些聚合**直接作用于本层 FROM 的表**时才豁免：`count(*)` 是不可分解的，
       但 `array_agg` / `string_agg` / `json_agg` 会把**多行内容**塞进一行 —— 结果虽只一行，
       内容却是多行数据，正是行数上限要拦的东西，所以它们不在豁免之列。
    """
    if select.args.get("group") is not None:
        return False

    # ⓪ 「把多行塞进一行」的聚合：结果只有一行，但**内容是**多行数据 —— 绝不是单行查询，
    #    必须带 LIMIT（C1 要拦的正是「一次拖走很多行」）。放在最前面，
    #    否则会被下面「等值查询」的豁免放过去（实测 `string_agg(status, ',') ... WHERE user_id=1`
    #    就带着一个等值条件，看起来完全像主键查询）。
    projections = [x.this if isinstance(x, exp.Alias) else x for x in select.expressions]
    for projection in projections:
        for agg in projection.find_all(exp.AggFunc):
            if not isinstance(agg, _SCALAR_AGGREGATES):
                return False

    # ① 不查表
    if not _has_table_source(select):
        return True

    # ② 主键等值查询
    where = select.args.get("where")
    if where is not None:
        for cmp_node in where.find_all(exp.EQ):
            left, right = cmp_node.left, cmp_node.right
            if isinstance(left, exp.Column) and _is_constant(right):
                return True
            if isinstance(right, exp.Column) and _is_constant(left):
                return True

    # ③ 纯标量聚合（且 FROM 的都是**物理表**，不是子查询别名/CTE）
    if projections and all(isinstance(e, _SCALAR_AGGREGATES) for e in projections):
        return _froms_are_physical_tables(select)

    return False


def _froms_are_physical_tables(select: exp.Select) -> bool:
    """本层 FROM / JOIN 的来源是否都是物理表（而非派生表别名、CTE 引用）。

    用途：决定「聚合结果只有一行」这条豁免能不能成立。
    `SELECT count(*) FROM orders` 很明确只有一行；但
    `WITH recent AS (...) SELECT string_agg(x, ',') FROM recent` 的 `string_agg`
    会把**多行**内容拼成一行 —— 结构上和「聚合子查询」长得一样，
    所以只要来源不是物理表，豁免就不给（宁可要求显式 LIMIT）。
    """
    from_node = select.args.get("from_")
    if from_node is None:
        return False
    if from_node.find(exp.Subquery) is not None:
        return False
    if not list(from_node.find_all(exp.Table)):
        return False
    ctes = _collect_ctes(select)
    return all(not _is_cte_reference(t, ctes) for t in from_node.find_all(exp.Table))


def _is_constant(expr: exp.Expression) -> bool:
    return isinstance(expr, (exp.Literal, exp.Placeholder, exp.Parameter, exp.Cast)) and not any(
        True for _ in expr.find_all(exp.Column)
    )


def _clamp_sql(sql: str, max_rows: int) -> str:
    """把 SQL 文本里最外层的 LIMIT 常量就地收紧到 max_rows（保留参数写法不变）。

    只替换**裸整数**形态；`LIMIT $1` / `LIMIT :n` 这类占位符原样保留 ——
    它们的值由执行侧绑定，守卫强行改写反而会破坏参数个数。
    """
    pattern = _LIMIT_INT_RE
    match = pattern.search(sql.strip())
    if not match:
        return sql
    value = int(match.group(1))
    if value <= max_rows:
        return sql
    return pattern.sub(f"LIMIT {max_rows}", sql.strip())


__all__ = ["DIALECT", "SQLSecurityError", "ValidationResult", "validate_sql"]
