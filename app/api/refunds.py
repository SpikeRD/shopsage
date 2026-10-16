"""退款接口（变更 003）：客户**申请**、员工**查询**、员工**审批**。

```
POST /api/refunds                        客户申请退款（要 request:refund）
GET  /api/refunds                        员工查询退款单（要 approve:refund，支持筛选）
POST /api/refunds/{refund_id}/decision   员工审批（要 approve:refund）
```

## 五条纪律

### ① 客户与员工共用这一个模块，但**授权完全不同**

| 动作 | 权限点 | 谁 | 产出 |
|---|---|---|---|
| 申请 | `request:refund` | 客户本人（+ admin） | 只能建 `requested` |
| 查询 | `approve:refund` | 客服/员工/主管/审计/管理员 | 只读 |
| 审批 | `approve:refund` | 同上 | `requested → approved/rejected` |

**客户拿不到 `approve:refund`** —— 这是他不能批准自己退款的全部依据（需求 2 的落点）。
每个口都用 `@require_permission(..., identity_from="actor")` 显式绑定身份参数
（仓库既有写法，避免装饰器看错变量 —— 见 `rbac.require_permission` 的长注释）。

### ② 申请接口**不接受**调用方自报的 `user_id`

退款归属只能是「令牌里的那个客户」（`actor.user_id`）。请求体里放 `user_id`
就是「让调用方声明这笔钱退给谁」——资金越权最经典的入口。
服务层还会再查一次「这笔订单是不是他的」（`_insert_refund_once` 的越权校验）。

### ③ 审批人来自令牌，不来自请求体

`decided_by` 取 `actor.subject_id`、`approver_role` 取 `actor.role`（都来自已验签身份）。
请求体里只有 `decision` 与 `note`。这样「谁批的」永远无法被调用方伪造。

### ④ 金额与时间一律出字符串

金额是 `NUMERIC(12,2)`（精确十进制），出网走 `str`，不过 JSON float。
时间走 ISO8601（`isoformat()`）—— 让前端能直接 `new Date(...)`，
而不是自己猜「这是秒还是毫秒」。

### ⑤ 数据层名字一律不 import（与 `orders.py` 同）

`db` 的类型注解写 `Any`，脱敏在服务层（`refund_rows_for_output`）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import Actor, require_actor
from app.api.errors import ApiError
from app.api.refund_errors import refund_errors
from app.api.schemas import (
    RefundDecisionRequest,
    RefundListResponse,
    RefundRequestCreate,
    RefundViewResponse,
)
from app.db.models import REFUND_STATUSES
from app.db.queries import DEFAULT_LIMIT, MAX_LIMIT
from app.db.session import get_db
from app.security.rbac import (
    PERM_APPROVE_REFUND,
    PERM_REQUEST_REFUND,
    require_permission,
)
from app.tools.refund import (
    decide_refund,
    get_refund_view,
    list_refunds_for_review,
    refund_rows_for_output,
    request_refund,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/refunds", tags=["refunds"])


def _iso(value: Any) -> str | None:
    """时间 → ISO8601 字符串（`None` 原样返回）。

    为什么统一在这里转：`datetime` 直接进 JSON 会被 FastAPI 序列化成 ISO ——
    但那只有走 pydantic 模型时才成立；服务层返回的是**普通 dict**，
    漏转一处就会变成「有的接口给字符串、有的给时间戳」，前端只能各写一套。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _refund_view(row: dict[str, Any]) -> RefundViewResponse:
    """服务层 dict → 响应模型（**唯一一处**转换；金额与时间在这里落成字符串）。"""
    return RefundViewResponse(
        refund_id=int(row["refund_id"]),
        order_id=int(row["order_id"]),
        user_id=int(row["user_id"]),
        amount=str(row["amount"]),
        status=str(row["status"]),
        reason=row.get("reason"),
        created_at=_iso(row.get("created_at")),
        decided_by=row.get("decided_by"),
        decided_at=_iso(row.get("decided_at")),
        decision_note=row.get("decision_note"),
        approver_role=row.get("approver_role"),
    )


