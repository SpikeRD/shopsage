"""越权拦截用例（`4.security.md` §7「越权（D1）」）。

对客路径（行级过滤）与对内路径（RBAC）分开测，因为它们的授权粒度不同：

- 对客：「这条**行**是不是我的」——本模块测「身份与请求的 user_id 必须一致」，
  以及「编译后的 SQL 必须带行级过滤列」；
- 对内：「这个**角色**能不能做这件事」——本模块测权限表与装饰器（含 `async def`）。

⚠️ 这里**不测**「WHERE 到底有没有真的进 SQL」的运行时效果 —— 那是
`tests/test_db_queries.py`（真库）与 `docs/runbook/3.db-runbook.md` §5.2（库侧 `pg_stat_statements`）的活。
两边合起来才是完整的 D1 证据：本模块保证「不给自己以外的人开口子」，
真库用例保证「开了口子之后真的只返回自己的行」。
"""

from __future__ import annotations

import asyncio

import pytest

from app.security.policy import CUSTOMER_ROLE, ROLE_MAX_SENSITIVITY
from app.security.rbac import (
    PERM_READ_ALL_ORDERS,
    PERM_READ_FINANCE,
    ROLE_PERMISSIONS,
    CustomerIdentity,
    PermissionDenied,
    assert_row_filter_present,
    has_permission,
    permissions_for,
    require_permission,
    require_role,
    row_filter_for_customer,
    visible_sensitivity_ceiling,
)


# ── ① 对客：行级过滤的校验 ───────────────────────────────────────────────────


def test_customer_can_query_own_id():
    identity = CustomerIdentity(user_id=1)
    assert row_filter_for_customer(identity, 1) == "user_id"


@pytest.mark.parametrize("other", [0, 2, 99, -1])
def test_customer_cannot_query_other_ids(other: int):
    """`4.security.md` §7 的越权用例：客户只能查自己的 user_id。"""
    identity = CustomerIdentity(user_id=1)
    with pytest.raises(PermissionDenied) as exc:
        row_filter_for_customer(identity, other)
    assert "越权" in str(exc.value)


def test_internal_role_without_read_all_cannot_query_any_user():
    """客服（无 `read:all_orders`）也不能借这个函数跨用户查 —— 默认拒绝。"""
    identity = CustomerIdentity(user_id=1, role="customer_service")
    with pytest.raises(PermissionDenied) as exc:
        row_filter_for_customer(identity, 2)
    assert PERM_READ_ALL_ORDERS in str(exc.value)


def test_internal_role_with_read_all_may_query_any_user():
    """有 `read:all_orders` 的角色（经理/审计/管理员）走的是对内路径，不受对客限制。"""
    for role in ("manager", "finance_auditor", "admin"):
        identity = CustomerIdentity(user_id=1, role=role)
        assert row_filter_for_customer(identity, 2) == "user_id"


def test_row_filter_present_accepts_filtered_statement():
    from sqlalchemy import select

    from app.db.models import Order

    stmt = select(Order.order_id).where(Order.user_id == 1)
    sql = assert_row_filter_present(stmt)
    assert "WHERE" in sql.upper() and "user_id" in sql


def test_row_filter_present_rejects_naked_query():
    """裸查询（没有行级过滤列）必须被拦 —— `WHERE 1=1` 也想蒙过去。"""
    from sqlalchemy import select, text

    from app.db.models import Order

    with pytest.raises(PermissionDenied):
        assert_row_filter_present(select(Order.order_id))

    with pytest.raises(PermissionDenied):
        assert_row_filter_present(select(Order.order_id).where(text("1 = 1")))


def test_row_filter_present_requires_equality_not_just_mention():
    """只是「提到 user_id」不算过滤：必须是 `user_id = ...` 的等值条件。"""
    from sqlalchemy import select

    from app.db.models import Order

    stmt = select(Order.user_id).where(Order.status == "paid")  # 选了 user_id，但按 status 过滤
    with pytest.raises(PermissionDenied):
        assert_row_filter_present(stmt)


# ── ② 对内：RBAC ────────────────────────────────────────────────────────────


def test_permissions_table_shape():
    """权限表自检：每个角色都要有权限点，且权限点是 `动词:资源` 形式。

    `admin` 是唯一的例外：它是「运维总开关」，本身不代表某类资源，
    所以显式豁免而不是硬塞成 `admin:all`（那会让人以为它对应某个资源）。
    """
    for role, perms in ROLE_PERMISSIONS.items():
        assert perms, f"{role} 没有任何权限点"
        for perm in perms:
            if perm == "admin":
                assert role == "admin", "只有 admin 角色能持有 admin 权限点"
                continue
            assert ":" in perm, f"{role} 的权限点 {perm} 不符合 动词:资源 约定"


def test_unknown_role_has_no_permissions():
    """未登记角色 = 空权限集（默认拒绝），而不是「默认全开」或报错放行。"""
    assert permissions_for("ghost") == frozenset()
    assert has_permission("ghost", PERM_READ_ALL_ORDERS) is False
    assert has_permission("", PERM_READ_FINANCE) is False


