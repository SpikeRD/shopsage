"""退款接口的异常映射（变更 003：`app/api/orders.py` 与 `app/api/refunds.py` 共用）。

## 为什么单独一个模块（与 `account_errors.py` 同一个理由）

`app/tools/refund.py` 抛的是**领域异常**，路由层要把它们翻成 HTTP 错误信封。
两个路由模块（订单页 + 退款台）都要做这件事，抄两份的后果是
「同一个错误在一处是 409、在另一处是 400」这种**同一类错误两种状态码**的漂移 ——
而它只会在某一条路径上被用户撞到。

## 状态码口径（每一档都有理由，不要随手改）

| 异常 | 状态码 | 为什么 |
|---|---|---|
| `RefundNotDecidable` | **409** | 「**别人已经处理过了**」。请求本身完全合法，冲突的是**资源当前状态**。前端据此刷新列表，而不是改参数 |
| `RefundNotAllowed` | **400** | 「业务前提此刻不成立」（订单还没发货、金额超过剩余额度）。**是调用方可以改的**（改单/改金额/等货到），所以是 4xx 而不是 5xx |
| `ToolContractError` | 交给全局处理器（400） | 参数形状问题的映射在 `app/api/errors.py` 里已有一处真相，这里不重复登记 |
| `PermissionDenied` | 交给全局处理器（**404**） | 越权的对外口径是全仓统一的（不确认资源是否存在），这里绝不覆盖 |

## ⚠️ 两条容易被"顺手改错"的地方

1. **`RefundNotAllowed` 必须显式登记**。它是 `PermanentToolError` 的子类，
   而全局处理器给 `PermanentToolError` 的映射是 **500**（「库/工具永久失败」）。
   本模块的映射更具体，Starlette 按 MRO 查找处理器时会命中本模块 ——
   但**我们并不依赖那个查找顺序**：路由用 `with refund_errors():` 显式包住调用，
   所以「客户端参数不对」不会被报成「服务端故障」。
2. **409 与 400 不能合并成一个码**。前者的正确动作是「刷新一下，别人批过了」，
   后者的正确动作是「改你的请求」。合成一个码，前端就只能给一句笼统的提示，
   而这两种情况下用户该做的事完全相反。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from app.api.errors import ApiError
from app.tools.refund import RefundNotAllowed, RefundNotDecidable

#: 错误码（前端按它做分支，不要按文案 —— 中文文案随时可能改）
CODE_NOT_DECIDABLE: str = "refund_not_decidable"
CODE_NOT_ALLOWED: str = "refund_not_allowed"


@contextmanager
def refund_errors() -> Iterator[None]:
    """把退款领域的异常翻成 `ApiError`。用法：`with refund_errors(): ...`"""
    try:
        yield
    except RefundNotDecidable as exc:
        raise ApiError(
            409,
            CODE_NOT_DECIDABLE,
            str(exc),
            # `current_status` 让前端能直接把列表里那一行刷新成正确的状态
            # （而不是只弹一句「操作失败，请重试」——那样用户会再点一次，再撞一次 409）。
            detail={"refund_id": exc.refund_id, "current_status": exc.status},
        ) from exc
    except RefundNotAllowed as exc:
        raise ApiError(400, CODE_NOT_ALLOWED, str(exc)) from exc


__all__ = [
    "CODE_NOT_ALLOWED",
    "CODE_NOT_DECIDABLE",
    "refund_errors",
]
