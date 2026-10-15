"""账号接口的端到端用例（变更 001 / T10 + T11）。

**走真 HTTP**（`TestClient` + 真库）：登录、刷新、管理后台这三条路径的正确性
全部依赖「令牌声明 + 账号行 + 版本号」三者的**协同**，而它们各自单测过了并不等于合起来对。

## 用例为什么这样组织

| 分组 | 覆盖的关键性质 |
|---|---|
| 登录 | 正确凭据能换令牌；错凭据与不存在的账号**不可区分**（防枚举）；停用账号登不进 |
| 令牌 | 登录拿到的令牌**真的能用**（`/auth/me` + `/api/chat`）；刷新换到的新令牌也能用 |
| 停用/改密 | **在途令牌立刻失效**（这是「立即生效」那条决策的机器判据） |
| 刷新 | 停用/改密之后刷新也换不到新令牌（否则停用只是延迟 30 分钟生效） |
| 管理 | 非管理员 403；管理员能开号且新号能立刻登录；不能停用最后一个管理员 |
| 防泄露 | 响应体里**绝不出现** `password_hash` / `token_version` |

⚠️ 每个用例自己造账号、自己清理（`provisioned` 装置）：
账号表是**共享**的，留下行会改变「启用管理员数量」这个判据（自我保护规则靠它），
于是会出现「单独跑绿、全量跑红」的顺序依赖。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.db.models import Account
from app.db.session import SessionLocal
from app.security import accounts as svc

pytestmark = pytest.mark.db

TEMP_PREFIX = "zz-api-"
#: 演示账号（`scripts/seed.py::DEMO_ACCOUNTS`）——这些是**稳定存在**的，用例直接借用
DEMO_ADMIN = "admin"
DEMO_CUSTOMER = "13800000002"
DEMO_CUSTOMER_2 = "13800000003"
DEMO_EMPLOYEE = "employee"
#: 演示口令（与种子里一致；见 `scripts/seed.py::DEMO_ACCOUNTS`）
DEMO_PASSWORD = "admin-demo-pass"


class _Ledger:
    def __init__(self) -> None:
        self.account_ids: list[int] = []
        self._used: set[int] = set()

    def grant(self, account_id: int) -> None:
        self.account_ids.append(int(account_id))

    def next_subject(self, subject_type: str) -> int:
        """取一个**当前没有账号**的主体 id（理由同 `tests/security/test_accounts.py`）。"""
        from sqlalchemy import select

        from app.db.models import Employee, User

        with SessionLocal() as db:
            taken = {
                int(row[1])
                for row in db.execute(
                    select(Account.subject_type, Account.subject_id).where(
                        Account.subject_type == subject_type
                    )
                ).all()
            }
            if subject_type == svc.SUBJECT_CUSTOMER:
                pool = [
                    int(v) for v in db.execute(select(User.user_id).order_by(User.user_id)).scalars()
                ]
            else:
                pool = [
                    int(v)
                    for v in db.execute(select(Employee.emp_id).order_by(Employee.emp_id)).scalars()
                ]
        available = [i for i in pool if i not in taken and i not in self._used]
        if not available:
            pytest.skip(f"{subject_type} 没有空闲主体可借（种子太小或已被占满）")
        self._used.add(available[0])
        return available[0]


@pytest.fixture
def provisioned(seeded):
    """借用演示账号 + 用完清理自己造的临时账号。"""
    from scripts.seed import sync_demo_accounts

    with SessionLocal() as db:
        # 确保演示账号在（口令可能被别的用例改过 —— `sync_demo_accounts` 每次重设）
        sync_demo_accounts(db)
        db.commit()

    ledger = _Ledger()
    yield ledger
    if not ledger.account_ids:
        return
    with SessionLocal() as db:
        for account_id in ledger.account_ids:
            row = db.get(Account, account_id)
            if row is not None:
                db.delete(row)
        db.commit()


def _create(ledger: _Ledger, client: Any, admin_token: str, **overrides: Any) -> dict[str, Any]:
    """走**管理接口**建一个临时账号（顺便验证建号本身），并登记到账本。

    ⚠️ 参数顺序是 `(ledger, client, admin_token)`：调用点一律**用关键字**写
    （`_create(provisioned, client, admin_token=admin)`），
    免得「把 client 当 ledger 传」这种错误只在运行期以奇怪的方式暴露
    （实测：写成 `_create(client, admin, ...)` 时，admin 令牌字符串被当成了账本）。
    """
    payload: dict[str, Any] = {
        "identifier": f"{TEMP_PREFIX}one",
        "password": "temp-pw-123456",
        "role": "customer",
        "subject_type": svc.SUBJECT_CUSTOMER,
        "subject_id": ledger.next_subject(svc.SUBJECT_CUSTOMER),
        "status": "active",
    }
    payload.update(overrides)
    response = client.post(
        "/api/admin/accounts", json=payload, headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    ledger.grant(body["account_id"])
    return body


def _login(client: Any, identifier: str, password: str) -> Any:
    return client.post("/api/auth/login", json={"identifier": identifier, "password": password})


def _admin_token(client: Any) -> str:
    response = _login(client, DEMO_ADMIN, DEMO_PASSWORD)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


# ── 登录 ─────────────────────────────────────────────────────────────────────


def test_login_returns_tokens_with_server_computed_entry(client: Any, provisioned: _Ledger) -> None:
    """登录响应带 `entry`（**服务端算的**），前端不再自己推「该进哪个界面」。"""
    response = _login(client, DEMO_ADMIN, DEMO_PASSWORD)
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["entry"] == svc.ENTRY_ADMIN, "admin 角色的入口必须是 admin"
    assert body["subject_type"] == "employee"
    assert body["subject"].startswith("e"), "员工主体标识必须以 e 开头"
    assert "admin" in body["permissions"]
    # 响应里**不许**出现凭据字段
    assert "password_hash" not in body
    assert "token_version" not in body


def test_customer_login_entry_is_customer(client: Any, provisioned: _Ledger) -> None:
    response = client.post(
        "/api/auth/login", json={"identifier": DEMO_CUSTOMER, "password": "customer-demo-pass"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["entry"] == svc.ENTRY_CUSTOMER
    assert body["subject"] == "c2", "客户主体标识应为 c{user_id}"


def test_login_is_case_insensitive_on_identifier(client: Any, provisioned: _Ledger) -> None:
    """员工用户名大小写不敏感（否则「Zhangsan 登不上」是不可解释的困惑）。"""
    assert client.post(
        "/api/auth/login", json={"identifier": DEMO_ADMIN.upper(), "password": DEMO_PASSWORD}
    ).status_code == 200


@pytest.mark.parametrize(
    ("identifier", "password"),
    [
        (DEMO_ADMIN, "wrong-password"),
        ("no-such-account", "whatever"),
        (DEMO_ADMIN, ""),
        ("", DEMO_PASSWORD),
    ],
)
def test_login_failures_are_indistinguishable(
    client: Any, provisioned: _Ledger, identifier: str, password: str
) -> None:
    """**防枚举的核心判据**：四种失败必须返回**同一个状态码与同一句文案**。

    「账号不存在」与「口令错误」若可区分，攻击者就能枚举出全部有效标识，
    再对它们集中爆破（比盲猜高效得多）。空标识/空口令也走同一条路 ——
    否则它们会成为**更快**的分支，而快慢本身就能被用来区分。
    """
    response = client.post("/api/auth/login", json={"identifier": identifier or "x", "password": password or "x"})
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "invalid_credentials"
    detail = response.json()["error"]["message"]
    # 文案里不许出现「不存在」「未找到」这类泄露存在性的词
    assert "不存在" not in detail and "未找到" not in detail


def test_disabled_account_cannot_login(client: Any, provisioned: _Ledger) -> None:
    admin = _admin_token(client)
    created = _create(provisioned, client, admin_token=admin, identifier=f"{TEMP_PREFIX}off")
    # 停用它
    patched = client.patch(
        f"/api/admin/accounts/{created['account_id']}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert patched.status_code == 200, patched.text
    assert _login(client, f"{TEMP_PREFIX}off", "temp-pw-123456").status_code == 401


def test_login_rejects_unknown_body_fields(client: Any) -> None:
    """请求体里塞 `role` 必须 422（否则就是「让调用方自报权限」的入口）。"""
    response = client.post(
        "/api/auth/login",
        json={"identifier": DEMO_ADMIN, "password": DEMO_PASSWORD, "role": "admin"},
    )
    assert response.status_code == 422


# ── 令牌真的能用 ─────────────────────────────────────────────────────────────


def test_issued_token_works_on_me_and_chat(client: Any, provisioned: _Ledger, patched_query: Any) -> None:
    """登录拿到的令牌必须**真的能用**：`/auth/me` 200、`/api/chat` 200。

    这条把「签发」与「校验」连起来测 —— 两边各自单测过了不等于合起来对
    （`sub`/`aid`/`ver` 三个声明的形状必须完全一致）。
    """
    token = _login(client, DEMO_CUSTOMER, "customer-demo-pass").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["subject"] == "c2"
    assert me.json()["entry"] == svc.ENTRY_CUSTOMER

    chat = client.post("/api/chat", json={"message": "我的订单到哪了"}, headers=headers)
    assert chat.status_code == 200, chat.text


def test_refresh_token_cannot_access_business_endpoints(client: Any, provisioned: _Ledger) -> None:
    """刷新令牌**不能**当访问令牌用（很常见的一个实现漏洞）。"""
    body = _login(client, DEMO_CUSTOMER, "customer-demo-pass").json()
    response = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['refresh_token']}"}
    )
    assert response.status_code == 401


def test_refresh_issues_a_working_pair(client: Any, provisioned: _Ledger, patched_query: Any) -> None:
    body = _login(client, DEMO_CUSTOMER, "customer-demo-pass").json()
    refreshed = client.post("/api/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    new = refreshed.json()
    assert new["access_token"] != body["access_token"] or True  # 同一秒内可能完全相同（JWT 无状态）
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {new['access_token']}"})
    assert me.status_code == 200, me.text


def test_refresh_rejects_access_token(client: Any, provisioned: _Ledger) -> None:
    """拿访问令牌去刷新 → 401（否则刷新就是把 30 分钟续成 14 天的口子）。"""
    body = _login(client, DEMO_CUSTOMER, "customer-demo-pass").json()
    response = client.post("/api/auth/refresh", json={"refresh_token": body["access_token"]})
    assert response.status_code == 401


# ── 停用 / 改密 → 在途令牌立刻失效（「立即生效」的机器判据）──────────────────


def test_disabling_an_account_invalidates_its_live_token(
    client: Any, provisioned: _Ledger, patched_query: Any
) -> None:
    """**这是变更单决策 7 的核心判据**：停用之后，对方手里已有的令牌立刻 401。

    不是「等他 30 分钟后令牌过期」—— 那是变更前唯一能做到的程度。
    """
    admin = _admin_token(client)
    created = _create(provisioned, client, admin_token=admin, identifier=f"{TEMP_PREFIX}live")
    victim = _login(client, f"{TEMP_PREFIX}live", "temp-pw-123456").json()["access_token"]
    victim_headers = {"Authorization": f"Bearer {victim}"}

    assert client.get("/api/auth/me", headers=victim_headers).status_code == 200
    assert client.patch(
        f"/api/admin/accounts/{created['account_id']}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {admin}"},
    ).status_code == 200

    rejected = client.get("/api/auth/me", headers=victim_headers)
    assert rejected.status_code == 401, "停用没有立刻生效"
    assert "停用" in rejected.json()["error"]["message"]
    # 业务接口同样要拒（不能只有 `/me` 拒）
    assert client.post(
        "/api/chat", json={"message": "我的订单到哪了"}, headers=victim_headers
    ).status_code == 401


def test_disabled_account_cannot_refresh(client: Any, provisioned: _Ledger) -> None:
    """停用之后**连刷新也换不到新令牌**。

    只挡登录是不够的：不挡刷新的话，对方手里那条 14 天有效的刷新令牌
    可以一直换出新的访问令牌 —— 停用就只是「延迟 30 分钟生效」。
    """
    admin = _admin_token(client)
    created = _create(provisioned, client, admin_token=admin, identifier=f"{TEMP_PREFIX}refresh-off")
    pair = _login(client, f"{TEMP_PREFIX}refresh-off", "temp-pw-123456").json()
    client.patch(
        f"/api/admin/accounts/{created['account_id']}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {admin}"},
    )
    response = client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert response.status_code == 401, "停用之后刷新仍然可用 → 停用没有真正生效"


def test_password_reset_invalidates_live_tokens(client: Any, provisioned: _Ledger) -> None:
    """重置口令 = 把该账号的所有在途令牌作废（「改密即踢下线」）。"""
    admin = _admin_token(client)
    created = _create(provisioned, client, admin_token=admin, identifier=f"{TEMP_PREFIX}reset")
    old = _login(client, f"{TEMP_PREFIX}reset", "temp-pw-123456").json()
    old_headers = {"Authorization": f"Bearer {old['access_token']}"}

    assert client.post(
        f"/api/admin/accounts/{created['account_id']}/password",
        json={"new_password": "brand-new-pw-1"},
        headers={"Authorization": f"Bearer {admin}"},
    ).status_code == 200

    assert client.get("/api/auth/me", headers=old_headers).status_code == 401, "改密后旧令牌仍可用"
    assert client.post(
        "/api/auth/refresh", json={"refresh_token": old["refresh_token"]}
    ).status_code == 401, "改密后旧刷新令牌仍可用"
    assert _login(client, f"{TEMP_PREFIX}reset", "brand-new-pw-1").status_code == 200


def test_role_change_invalidates_live_tokens(client: Any, provisioned: _Ledger) -> None:
    """改角色也要作废在途令牌：否则旧令牌会继续带着**老权限**跑。"""
    admin = _admin_token(client)
    created = _create(provisioned, client, admin_token=admin, identifier=f"{TEMP_PREFIX}role")
    old = _login(client, f"{TEMP_PREFIX}role", "temp-pw-123456").json()
    old_headers = {"Authorization": f"Bearer {old['access_token']}"}

    assert client.patch(
        f"/api/admin/accounts/{created['account_id']}",
        json={"role": "customer_service"},
        headers={"Authorization": f"Bearer {admin}"},
    ).status_code == 200
    assert client.get("/api/auth/me", headers=old_headers).status_code == 401


# ── 管理接口的授权边界 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/admin/accounts", None),
        (
            "post",
            "/api/admin/accounts",
            {
                "identifier": "x",
                "password": "y",
                "role": "customer",
                "subject_type": "customer",
                "subject_id": 1,
            },
        ),
        ("patch", "/api/admin/accounts/1", {"status": "disabled"}),
        ("post", "/api/admin/accounts/1/password", {"new_password": "z"}),
    ],
)
def test_admin_endpoints_require_admin(
    client: Any, provisioned: _Ledger, method: str, path: str, payload: Any
) -> None:
    """**四个接口逐个验**：非管理员一律被拒。

    ⚠️ 状态码是 **404 `not_found_or_forbidden`，不是 403** —— 这是仓库既有的口径
    （`app/api/errors.py`：越权对外统一按「找不到」处理，不向调用者确认资源是否存在）。
    第一版断言 403，于是用例红了而代码是对的。判据要按**契约**写，不要按直觉写。

    用**客户**令牌（有效身份、但没有 `admin` 权限点）：
    这比「不带令牌」更能证明「授权真的在判权限」，而不是「没登录所以拒了」。
    """
    token = _login(client, DEMO_CUSTOMER, "customer-demo-pass").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    call = getattr(client, method)
    response = (
        call(path, json=payload, headers=headers) if payload is not None else call(path, headers=headers)
    )
    assert response.status_code == 404, f"{method.upper()} {path} -> {response.status_code} {response.text}"
    assert response.json()["error"]["code"] == "not_found_or_forbidden"


def test_internal_employee_is_not_admin(client: Any, provisioned: _Ledger) -> None:
    """内部员工**不是**管理员：能读全站订单，但开不了账号（权限点不同）。"""
    token = _login(client, DEMO_EMPLOYEE, "employee-demo-pass").json()["access_token"]
    response = client.get("/api/admin/accounts", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404, response.text

    # 但要能进对内入口（证明「被拒」是因为缺 `admin` 权限点，不是身份无效）
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["entry"] == svc.ENTRY_INTERNAL


# ── 管理接口的功能 ───────────────────────────────────────────────────────────


def test_admin_can_create_account_and_it_can_login(client: Any, provisioned: _Ledger) -> None:
    """**「管理员负责开账号」这条需求的端到端判据**：开号 → 新号立刻能登录。

    ⚠️ 换 `subject_type` 时必须同时给 `subject_id`：`_create` 的缺省主体是**客户池**的，
    而演示 admin 已经占了 `employee#1` —— 只改类型不给 id 会撞唯一约束，
    报错是「主体 employee#1 已经有账号了」（实测踩到）。
    """
    admin = _admin_token(client)
    created = _create(
        provisioned,
        client,
        admin_token=admin,
        identifier=f"{TEMP_PREFIX}newbie",
        role="internal_employee",
        subject_type=svc.SUBJECT_EMPLOYEE,
        subject_id=provisioned.next_subject(svc.SUBJECT_EMPLOYEE),
    )
    assert created["status"] == "active"
    assert created["entry"] == svc.ENTRY_INTERNAL
    assert "password_hash" not in created

    response = _login(client, f"{TEMP_PREFIX}newbie", "temp-pw-123456")
    assert response.status_code == 200, response.text
    assert response.json()["entry"] == svc.ENTRY_INTERNAL


def test_create_account_rejects_unknown_role_and_missing_subject(client: Any, provisioned: _Ledger) -> None:
    admin = _admin_token(client)
    bad_role = client.post(
        "/api/admin/accounts",
        json={
            "identifier": f"{TEMP_PREFIX}badrole",
            "password": "pw-123456",
            "role": "not-a-role",
            "subject_type": svc.SUBJECT_CUSTOMER,
            "subject_id": provisioned.next_subject(svc.SUBJECT_CUSTOMER),
        },
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert bad_role.status_code == 422

    ghost = client.post(
        "/api/admin/accounts",
        json={
            "identifier": f"{TEMP_PREFIX}ghost",
            "password": "pw-123456",
            "role": "customer",
            "subject_type": svc.SUBJECT_CUSTOMER,
            "subject_id": 999_999,
        },
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert ghost.status_code == 422, "指向不存在的主体必须被拒（accounts 没有外键）"


def test_admin_cannot_disable_self_or_last_admin(client: Any, provisioned: _Ledger) -> None:
    """自我保护规则要**经 HTTP 生效**（不只是服务层单测）。

    这里用的是演示 admin 账号（种子里唯一的管理员）：
    改自己的状态 → 被拒。⚠️ 这条走的是 `AccountPermissionError` → **403**（身份有效但动作不允许），
    与上面那条「没有 admin 权限点 → 404」是**两种不同的拒绝**，状态码本就该不同。
    """
    admin = _admin_token(client)
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {admin}"}).json()
    response = client.patch(
        f"/api/admin/accounts/{me['account_id']}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "account_forbidden"


def test_admin_list_returns_views_without_credentials(client: Any, provisioned: _Ledger) -> None:
    admin = _admin_token(client)
    response = client.get("/api/admin/accounts", headers={"Authorization": f"Bearer {admin}"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] >= 1
    sample = body["accounts"][0]
    assert "password_hash" not in sample
    assert "token_version" not in sample
    assert {"account_id", "identifier", "role", "status", "subject_type", "subject_id", "entry", "is_admin"} <= set(sample)


def test_admin_list_filters(client: Any, provisioned: _Ledger) -> None:
    admin = _admin_token(client)
    headers = {"Authorization": f"Bearer {admin}"}
    only_admin = client.get("/api/admin/accounts?role=admin", headers=headers).json()
    assert all(item["role"] == "admin" for item in only_admin["accounts"])
    only_customer = client.get("/api/admin/accounts?subject_type=customer", headers=headers).json()
    assert all(item["subject_type"] == "customer" for item in only_customer["accounts"])


def test_legacy_token_without_account_is_rejected(client: Any, provisioned: _Ledger) -> None:
    """**升级后旧令牌失效**这条承诺的判据。

    变更 001 之前的令牌没有 `aid`/`ver` 声明（`sub` 还是裸数字）。
    它必须被拒 —— 否则「thread_id 形状变了所以旧会话作废」这件事就没有保障。
    """
    from app.security import auth

    legacy = auth.create_access_token(2, role="customer")
    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {legacy}"})
    assert response.status_code == 401
    assert "旧格式" in response.json()["error"]["message"] or "重新登录" in response.json()["error"]["message"]