def test_read_capability_ladder_is_monotonic():
    """**读**能力必须沿着角色阶梯单调递增：低级别能读的，高级别一定能读。

    这条防的是「补权限时手滑」—— 比如给审计加了 `read:finance` 却漏了 `read:all_orders`，
    于是「能看财务报表但看不了订单」这种自相矛盾的角色就诞生了。
    """
    ladder = [
        CUSTOMER_ROLE,
        "customer_service",
        "internal_employee",
        "manager",
        "finance_auditor",
        "admin",
    ]
    read_only = lambda perms: frozenset(p for p in perms if p.startswith("read:"))  # noqa: E731
    for lower, higher in zip(ladder, ladder[1:], strict=False):
        assert read_only(permissions_for(lower)) <= read_only(permissions_for(higher)), (
            f"{lower} 的读权限不是 {higher} 的子集"
        )


def test_write_capability_is_not_a_ladder():
    """**写**权限**不是**单调阶梯：审计角色能看财务，但**不能**发起退款。

    这是有意的设计，不是遗漏：`finance_auditor` 的职责是「看账」，不是「动账」；
    把它当成「更高级的 manager」会让职责分离（SoD）失效 —— 一个人既能看账又能改账，
    审计就失去了意义。所以写权限单独断言，而不是塞进单调性测试里。
    """
    assert "write:refund" in permissions_for("manager")
    assert "write:refund" not in permissions_for("finance_auditor")
    assert "write:refund" not in permissions_for(CUSTOMER_ROLE)
    assert "write:refund" in permissions_for("admin")  # 运维总开关仍有全部写权限


def test_finance_permission_is_restricted():
    """财务级权限只给审计与管理员。"""
    holders = {role for role, perms in ROLE_PERMISSIONS.items() if PERM_READ_FINANCE in perms}
    assert holders == {"finance_auditor", "admin"}


def test_require_role_allows_and_denies():
    @require_role("manager", "admin")
    def refund(role: str) -> str:
        return "ok"

    assert refund(role="manager") == "ok"
    with pytest.raises(PermissionDenied):
        refund(role="customer")


def test_require_role_reads_role_from_first_argument_object():
    """角色也可以从第一个参数对象上取（兼容身份对象/依赖注入对象）。"""

    @require_role("manager")
    def report(identity: CustomerIdentity) -> str:
        return identity.role

    assert report(CustomerIdentity(user_id=1, role="manager")) == "manager"
    with pytest.raises(PermissionDenied):
        report(CustomerIdentity(user_id=1))


def test_require_permission_denies_missing_and_lists_them():
    @require_permission(PERM_READ_FINANCE)
    def finance_report(role: str) -> str:
        return "ok"

    with pytest.raises(PermissionDenied) as exc:
        finance_report(role="internal_employee")
    assert exc.value.missing == (PERM_READ_FINANCE,)
    assert finance_report(role="finance_auditor") == "ok"


def test_require_permission_requires_all_listed_permissions():
    @require_permission(PERM_READ_FINANCE, PERM_READ_ALL_ORDERS)
    def both(role: str) -> str:
        return "ok"

    # 审计有 read:finance + read:all_orders → 通过；客服两个都没有 → 拒绝
    assert both(role="finance_auditor") == "ok"
    with pytest.raises(PermissionDenied) as exc:
        both(role="customer_service")
    assert set(exc.value.missing) == {PERM_READ_FINANCE, PERM_READ_ALL_ORDERS}


def test_decorators_work_on_async_routes():
    """FastAPI 路由是 async 的：装饰器必须同时支持两种形态（否则第 9 步才发现）。"""

    @require_permission(PERM_READ_ALL_ORDERS)
    async def list_orders(role: str) -> str:
        return "ok"

    assert asyncio.run(list_orders(role="manager")) == "ok"
    with pytest.raises(PermissionDenied):
        asyncio.run(list_orders(role="customer"))


def test_decorator_preserves_function_metadata():
    """装饰后函数名/文档要保留（否则 FastAPI 的路由名会变成 `wrapper`，排障很痛苦）。"""

    @require_role("admin")
    def healthz(role: str) -> str:
        """健康检查。"""
        return "ok"

    assert healthz.__name__ == "healthz"
    assert healthz.__doc__ == "健康检查。"


# ── ③ 与脱敏模块的接口一致 ───────────────────────────────────────────────────


def test_sensitivity_ceiling_matches_masking_policy():
    """角色等级上限只有一个真相（policy），未登记角色为 -1。"""
    for role, ceiling in ROLE_MAX_SENSITIVITY.items():
        assert visible_sensitivity_ceiling(role) == ceiling
    assert visible_sensitivity_ceiling("ghost") == -1
    assert visible_sensitivity_ceiling(CUSTOMER_ROLE) >= 0
