# sqlglot（SQL 守卫：用 AST 而不是正则）

## 本项目用它解决什么

拦住"LLM 自由生成的 SQL"：只读、表白名单、列白名单、禁 `SELECT *`、必须有 WHERE、必须有 LIMIT。**用 AST 解析而不是正则**，因为正则永远漏。

## 实际用法（代码片段 + 文件:行号）

### 1）入口：`validate_sql` 六道校验（顺序即语义）

```python
# app/security/sql_guard.py:104-162（节选）
if not sql or not sql.strip():
    raise SQLSecurityError("SQL 为空", rule="empty_sql")

# ① 多语句：parse_one 会静默丢掉后面
statements = _parse_statements(sql)
if len(statements) > 1:
    raise SQLSecurityError(f"拒绝多语句 SQL：解析出 {len(statements)} 条", rule="multiple_statements")

# ② 只读：必须是 SELECT/UNION，且不得含写节点
root = statements[0]
if not isinstance(root, exp.Select) and not isinstance(root, exp.Union):
    raise SQLSecurityError("拒绝非查询语句", rule="not_read_only")
if _has_write_node(root):
    raise SQLSecurityError("拒绝读写混用语句", rule="write_in_read")

# ③ 白名单：表 + 列 + 禁 SELECT *
ctx = _QueryScope.build(root, protected)
_check_tables(ctx, tables_allow, sql)
_check_star(ctx, root, sql)
_check_columns(ctx, root, sql)

# ④ 危险函数
_check_functions(root, sql)
_check_select_projections(root, sql)

# ⑤ WHERE / 全表扫描 / 恒真条件（必须在 ⑥ 之前）
if require_where:
    _check_row_filters(root, sql)

# ⑥ LIMIT
limit, limit_clamped, out_sql = _check_limit(root, sql, max_rows)
```

### 2）多条语句：必须用 `parse()` 而不是 `parse_one()`

```python
# app/security/sql_guard.py:168-179
def _parse_statements(sql: str) -> list[exp.Expression]:
    """解析成语句列表；语法错误也归为「不安全」（校验不了的一律不放行）。"""
```

> `sqlglot.parse_one("SELECT 1; DROP TABLE users")` **只返回第一条**（实测 30.19.0），所以先 `parse()` 数条数。

### 3）行级过滤的"形状"校验

```python
# app/agents/agent_llm.py:267-334
def customer_row_filter_error(sql: str, *, user_id: int, agent: str = "data") -> str | None:
    """对客路径：SQL 必须把 user_id 绑到 :uid，且不能用字面量冒充绑定值。"""
```

```python
# app/agents/agent_llm.py:385-442 的关键判据
def _literal_row_filter_value(node) -> str | None: ...   # WHERE user_id = 5 这种字面量 → 拒
def _is_bound_uid_equality(node) -> bool: ...            # WHERE user_id = :uid 才算数
```

### 4）执行层的行数兜底

```python
# app/tools/sql_executor.py:93-141
def limit_placeholder_name(sql: str) -> str | None: ...   # 认出 LIMIT :n 这种参数化写法
def clamp_bound_limit(...): ...                           # 把绑定值夹到上限
```

## 最小原理

1. **sqlglot 把 SQL 解析成表达式树**：`exp.Select` / `exp.Table` / `exp.Column` / `exp.Func` / `exp.Where` / `exp.Limit`。所有校验都是在树上"找节点 + 查属性"。
2. **"只读"是结构判断**：根节点类型 + 树里是否存在 `Insert/Update/Delete/Drop/Create` 等写节点（`_has_write_node`）。比关键字匹配可靠，因为 `WITH x AS (DELETE ...)` 这种写法关键字不在开头。
3. **"恒真条件"也是结构判断**：`WHERE 1=1` 与 `WHERE 1=1 OR user_id = :uid` 在树上表现为 `exp.Or`/`exp.EQ` 常量，`_is_trivially_true()` 一眼能认出。
4. **顺序有语义**：LIMIT 那一步有一处豁免（"主键等值查询可免 LIMIT"，`_is_single_row_lookup`），而 `WHERE 1=1 OR user_id = 5` 恰好长得像等值查询 —— 所以**必须先跑恒真检查再跑 LIMIT**（`app/security/sql_guard.py:142-148` 的长注释）。

## 常见坑

| 坑 | 症状 | 依据 |
|---|---|---|
| 用 `parse_one` 做多语句检查 | `SELECT 1; DROP TABLE users` 被放行 | `app/security/sql_guard.py:110-113` |
| 用正则找 `DROP/DELETE` 关键字 | 注释、字符串常量、大小写、换行都能绕过 | 同文件 §4.1 的选型理由 |
| 先查 LIMIT 再查恒真 | `WHERE 1=1` 系列全部 ALLOW（实测踩过） | `app/security/sql_guard.py:142-148` |
| 只校验 SQL 形状、不校验绑定值 | 形状对（`:uid` 在）但绑定值来自模型 → 查到别人的数据 | `app/agents/data.py:420-433` |
| 以为 `validate_sql(read_only=False)` 能关安全检查 | 危险语句黑名单**始终生效** | `app/security/sql_guard.py:97-98` |

## 深入关键词

`sqlglot.parse` vs `parse_one` · `exp.Select` / `exp.Union` / `exp.Table` / `exp.Column` · `find_all(exp.Column)` · CTE 识别 · `dialect="postgres"` · 恒真条件（tautology）检测 · 参数化绑定 vs 字面量 · 表白名单（default-deny）
