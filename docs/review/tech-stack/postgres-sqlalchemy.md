# PostgreSQL + SQLAlchemy 2 + Alembic（数据层）

## 本项目用它解决什么

10 张表、连接池、**读写两个引擎**（写角色 / 只读角色）、以及可升可降的迁移。它是"行级过滤 + 只读兜底"这条安全设计的地基。

## 实际用法（代码片段 + 文件:行号）

### 1）连接池参数

```python
# app/db/session.py:27-30
POOL_SIZE = 5
MAX_OVERFLOW = 10
POOL_TIMEOUT = 30      # 借不到连接最多等 30s，然后抛 TimeoutError（快速失败）
POOL_RECYCLE = 1800    # 连接最长寿命，防中间设备静默掐断空闲连接
```

### 2）读写双引擎

```python
# app/db/session.py:83-103
def _read_url() -> str: ...
engine: Engine = make_engine(_settings.database_url)
def get_write_engine() -> Engine: ...
def get_read_engine() -> Engine: ...
```

### 3）行级过滤自检（代码级强制）

```python
# app/db/queries.py:29 / :52
MAX_LIMIT = 200
def assert_row_filtered(stmt: Select, *, expect_limit: bool = True) -> str:
    """没有 WHERE user_id 或没有 LIMIT 的查询在这里就被拦下。"""
```

### 4）模型清单：8 业务 + 2 基础设施

```python
# app/db/models.py:433 / :452 / :454
BUSINESS_MODELS = (User, Product, Order, OrderItem, Refund, StockMovement, Ticket, Employee)
INFRA_MODELS = (QueueDeadLetter, Account)
ALL_MODELS = BUSINESS_MODELS + INFRA_MODELS
```

### 5）Alembic：连接串从 Settings 注入

```ini
# alembic.ini —— 不写 URL
# alembic/env.py 从 app.config 的 Settings 取 database_url
```

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic heads
```

### 6）迁移是单链（见 `consolidated/modules/03-db.md` 的表）

```
1d0329d8d747 → 29d823809e4f → a1f0c3d5b7e9 → 7164220823a3 → b3c7f1a92d40 → f3b8c41d9e27 → c7a4e2f9b1d8 (head)
```

## 最小原理

1. **连接池是"预借出的连接集合"**：`pool_size` 常驻、`max_overflow` 临时，超出后等 `pool_timeout` 秒再失败。**没有池**的话每次请求都 TCP + 认证，高并发下库会被打爆（这就是 `docs/problem-solving.md` C1）。
2. **`pool_recycle` 解决"僵尸连接"**：NAT/防火墙会静默丢弃长时间空闲的 TCP 连接，应用以为还活着。
3. **SQLAlchemy 2 的 `Select` 是可组合对象**：`assert_row_filtered` 能检查它，是因为语句还没被编译成字符串。
4. **Alembic 的本质是"版本表 + 有序脚本"**：`alembic_version` 表里只有一行（当前 revision），`down_revision` 把脚本串成链。链分叉就会出现多个 head。
5. **SQL 方言（dialect）决定驱动**：`postgresql+psycopg://` 是 psycopg3，`postgresql://` 默认走 psycopg2。

## 常见坑

| 坑 | 症状 | 依据 |
|---|---|---|
| URL 方言写错 | 装的是 psycopg3 却按 psycopg2 找驱动 | `app/db/session.py:33` |
| 不配 `DATABASE_READ_URL` | "只读"只在命名上成立，读路径落到写角色 | `app/config/settings.py:215` 注释 |
| 改模型不加迁移 | `test_no_pending_migration_against_models` 红 | `tests/test_db_migrations.py` |
| 迁移写进 api 的启动命令 | 多副本互相抢 advisory lock，或 `relation already exists` 退出 | `docker-compose.yml:46-49` 注释 |
| 把 `accounts` 加进 SQL 白名单 | Text-to-SQL 能读到 `password_hash` | `app/security/policy.py:117-123` |
| `WHERE 1=1` 认为"有 WHERE 就安全" | 全表扫描照样执行 | `app/security/sql_guard.py:488` 的 `_is_trivially_true` |

## 深入关键词

`QueuePool` / `pool_size` / `max_overflow` / `pool_timeout` / `pool_recycle` · `pool_pre_ping` · SQLAlchemy 2 `Select` 与 `sessionmaker(expire_on_commit=False)` · `psycopg3` vs `psycopg2` · Alembic `revision` / `down_revision` / `head` · `alembic_version` 表 · 命名约定（`NAMING_CONVENTION`，`app/db/base.py:15`）
