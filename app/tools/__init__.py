"""工具层（第 6 步）：Agent 与外部客户端**唯一允许的副作用出口**。

`6.tools.md` 第 2 节的四个能力 + 本步落地时明确补上的退款写路径，都在这里对外：

| 工具 | 文件 | 性质 | 关键约束 |
|---|---|---|---|
| `execute_readonly_query` | `sql_executor.py` | 读（唯一读库出口） | 二次 `validate_sql` + 只读引擎 + 行数截断 |
| `retrieve_docs` | `retriever.py` | 读（知识库） | 只返回**带溯源**的结果（无源一律丢弃） |
| `build_chart_spec` | `chart.py` | 纯计算 | 返回结构化 spec，不返回图片 |
| `create_ticket` / `get_ticket_status` | `ticket.py` | 写 / 读 | 幂等（DB 唯一约束）+ RBAC + 审计 |
| `create_refund_request` / `get_refund_status` | `refund.py` | 写 / 读 | 幂等 + 剩余额度校验 + 行锁 + SoD |
| `request_refund` / `decide_refund` / `list_refunds_for_review` | `refund.py` | 写 / 读 | 变更 003：客户申请只能建 `requested`；审批 `requested → approved/rejected`（行锁 + 409） |
| `list_orders_for_customer` / `get_order_view` | `orders.py` | 读（对客订单页） | 行级过滤 + 脱敏 + 派生「还剩多少可退」 |

三条使用铁律（与 `6.tools.md` 第 4、5 节对应）：

1. **别绕过工具去碰数据库**：读走 `app/db/queries.py` 或本模块的 `execute_readonly_query`，
   写只能走本模块的预定义函数。LLM 永远拿不到「直接写库」的能力（D1 的读写路径分离）。
2. **每次调用都要传 `caller`**：审计字段「谁在调用」，缺了会被 `ToolContractError` 拒绝 ——
   这是刻意的（审计漏一条，事后就无法回答「谁干的」）。
3. **异常分三类处置**：`SQLSecurityError`（SQL 不合规，映射 400）、
   `PermissionDenied`（身份/权限不足，映射 403）、本模块的 `ToolError` 子类
   （可重试 vs 不可重试 → 第 12 步队列的依据）。

⚠️ 本文件**只导出领域工具**，不导出 `app/tools/write_path.py` 里的 `write_session` /
`insert_once`：那是写路径的内部零件，「能拿到一个写会话」不该是工具层的公共 API。

自查出口（`docs/runbook/6.tools-runbook.md` §3.4）：

    uv run python -c "import inspect, app.tools as t; [print(n, inspect.signature(getattr(t, n))) for n in t.__all__]"
"""

from app.tools.chart import CHART_SCHEMA, CHART_TYPES, build_chart_spec
from app.tools.errors import (
    PermanentToolError,
    RetryableToolError,
    ToolContractError,
    ToolError,
    ToolNotAvailableError,
)
from app.tools.orders import get_order_view, list_orders_for_customer
from app.tools.refund import (
    RefundNotAllowed,
    RefundNotDecidable,
    RefundRef,
    RefundView,
    create_refund_request,
    decide_refund,
    get_refund_status,
    list_refunds_for_review,
    refund_rows_for_output,
    request_refund,
)
from app.tools.retriever import RetrievedDoc, retrieve_docs, retrieve_docs_async
from app.tools.sql_executor import execute_readonly_query
from app.tools.ticket import TicketRef, create_ticket, get_ticket_status

# 审计辅助也导出：调用方（第 9 步 API / 第 11 步观测）要靠 `audit_records()` 解析审计行
from app.tools.audit import audit_records

__all__ = [
    "CHART_SCHEMA",
    "CHART_TYPES",
    "PermanentToolError",
    "RefundNotAllowed",
    "RefundNotDecidable",
    "RefundRef",
    "RefundView",
    "RetryableToolError",
    "RetrievedDoc",
    "TicketRef",
    "ToolContractError",
    "ToolError",
    "ToolNotAvailableError",
    "audit_records",
    "build_chart_spec",
    "create_refund_request",
    "create_ticket",
    "decide_refund",
    "execute_readonly_query",
    "get_order_view",
    "get_refund_status",
    "get_ticket_status",
    "list_orders_for_customer",
    "list_refunds_for_review",
    "refund_rows_for_output",
    "request_refund",
    "retrieve_docs",
    "retrieve_docs_async",
]
