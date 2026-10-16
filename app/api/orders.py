"""客户订单查询接口（变更 003 / 需求 2）：`GET /api/orders`、`GET /api/orders/{order_id}`。

```
GET /api/orders              我的订单列表（含明细、退款记录、还剩多少可退）
GET /api/orders/{order_id}   我的某一笔订单
```

## 四条纪律

### ① 行级过滤的依据只能来自令牌

`user_id` 取自 `Actor.user_id`（由已验签令牌派生），**请求体与查询串里都没有它**。
这是 D1 的落点：客户端没有任何字段可以表达「我要看别人的订单」。

### ② 「不是我的单」与「这单不存在」必须**不可区分**

两者都返回同一个 404 + 同一句话（`not_found`）。理由见 `app/api/errors.py` 的
「为什么越权是 404 而不是 403」——用状态码的差异可以枚举出「哪些订单号是真的」。

### ③ 授权用 `read:own`，不是「有订单就放行」

本模块的每个口都挂 `@require_permission(PERM_READ_OWN, identity_from="actor")`：
`customer` / `customer_service` / 内部角色都持有它（它是「读自己的数据」这条最基础的能力）。
**员工查不到自己的 orders**，因为他的 `user_id` 是 `0`（他在 `users` 表里没有行）——
服务层会把这种情况显式拒掉（`ToolContractError`），而不是返回一个空列表
（空列表看起来像「你没有订单」，真因却是「没人告诉系统该查谁」）。

### ④ 金额口径在服务层，本模块只做形状转换

「还剩多少可退」= 订单金额 − 已占用（`rejected` 不计）。这条口径的唯一实现在
`app/tools/orders.py`（它 import `refund.CONSUMING_REFUND_STATUSES`）。
本模块**不重算**任何金额，也不做脱敏 —— 脱敏同样在服务层
（`app/tools/orders.py::_mask_rows`），因为 `app/api/` 被契约测试禁止调用 `mask_pii`。

### ⑤ 数据层名字一律不 import

`db: Any = Depends(get_db)` 的类型注解写 `Any`（理由与 `app/api/admin.py` 的模块 ⑤ 相同：
`tests/api/test_api_contracts.py::test_no_data_layer_imports` 钉住 API 层不 import `sqlalchemy`）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import Actor, require_actor
from app.api.errors import ApiError
from app.api.schemas import OrderListResponse, OrderView
from app.db.queries import DEFAULT_LIMIT, MAX_LIMIT
from app.db.session import get_db
from app.security.rbac import PERM_READ_OWN, require_permission
from app.tools.errors import ToolContractError
from app.tools.orders import get_order_view, list_orders_for_customer

router = APIRouter(prefix="/orders", tags=["orders"])

#: 对外统一话术：**不区分**「不存在」与「不是你的」。
#: 「不是我的单」与「这单不存在」返回同一个 404 + 同一句话，攻击者拿不到增量信息。
_NOT_FOUND: str = "未找到该订单，或它不属于你"


def _as_view(row: dict[str, Any]) -> OrderView:
    """服务层的普通 dict → 响应模型（**唯一一处**转换）。

    为什么不让 FastAPI 直接用 `OrderView` 去校验服务层返回值：服务层的 dict 里
    `total_amount` 是**字符串**（十进制精确值），而模型的字段也是字符串 ——
    中间插一层显式的 `model_validate` 会让「契约不符」在**响应构造时**就炸出
    （500 + 清楚的字段名），而不是被 pydantic 静默强转成另一种形状。
    """
    return OrderView.model_validate(row)


@router.get(
    "",
    response_model=OrderListResponse,
    summary="我的订单列表（行级过滤 + 按角色脱敏 + 剩余可退额度）",
)
@require_permission(PERM_READ_OWN, identity_from="actor")
def list_my_orders(
    request: Request,
    actor: Actor = Depends(require_actor),
    db: Any = Depends(get_db),
    limit: int = Query(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description=f"最多返回多少笔（1~{MAX_LIMIT}）",
    ),
) -> OrderListResponse:
    """`GET /api/orders`：客户自己的订单（**行级过滤**在服务层，见模块 ①）。

    ⚠️ **员工主体的 `user_id` 是 0**：他调这个口会得到 422（`ToolContractError` → 其实
    是 400），而不是空列表。跨用户查订单请走对内路径（`POST /api/chat` 的分析/查询），
    那条路受 `read:all_orders` 管。这个区分是刻意的：**空列表会掩盖接线错误**。
    """
    del request  # 本口不需要它；保留参数是为了让 `require_actor` 拿到 request（FastAPI 注入）
    try:
        rows = list_orders_for_customer(
            actor.user_id,
            role=actor.role,
            limit=int(limit),
            caller=f"api:orders.list:account={actor.account_id}",
            session=db,
        )
    except ToolContractError as exc:
        # 员工的 user_id=0 走这里。用 400 而不是 404：这不是「资源不存在」，
        # 而是「这个身份根本不该用这个口」——报错要指向正确的做法（走对内路径）。
        raise ApiError(400, "no_customer_identity", str(exc)) from exc
    return OrderListResponse(
        orders=[_as_view(row) for row in rows],
        total=len(rows),
        limit=int(limit),
    )


@router.get(
    "/{order_id}",
    response_model=OrderView,
    summary="我的某一笔订单（含明细与该单退款记录）",
)
@require_permission(PERM_READ_OWN, identity_from="actor")
def get_my_order(
    order_id: int,
    request: Request,
    actor: Actor = Depends(require_actor),
    db: Any = Depends(get_db),
) -> OrderView:
    """`GET /api/orders/{order_id}`。

    ⚠️ **路径参数在静态路径之后声明**：本模块只有 `/orders/{order_id}` 一条参数路径，
    暂时没有冲突；但将来若加 `/orders/summary` 这类静态路径，**必须放在它前面** ——
    FastAPI 按声明顺序匹配，反了的话 `summary` 会被当成一个 `order_id`
    （症状是那个接口返回 422「不是合法的整数」，而路由看起来完全正确，
    与 `app/api/tasks.py` 记的 `/tasks/queue` 是同一个坑）。
    """
    del request
    try:
        row = get_order_view(
            actor.user_id,
            int(order_id),
            role=actor.role,
            caller=f"api:orders.view:account={actor.account_id}",
            session=db,
        )
    except ToolContractError as exc:
        raise ApiError(400, "no_customer_identity", str(exc)) from exc
    if row is None:
        raise ApiError(404, "not_found", _NOT_FOUND, detail={"order_id": int(order_id)})
    return _as_view(row)


__all__ = ["router"]