def _parse_time(raw: str | None, *, field: str) -> datetime | None:
    """把查询串里的时间解成 `datetime`；解不出来 → 422 并说清格式。

    ⚠️ 只接受 ISO8601（`2026-10-01` 或 `2026-10-01T12:00:00`）：
    让 `created_from=昨天` 这种输入静默变成「不过滤」是最坏的选择 ——
    用户会以为自己筛过了，而看到的其实是全部数据。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ApiError(
            422,
            "invalid_time_filter",
            f"{field} 必须是 ISO8601 时间（例如 2026-10-01 或 2026-10-01T12:00:00），收到 {text!r}",
        ) from exc


# ── 客户：申请退款 ───────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=RefundViewResponse,
    status_code=status.HTTP_201_CREATED,
    summary="提交退款申请（客户自助；只能建 requested，等员工审批）",
)
@require_permission(PERM_REQUEST_REFUND, identity_from="actor")
def create_refund(
    body: RefundRequestCreate,
    request: Request,
    actor: Actor = Depends(require_actor),
    db: Any = Depends(get_db),
) -> RefundViewResponse:
    """`POST /api/refunds`：客户为一笔**自己的**订单提交退款申请。

    返回 **201 Created**（与 `POST /api/admin/accounts` 同一口径）：
    它新建了一行，客户端据此区分「建好了」与「查到了」。重复提交同一个
    `idempotency_key` 会**命中幂等**并返回**那一行的当前状态**（可能是 200 语义，
    但这里仍给 201 —— 幂等的承诺是「同一个键返回同一个结果」，不是「状态码也随次数变」；
    真正需要区分的是响应体里的 `refund_id` 是否与上次一致）。

    ⚠️ 权限是 `request:refund`（客户持有），而**不是** `write:refund` ——
    后者代表「代客户发起」这个更强的动作。这个区分就是需求 2 的落点：
    客户能申请，但批不了。
    """
    del request
    with refund_errors():
        ref = request_refund(
            actor.user_id,
            int(body.order_id),
            body.amount,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
            caller=f"api:refunds.create:account={actor.account_id}",
            role=actor.role,
            session=db,
        )
        db.commit()
        # 回读一次拿到完整视图（含 created_at 与审批留痕列）：
        # `request_refund` 返回的是**最小回执**（RefundRef），刻意不含那些字段。
        #
        # ⚠️ 必须用 `get_refund_view(owner_user_id=...)` 而**不是** `list_refunds_for_review`：
        # 后者要求 `approve:refund`，而本接口的调用者是客户 —— 用它回读会让
        # 「提交成功」变成 404（实测踩到：`missing=['approve:refund']`）。
        # 「读自己的单」与「读全站单」是两件事，各有各的授权。
        view = get_refund_view(ref.refund_id, owner_user_id=actor.user_id, session=db)
    if view is None:
        # 刚插入的行读不到 = 事务/隔离级别出了问题，必须显式暴露而不是回一个空响应
        raise ApiError(500, "refund_readback_failed", "退款单已受理但读不回来，请联系管理员")
    logger.info(
        "退款申请已受理：refund_id=%s order_id=%s user_id=%s amount=%s role=%s",
        ref.refund_id,
        ref.order_id,
        ref.user_id,
        ref.amount,
        actor.role,
    )
    return _refund_view(
        refund_rows_for_output(
            [view], role=actor.role, caller=f"api:refunds.create:account={actor.account_id}"
        )[0]
    )


# ── 员工：查询 ───────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=RefundListResponse,
    summary="查询退款单（员工；支持单号/订单号/客户/状态/时间范围筛选）",
)
@require_permission(PERM_APPROVE_REFUND, identity_from="actor")
def list_refunds(
    request: Request,
    actor: Actor = Depends(require_actor),
    db: Any = Depends(get_db),
    refund_id: int | None = Query(default=None, ge=1, description="按退款单号精确查"),
    order_id: int | None = Query(default=None, ge=1, description="按订单号精确查"),
    user_id: int | None = Query(
        default=None, ge=1, description="按客户 user_id 精确查（**不是手机号**，理由见下）"
    ),
    status_filter: list[str] | None = Query(
        default=None,
        alias="status",
        description=f"按状态筛，可重复传多个：{'/'.join(REFUND_STATUSES)}",
    ),
    created_from: str | None = Query(
        default=None, description="创建时间下界（ISO8601，含）"
    ),
    created_to: str | None = Query(default=None, description="创建时间上界（ISO8601，含）"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description=f"1~{MAX_LIMIT}"),
) -> RefundListResponse:
    """`GET /api/refunds`：员工审批台的查询。

    ## 为什么不支持按客户手机号搜（一个刻意的「少做」）

    手机号是 PII，而 `customer_service` 的清晰度上限是**公开级(0)** ——
    把手机号做成可搜索字段，等于给「可见度最低的角色」一个**存在性探测**工具
    （「这个号在我们这有没有单」本身就是信息，而它连脱敏后的号码都不该看到）。
    需要按客户定位时用 `user_id`（内部标识，不泄漏 PII）。

    ## 状态为什么是**多选**

    员工最常做的事情是「把所有待审批的捞出来」（`status=requested`），
    第二常见的是「这笔单最后怎么了」（`status=approved&status=rejected`）。
    单选做不到第二件事，只能查两次再人工合并。

    ⚠️ 参数名叫 `status_filter` 而对外是 `?status=`：与 `app/api/admin.py` 的
    `account_role` 同一个手法 —— 让「筛选条件」与「调用者身份」在任何读代码的人眼里
    都不是同一个词（`require_permission` 的身份解析曾经在这里看错过变量）。
    """
    del request
    statuses = [str(item) for item in (status_filter or [])]
    with refund_errors():
        views = list_refunds_for_review(
            refund_id=refund_id,
            order_id=order_id,
            user_id=user_id,
            statuses=statuses,
            created_from=_parse_time(created_from, field="created_from"),
            created_to=_parse_time(created_to, field="created_to"),
            limit=int(limit),
            caller=f"api:refunds.list:account={actor.account_id}",
            role=actor.role,
            session=db,
        )
    rows = refund_rows_for_output(
        views, role=actor.role, caller=f"api:refunds.list:account={actor.account_id}"
    )
    return RefundListResponse(
        refunds=[_refund_view(row) for row in rows],
        total=len(rows),
    )


# ── 员工：审批 ───────────────────────────────────────────────────────────────


@router.post(
    "/decision",
    response_model=RefundViewResponse,
    summary="审批退款单（员工；requested → approved / rejected）",
)
@require_permission(PERM_APPROVE_REFUND, identity_from="actor")
def decide(
    body: RefundDecisionRequest,
    request: Request,
    actor: Actor = Depends(require_actor),
    db: Any = Depends(get_db),
) -> RefundViewResponse:
    """`POST /api/refunds/decision`：通过或驳回（**退款单号在请求体里**）。

    ## ⚠️ 为什么路径不是 `/refunds/{refund_id}/decision`（一个刻意的取舍）

    `app/api/scope.py` 的 `SCOPE_BY_ROUTE` 与 `middleware.LIMITED_SCOPES` 都是
    **精确的 `(method, path)` 匹配**（模块 docstring 写明「路径参数不在这里出现」）。
    带路径参数的审批口**匹配不上任何 scope**，于是它会静默退回「不限流」——
    而限流的失效**不会有任何报错**（`tests/api/test_api_contracts.py` 的反向断言
    正是为这类静默失效写的）。

    两条路可选：① 给 scope 表加 pattern 匹配（改基础设施，收益是「URL 更好看」）；
    ② 把单号放进请求体（本次采用）。②的成本是 URL 不如 RESTful，
    换来的是「这条写路径**确实**受限流保护」——对资金动作来说，后者重要得多。

    三条口径：

    1. **状态机只有一条**：`requested → approved / rejected`。已经批过/驳过的单再批
       返回 **409**（`refund_not_decidable`）并带上当前状态 —— 这是并发与重复点击的
       **正常结果**，不是错误用法（语义见 `RefundNotDecidable` 的 docstring）。
    2. **审批人来自令牌**：`decided_by` = `actor.subject_id`（员工主体 id）、
       `approver_role` = `actor.role` 的快照。请求体里没有、也不接受这两个字段。
    3. **不改订单状态、不接支付通道**（变更 003 §2.2.1 的显式排除项）：
       `approved` 只表示「这笔退款被批准了」，钱怎么退是另一件事。
    """
    del request
    with refund_errors():
        view = decide_refund(
            int(body.refund_id),
            decision=body.decision,
            note=body.note,
            decider_subject_id=actor.subject_id,
            decider_subject_type=actor.subject_type,
            caller=f"api:refunds.decide:account={actor.account_id}",
            role=actor.role,
            session=db,
        )
        db.commit()
    rows = refund_rows_for_output(
        [view], role=actor.role, caller=f"api:refunds.decide:account={actor.account_id}"
    )
    logger.info(
        "退款审批完成：refund_id=%s decision=%s -> %s approver=%s role=%s",
        body.refund_id,
        body.decision,
        view.status,
        actor.subject_id,
        actor.role,
    )
    return _refund_view(rows[0])


__all__ = ["router"]
