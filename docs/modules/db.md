# db — 数据访问层（SQLAlchemy 模型 / 连接池 / 读写分离 / 权限）

## 1. 职责

统一封装对 PostgreSQL 的访问：ORM 模型、连接池、以及「对客 / 对内」两条权限路径的数据库视角。
是 Text-to-SQL 和所有数据查询的**唯一入口**，业务代码不直接裸写连接。

## 2. 对外接口

```python
# 模型（对应 architecture.md 第 5 节 Schema）
from app.db.models import User, Product, Order, OrderItem, Refund, StockMovement, Ticket, Employee

# 引擎 / 会话
from app.db.session import engine, SessionLocal, get_db          # get_db 给 FastAPI 依赖注入
from app.db.session import get_read_engine, get_write_engine      # 读写分离
```

## 3. 依赖

- `SQLAlchemy 2.x`（ORM + Core）+ `psycopg`（异步驱动，可选 `asyncpg`）
- `pgvector`（向量检索，见 `rag.md`，同库不同表/扩展）
- 读取 `config` 的 `database_url`

## 4. 关键设计决策

1. **一张业务库装下关系数据 + 向量数据**：pgvector 是 PostgreSQL 扩展，知识库向量和业务表同库，省一套独立向量库（小企业够用，架构第 2 节）。
2. **读写分离是「能力」，不是「必须一开始就上」**：先单库，用 `get_read_engine` / `get_write_engine` 两个入口预留切换点；负载上来再挂只读副本改连接串即可（架构第 8 节）。
3. **权限落到「行级过滤 + 只读视图」而非裸表**：对客查询永远只走带 `WHERE user_id = :当前用户` 的过滤条件，从数据库层面兜底（与 `security.md` 配合，D1）。
4. **连接池显式设上限**：`pool_size` + `max_overflow` + `pool_pre_ping=True`，防止连接被打爆（C1）。

## 5. 核心技术点（要懂为什么）

- **`pool_pre_ping=True`**：每次借连接前先 ping，踢掉因网络/超时失效的连接，避免「从池里拿到死连接」。
- **连接池参数怎么估**：`pool_size ≈ 每 worker 并发 SQL 数`，`max_overflow` 是峰值临时额度；设太大拖垮库，太小排队。
- **行级过滤为什么必须在 SQL 层而非应用层**：应用层过滤 = 先查出全部再截断，既慢又可能泄露；`WHERE user_id=...` 由数据库保证只返回本用户数据，是防越权的最后一道闸。
- **`sessionmaker` 的 `expire_on_commit=False`**：提交后对象仍可用，避免异步上下文里反复触发刷新。

## 6. 开发步骤

1. `docker-compose.dev.yml` 起 `postgres` + 启用 `pgvector` 扩展（见 `infra.md`）。
2. 写 `app/db/base.py`（DeclarativeBase）+ 8 张表的模型（字段、索引、外键）。
3. 写 `session.py`：`create_engine`（带池参数）、`SessionLocal`、`get_db` / `get_read_engine` / `get_write_engine`。
4. 用 Alembic 建迁移：`alembic init` → 生成首版 → `alembic upgrade head`。
5. 写 `scripts/seed.py`：插入模拟用户/订单/商品/库存/工单/员工数据（电商场景演示必需）。
6. 写「只读视图 + 行级过滤」的查询封装：`get_orders_for_customer(user_id)` 等。

## 7. 测试用例

```python
def test_pool_limits_connections():
    # 高并发下数据库连接数不超过 pool_size + max_overflow

def test_customer_query_is_row_filtered(user, other_user):
    # 传入 user_id=A，返回结果不含 B 的订单
    orders = get_orders_for_customer(user.user_id)
    assert all(o.user_id == user.user_id for o in orders)

def test_seed_data_counts():
    # users/products/orders/refunds/... 各表行数 >= 最小演示量

def test_read_write_separation_switch():
    # 只读引擎与写引擎为不同连接串时，读路径不落写库
```

## 8. 验收标准

- [ ] 8 张表建模完成，Alembic 迁移可重复执行（`upgrade` / `downgrade`）。
- [ ] 高并发下数据库连接数稳定不超上限（承接 **C1**）。
- [ ] 对客查询路径所有 SQL 都带行级过滤（承接 **D1**）。
- [ ] 证据链接：`PR#__` / `压测报告#__`
