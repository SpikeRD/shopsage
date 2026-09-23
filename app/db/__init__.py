"""db 数据访问层：模型、连接、查询封装。

对外接口（`docs/modules/3.db.md` 第 2 节）：
    from app.db.models import User, Product, Order, OrderItem, Refund, StockMovement, Ticket, Employee
    from app.db.session import engine, SessionLocal, get_db, get_read_engine, get_write_engine
    from app.db.queries import get_orders_for_customer, ...

本包不 import 任何 FastAPI / Agent 代码，保证「数据层可独立测试」。
"""

from app.db.base import Base

__all__ = ["Base"]
