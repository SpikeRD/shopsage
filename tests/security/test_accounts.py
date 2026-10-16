"""账号服务用例（变更 001 / T9）。

**分两层**：

- **纯逻辑层**（不需要库）：口令归一化、入口分流、角色/状态白名单校验；
- **真库层**（`@pytest.mark.db`）：认证、生命周期、自我保护规则 —— 这些的性质
  （唯一约束、`token_version` 自增、并发冲突）只有在真库上才验得到。

## 用例为什么长这样（两条纪律）

1. **每条用例自己造账号、自己清理**（`temp_accounts` 装置）：账号表是**共享**的，
   留下行会改变后续用例看到的「启用管理员数量」—— 那正是自我保护规则的判据，
   于是会出现「单独跑绿、全量跑红」这种最难查的顺序依赖。
2. **不测耗时**：防枚举的性质用「未知标识也调用了 `verify_password`」来钉，
   而不是去量毫秒。时间断言在共享 CI 机器上必然不稳（这就是一条 flaky 用例的配方）。
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import select

from app.db.models import Account
from app.db.session import SessionLocal
from app.security import accounts as svc
from app.security.rbac import PERM_ADMIN, has_permission

pytestmark = pytest.mark.db

#: 临时账号的标识前缀：清理时只删自己造的行（**绝不 `TRUNCATE`**，
#: 那会把种子管理员一起删掉，而别的用例正依赖它）
TEMP_PREFIX = "zz-test-"


def _subject_id_pool(subject_type: str) -> list[int]:
    """该类型**实际存在、且当前还没有账号**的主体 id，升序。

    ⚠️ 两个「不能想当然」的地方，都是实测踩出来的：

    1. **不能硬编码 id**：种子数据里客户有 8 个、员工只有 **3 个**
       （`scripts/db_check.MIN_COUNTS`）。第一版照抄客户的口径写成 `emp_id=7`，
       5 条用例直接红在「主体不存在」。
    2. **不能只看「本用例借过哪些」**：库里可能已经有**演示账号**占着某些主体
       （`scripts/seed.py` 的 `sync_demo_accounts` 会在跑 `tests/api` 时补齐演示账号）。
       第一版从池头取，于是全量跑时这些用例红、单独跑时绿 —— 典型的顺序依赖。
       判据只有一个：**这个主体现在有没有账号**。
    """
    from app.db.models import Employee, User
    from app.security.accounts import SUBJECT_CUSTOMER

    with SessionLocal() as db:
        taken = {
            int(row[1])
            for row in db.execute(
                select(Account.subject_type, Account.subject_id).where(
                    Account.subject_type == subject_type
                )
            ).all()
        }
        if subject_type == SUBJECT_CUSTOMER:
            pool = [int(v) for v in db.execute(select(User.user_id).order_by(User.user_id)).scalars()]
        else:
            pool = [
                int(v) for v in db.execute(select(Employee.emp_id).order_by(Employee.emp_id)).scalars()
            ]
    return [subject_id for subject_id in pool if subject_id not in taken]


class _Ledger:
    """记录本次用例造了哪些账号，供清理用；并分发**本用例内不重复**的主体 id。"""

    def __init__(self) -> None:
        self.account_ids: list[int] = []
        self._pools: dict[str, list[int]] = {}
        self._used: dict[str, set[int]] = {}

    def grant(self, account_id: int) -> None:
        self.account_ids.append(int(account_id))

    def next_subject(self, subject_type: str) -> int:
        """取一个本用例内**还没被借走**的主体 id。

        两条必要的纪律（都是实测踩出来的）：

        1. **本用例内不复用**：`_make` 会连续借主体（目标 + 操作者），
           若两次拿到同一个 id，第二次 `create_account` 会直接抛「已经有账号」——
           而这个异常看起来像「服务坏了」，实际是用例自己撞了自己。
        2. **每个用例从池头重新开始**：`temp_accounts` 在用例结束时把本用例造的账号
           全删掉，所以池头永远是可用的。这样「借到哪个 id」只取决于本用例内的调用顺序，
           与其他用例跑了什么无关（第一版用 `itertools.cycle` 且**第二个主体留给调用方
           决定时也会推进循环**，于是失败点随测试顺序漂移 —— 那是最难查的一类脆弱）。
        """
        pool = self._pools.get(subject_type)
        if pool is None:
            pool = _subject_id_pool(subject_type)
            if not pool:
                pytest.skip(f"{subject_type} 没有任何主体可用：先跑 `uv run python -m scripts.seed`")
            self._pools[subject_type] = pool
        used = self._used.setdefault(subject_type, set())
        for candidate in pool:
            if candidate not in used:
                used.add(candidate)
                return int(candidate)
        pytest.skip(f"{subject_type} 的可用主体都被本用例占满了（种子太小或主体被其他账号占用）")


@pytest.fixture
def temp_accounts(seeded):
    """需要种子数据（业务主体必须存在）+ 用完清理自己造的账号。"""
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


def _make(
    ledger: _Ledger,
    *,
    identifier: str,
    password: str = "pw-for-tests",
    role: str = "customer",
    subject_type: str = svc.SUBJECT_CUSTOMER,
    subject_id: int | None = None,
    status: str = svc.STATUS_ACTIVE,
) -> svc.AccountView:
    """建一个临时账号并登记到账本。`subject_id` 缺省时按角色自动取一个可用的。"""
    with SessionLocal() as db:
        view = svc.create_account(
            db,
            identifier=identifier,
            password=password,
            role=role,
            subject_type=subject_type,
            subject_id=ledger.next_subject(subject_type) if subject_id is None else subject_id,
            status=status,
        )
        db.commit()
    ledger.grant(view.account_id)
    return view


# ── 纯逻辑层 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("role", "entry"),
    [
        ("admin", svc.ENTRY_ADMIN),
        ("internal_employee", svc.ENTRY_INTERNAL),
        ("manager", svc.ENTRY_INTERNAL),
        ("finance_auditor", svc.ENTRY_INTERNAL),
        ("customer", svc.ENTRY_CUSTOMER),
        ("customer_service", svc.ENTRY_CUSTOMER),
        # 未登记角色 = 零权限 → 只能进对客入口（**默认拒绝**的体现）
        ("nobody", svc.ENTRY_CUSTOMER),
    ],
)
def test_entry_for_role(role, entry):
    """入口分流按**能力**判据，不按角色名硬编码（新增角色时入口自动正确）。"""
    assert svc.entry_for_role(role) == entry


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  ZhangSan ", "zhangsan"), ("13800000001", "13800000001"), ("", ""), (None, "")],
)
def test_normalize_identifier(raw, expected):
    """标识归一化：去空白 + 转小写（否则「Zhangsan 登不上」是必然的困惑）。"""
    assert svc.normalize_identifier(raw) == expected


def test_account_view_has_no_credential_fields():
    """对外形态**不许**带 `password_hash` / `token_version`。

    这条是「少一个字段就少一处要审的出口」这句承诺的机器判据：
    将来有人为了图方便把 `password_hash` 加回 `AccountView`，这里会红。
    """
    fields = set(svc.AccountView.__dataclass_fields__)
    assert "password_hash" not in fields
    assert "token_version" not in fields


# ── 真库层：建号校验 ─────────────────────────────────────────────────────────


# ── 口令强度（issue 003 的 P1-3）────────────────────────────────────────────


@pytest.mark.parametrize(
    "password",
    [
        "",  # 空
        "x",  # 1 位（修复前**能建出可登录的账号**）
        "short",  # 5 位
        "12345678901",  # 11 位（差一位）
        "   ",  # 全空白
        "password",  # 常见口令
        "shopsage1234",  # 常见口令（项目名）
    ],
)
def test_validate_password_strength_rejects_weak(temp_accounts, password: str) -> None:
    """★弱口令必须被拒（P1-3：修复前 `min_length=1`，实测 `"x"` 能建号并登录）。"""
    with pytest.raises(svc.AccountError):
        svc.validate_password_strength(password, identifier=f"{TEMP_PREFIX}weak")


def test_validate_password_strength_rejects_identifier_echo() -> None:
    """口令与登录标识相同 → 拒（那是「猜三次就能进」的形态）。"""
    with pytest.raises(svc.AccountError, match="不能与登录标识相同"):
        # 标识与口令都够长，才会走到「与标识相同」这条规则（而不是先被长度拦下）
        svc.validate_password_strength("employee-zhang", identifier="employee-zhang")


def test_validate_password_strength_accepts_a_reasonable_one() -> None:
    """长度够、不常见、与标识不同 → 放行（修复不能把正常口令也挡掉）。"""
    svc.validate_password_strength("a-long-enough-passphrase", identifier="someone")


def test_create_account_rejects_short_password(temp_accounts) -> None:
    """★真库路径也要拦：脚本/后台任务直接调 `create_account` 不走 pydantic。"""
    with SessionLocal() as db:
        with pytest.raises(svc.AccountError, match="至少"):
            svc.create_account(
                db,
                identifier=f"{TEMP_PREFIX}weakpw",
                password="x",
                role="customer",
                subject_type=svc.SUBJECT_CUSTOMER,
                subject_id=temp_accounts.next_subject(svc.SUBJECT_CUSTOMER),
            )


def test_create_account_rejects_unregistered_role(temp_accounts):
    """未登记角色建不出账号：它按默认拒绝是零权限，建出来是个「点不动」的账号。"""
    with SessionLocal() as db:
        with pytest.raises(svc.AccountError, match="未登记的角色"):
            svc.create_account(
                db,
                identifier=f"{TEMP_PREFIX}role",
                password="pw-for-tests",
                role="not-a-role",
                subject_type=svc.SUBJECT_CUSTOMER,
                subject_id=temp_accounts.next_subject(svc.SUBJECT_CUSTOMER),
            )


def test_create_account_rejects_missing_subject(temp_accounts):
    """`subject_id` 指向不存在的客户 → 拦住。

    这条补偿的是**没有外键**这个硬伤（`accounts.subject_id` 指向两张表之一，
    PostgreSQL 不支持这种外键）。不拦的后果：账号能登录，但对客路径按一个不存在的
    user_id 过滤，永远查不到东西，而错误信息只会说「你没有订单」。
    """
    with SessionLocal() as db:
        with pytest.raises(svc.AccountError, match="主体不存在"):
            svc.create_account(
                db,
                identifier=f"{TEMP_PREFIX}ghost",
                password="pw-for-tests",
                role="customer",
                subject_type=svc.SUBJECT_CUSTOMER,
                subject_id=999_999,
            )


def test_create_account_rejects_duplicate_handle_and_subject(temp_accounts):
    """标识唯一 + 主体唯一（一个主体最多一个账号）—— 两条约束各测一次。"""
    first = _make(temp_accounts, identifier=f"{TEMP_PREFIX}dup")
    with SessionLocal() as db:
        record = svc.get_account(db, first.account_id)

        # ① 标识重复：换一个主体，仍被标识的唯一约束拦下
        with pytest.raises(svc.AccountError, match="已被占用"):
            svc.create_account(
                db,
                identifier=f"{TEMP_PREFIX}dup",
                password="pw-for-tests",
                role="customer",
                subject_type=svc.SUBJECT_EMPLOYEE,
                subject_id=temp_accounts.next_subject(svc.SUBJECT_EMPLOYEE),
            )

        # ② 主体重复：用 `first` **真正占用的那个主体**，换个标识来撞
        with pytest.raises(svc.AccountError, match="已经有账号"):
            svc.create_account(
                db,
                identifier=f"{TEMP_PREFIX}same-subject",
                password="pw-for-tests",
                role="customer",
                subject_type=record.subject_type,
                subject_id=record.subject_id,
            )


def test_identifier_is_stored_normalized(temp_accounts):
    """库里存的是归一化后的标识，所以登录时大小写不敏感。"""
    view = _make(temp_accounts, identifier=f"  {TEMP_PREFIX}MiXeD  ")
    assert view.identifier == f"{TEMP_PREFIX}mixed".lower()


# ── 真库层：认证 ─────────────────────────────────────────────────────────────


def test_authenticate_success_and_failure(temp_accounts):
    view = _make(temp_accounts, identifier=f"{TEMP_PREFIX}auth", password="correct-horse-battery")
    with SessionLocal() as db:
        record = svc.authenticate(db, f"{TEMP_PREFIX}auth", "correct-horse-battery")
        db.commit()
        assert record.account_id == view.account_id
        assert record.is_active
        assert record.last_login_at is not None, "登录成功要写 last_login_at"

        with pytest.raises(svc.AccountAuthError):
            svc.authenticate(db, f"{TEMP_PREFIX}auth", "wrong")
        with pytest.raises(svc.AccountAuthError):
            svc.authenticate(db, f"{TEMP_PREFIX}nosuch", "whatever")


def test_authenticate_is_case_insensitive_on_identifier(temp_accounts):
    _make(temp_accounts, identifier=f"{TEMP_PREFIX}case", password="pw-123456789")
    with SessionLocal() as db:
        assert svc.authenticate(db, f"{TEMP_PREFIX.upper()}CASE", "pw-123456789").identifier


def test_last_login_does_not_bump_token_version(temp_accounts):
    """⚠️ `last_login_at` 是纯统计字段，**绝不能**自增 `token_version`。

    自增的后果是「用户登录成功、拿到令牌，然后立刻 401」—— 因为那条令牌的 `ver`
    在签发的同时就被这次登录自己作废了。这是本模块最隐蔽的一个坑，
    所以它有一条专门的用例。
    """
    _make(temp_accounts, identifier=f"{TEMP_PREFIX}ver", password="pw-123456789")
    with SessionLocal() as db:
        before = svc.get_account_by_identifier(db, f"{TEMP_PREFIX}ver").token_version
        svc.authenticate(db, f"{TEMP_PREFIX}ver", "pw-123456789")
        db.commit()
        after = svc.get_account_by_identifier(db, f"{TEMP_PREFIX}ver").token_version
    assert after == before


def test_unknown_identifier_still_pays_the_hash_cost(temp_accounts, monkeypatch):
    """**防枚举的机器判据**：未知标识也要走一次 `verify_password`。

    不做的话，「账号不存在」会比「口令错误」快一个数量级（少一次 Argon2），
    攻击者据此可以枚举出全部有效标识，再对它们集中爆破。

    ⚠️ 这里断言的是「调用了」，不是「耗时多少」：量毫秒的断言在共享 CI 上必然 flaky。
    """
    calls: list[str] = []
    original = svc.verify_password

    def _spy(password: str, stored: str) -> bool:
        calls.append(stored)
        return original(password, stored)

    monkeypatch.setattr(svc, "verify_password", _spy)
    with SessionLocal() as db:
        with pytest.raises(svc.AccountAuthError):
            svc.authenticate(db, f"{TEMP_PREFIX}definitely-missing", "whatever")
    assert calls, "未知标识没有走口令校验 → 耗时不同 → 可以被用来枚举账号"
    assert calls[0] == svc._DUMMY_HASH


def test_disabled_account_is_rejected_after_password_check(temp_accounts):
    """停用账号报「已停用」，但**在口令校验之后** —— 否则它成了一条不需要口令的探测路径。"""
    _make(
        temp_accounts,
        identifier=f"{TEMP_PREFIX}off",
        password="pw-123456789",
        status=svc.STATUS_DISABLED,
    )
    with SessionLocal() as db:
        with pytest.raises(svc.AccountAuthError, match="停用"):
            svc.authenticate(db, f"{TEMP_PREFIX}off", "pw-123456789")
        # 口令错时仍然只报「不正确」，不泄露状态
        with pytest.raises(svc.AccountAuthError, match="不正确"):
            svc.authenticate(db, f"{TEMP_PREFIX}off", "wrong")


def test_legacy_hash_is_upgraded_in_place_on_login(temp_accounts):
    """旧 `sha256$` 口令：**仍可登录**，并在登录成功时原地升级为 Argon2id。

    升级会自增 `token_version`（那是「影响鉴权的变更」），所以这里同时钉住那一半。
    """
    from app.security.auth import LEGACY_PASSWORD_SCHEME, PASSWORD_SCHEME

    view = _make(temp_accounts, identifier=f"{TEMP_PREFIX}legacy", password="ignored-for-tests")
    legacy = svc.hash_password("legacy-pw", scheme=LEGACY_PASSWORD_SCHEME)
    with SessionLocal() as db:
        row = db.get(Account, view.account_id)
        row.password_hash = legacy
        before = int(row.token_version)
        db.commit()

    with SessionLocal() as db:
        record = svc.authenticate(db, f"{TEMP_PREFIX}legacy", "legacy-pw")
        db.commit()
        assert record.password_hash.startswith(PASSWORD_SCHEME)
        assert record.token_version == before + 1, "升级散列属于「影响鉴权的变更」，必须作废旧令牌"

    with SessionLocal() as db:
        assert svc.authenticate(db, f"{TEMP_PREFIX}legacy", "legacy-pw").account_id == view.account_id


# ── 真库层：生命周期与自我保护 ───────────────────────────────────────────────


def test_role_change_bumps_token_version(temp_accounts):
    """改角色要自增 `token_version`：把该账号所有在途令牌一次性作废。

    ⚠️ 构造上的坑（**实测踩了两次**）：操作者不能是库里唯一启用的管理员，
    否则「把目标降级」会先被自我保护规则拦下（那是在防运维事故，不是判授权），
    于是这条用例看起来像「自增没生效」，而真相是用例前提不成立。
    这里把目标也造成管理员：目标从 admin 降成 manager 之后，至少还有操作者一个启用管理员。
    """
    view = _make(temp_accounts, identifier=f"{TEMP_PREFIX}bump-role", role="admin")
    actor = _make(
        temp_accounts,
        identifier=f"{TEMP_PREFIX}bump-role-actor",
        role="admin",
        subject_type=svc.SUBJECT_EMPLOYEE,
    )
    with SessionLocal() as db:
        before = svc.get_account(db, view.account_id).token_version
        svc.update_account(db, view.account_id, actor_account_id=actor.account_id, role="manager")
        db.commit()
        after = svc.get_account(db, view.account_id)
    assert after.role == "manager"
    assert after.token_version == before + 1


def test_status_change_bumps_token_version(temp_accounts):
    """停用要自增 `token_version`（同一个道理：库里的行改了，在途令牌不会自己失效）。

    目标用**非管理员**，于是「停用」不会碰到自我保护规则 —— 这条用例只测自增这一件事。
    """
    view = _make(temp_accounts, identifier=f"{TEMP_PREFIX}bump-status", role="customer")
    actor = _make(
        temp_accounts,
        identifier=f"{TEMP_PREFIX}bump-status-actor",
        role="admin",
        subject_type=svc.SUBJECT_EMPLOYEE,
    )
    with SessionLocal() as db:
        before = svc.get_account(db, view.account_id).token_version
        svc.update_account(
            db, view.account_id, actor_account_id=actor.account_id, status=svc.STATUS_DISABLED
        )
        db.commit()
        after = svc.get_account(db, view.account_id)
    assert after.status == svc.STATUS_DISABLED
    assert after.token_version == before + 1


def test_disabling_a_non_admin_is_allowed_even_when_only_one_admin_left(temp_accounts):
    """**回归用例**：只剩一个启用管理员时，停用**一个客户**必须照常放行。

    第一版守卫写成 `(losing_admin or being_disabled) and admins <= 1` —— 于是库里只剩
    一个管理员时，**任何账号**都停用不了，报错还说「不能停用最后一个启用的管理员」，
    而被拦的那个根本不是管理员（实测现场：`current_role='customer'`、`admins=1`）。

    判据要连在「这次操作真的会让**管理员数**减少」上，而不是连在「有人被停用」上。

    ⚠️ 构造上要先**把库里既有的管理员暂时停用**（演示种子里有一个 admin）：
    否则「只剩一个管理员」这个前提不成立，用例就在测别的东西了。用完恢复。
    """
    with _ambient_admins_disabled():
        actor = _make(
            temp_accounts,
            identifier=f"{TEMP_PREFIX}only-admin",
            role="admin",
            subject_type=svc.SUBJECT_EMPLOYEE,
        )
        victim = _make(temp_accounts, identifier=f"{TEMP_PREFIX}victim", role="customer")
        with SessionLocal() as db:
            # 前提：库里只有一个启用管理员（就是 actor）
            assert svc.count_active_admins(db) == 1
            svc.update_account(
                db, victim.account_id, actor_account_id=actor.account_id, status=svc.STATUS_DISABLED
            )
            db.commit()
            assert svc.get_account(db, victim.account_id).status == svc.STATUS_DISABLED


@contextmanager
def _ambient_admins_disabled():
    """把库里**既有的启用管理员暂时停用**，用完恢复（`finally` 保证）。

    为什么需要它：演示种子会灌一个 admin（`scripts/seed.py::DEMO_ACCOUNTS`），
    于是「库里只剩一个启用管理员」这类前提在共享库上**不成立**。
    第一版直接断言 `count_active_admins(db) == 1`，单独跑绿、全量跑红 ——
    因为全量跑时 `tests/api` 的装置先补了演示账号（顺序依赖，最难查的一类）。

    刻意**不改 `token_version`**：这里只是把环境调成用例需要的样子，
    不是一次真实的停用动作（改了会顺带作废别的用例刚拿到的令牌）。
    """
    from app.db.models import Account

    with SessionLocal() as db:
        saved: list[int] = []
        for row in db.execute(select(Account).where(Account.status == svc.STATUS_ACTIVE)).scalars():
            from app.security.rbac import has_permission

            if has_permission(str(row.role), PERM_ADMIN):
                saved.append(int(row.account_id))
                row.status = svc.STATUS_DISABLED
        db.commit()
    try:
        yield saved
    finally:
        with SessionLocal() as db:
            for account_id in saved:
                row = db.get(Account, account_id)
                if row is not None:
                    row.status = svc.STATUS_ACTIVE
            db.commit()


def test_noop_update_does_not_bump_token_version(temp_accounts):
    """**没变就不许自增**：否则每次「保存」都会把用户踢下线。"""
    view = _make(temp_accounts, identifier=f"{TEMP_PREFIX}noop", role="customer")
    actor = _make(
        temp_accounts,
        identifier=f"{TEMP_PREFIX}actor2",
        role="admin",
        subject_type=svc.SUBJECT_EMPLOYEE,
    )
    with SessionLocal() as db:
        before = svc.get_account(db, view.account_id).token_version
        svc.update_account(db, view.account_id, actor_account_id=actor.account_id, role="customer")
        db.commit()
        assert svc.get_account(db, view.account_id).token_version == before


def test_cannot_disable_or_demote_self(temp_accounts):
    """不能停用或降级自己（一次误点就把自己关在门外）。"""
    actor = _make(
        temp_accounts,
        identifier=f"{TEMP_PREFIX}self",
        role="admin",
        subject_type=svc.SUBJECT_EMPLOYEE,
    )
    with SessionLocal() as db:
        with pytest.raises(svc.AccountPermissionError, match="自己"):
            svc.update_account(
                db, actor.account_id, actor_account_id=actor.account_id, status=svc.STATUS_DISABLED
            )
        with pytest.raises(svc.AccountPermissionError, match="自己"):
            svc.update_account(
                db, actor.account_id, actor_account_id=actor.account_id, role="customer"
            )


def test_cannot_disable_last_active_admin(temp_accounts):
    """**防的是运维事故，不是授权判断**：两个管理员互相停用同样会锁死系统。

    构造法：先把库里既有的启用管理员暂时停用（演示种子有一个 admin），
    把「启用管理员」收缩到本用例造的这两个，于是「停第二个」必然触到
    `count_active_admins() <= 1` 这条线。用完由 `_ambient_admins_disabled` 的
    `finally` 恢复 —— 用例中途失败也不会把演示库留在「没有管理员」的状态。
    """
    with _ambient_admins_disabled():
        # ⚠️ 造账号要放在**停用之后**：`_ambient_admins_disabled` 会把库里既有的
        # 启用管理员都停掉，若先造账号、后停用，新造的这两个会被一起停掉（用例随即失去意义）。
        first = _make(
            temp_accounts,
            identifier=f"{TEMP_PREFIX}admin-a",
            role="admin",
            subject_type=svc.SUBJECT_EMPLOYEE,
        )
        second = _make(
            temp_accounts,
            identifier=f"{TEMP_PREFIX}admin-b",
            role="admin",
            subject_type=svc.SUBJECT_CUSTOMER,
        )
        with SessionLocal() as db:
            assert svc.count_active_admins(db) == 2
            # 停掉一个 → 只剩一个启用管理员
            svc.update_account(
                db, first.account_id, actor_account_id=second.account_id, status=svc.STATUS_DISABLED
            )
            db.commit()
            assert svc.count_active_admins(db) == 1
            # 停最后一个 → 拒绝
            with pytest.raises(svc.AccountPermissionError, match="最后一个启用的管理员"):
                svc.update_account(
                    db,
                    second.account_id,
                    actor_account_id=first.account_id,
                    status=svc.STATUS_DISABLED,
                )
            # 把最后一个降级成非管理员 → 同样拒绝
            with pytest.raises(svc.AccountPermissionError, match="最后一个启用的管理员"):
                svc.update_account(
                    db, second.account_id, actor_account_id=first.account_id, role="customer"
                )


def test_reset_and_change_password(temp_accounts):
    """重置（管理员，不需要旧口令）与本人改口令（需要旧口令）都要作废在途令牌。"""
    view = _make(temp_accounts, identifier=f"{TEMP_PREFIX}pw", password="old-pw-1234567")
    with SessionLocal() as db:
        before = svc.get_account(db, view.account_id).token_version
        svc.reset_password(db, view.account_id, "reset-pw-1234567")
        db.commit()
        assert svc.get_account(db, view.account_id).token_version == before + 1

    with SessionLocal() as db:
        # 旧口令已失效、新口令可用
        with pytest.raises(svc.AccountAuthError):
            svc.authenticate(db, f"{TEMP_PREFIX}pw", "old-pw-1234567")
        svc.authenticate(db, f"{TEMP_PREFIX}pw", "reset-pw-1234567")
        db.commit()

    with SessionLocal() as db:
        with pytest.raises(svc.AccountAuthError, match="当前口令"):
            svc.change_own_password(
                db, view.account_id, current_password="wrong-password-1", new_password="next-pw-1234567"
            )
    with SessionLocal() as db:
        before = svc.get_account(db, view.account_id).token_version
        svc.change_own_password(
            db, view.account_id, current_password="reset-pw-1234567", new_password="next-pw-1234567"
        )
        db.commit()
        assert svc.get_account(db, view.account_id).token_version == before + 1


def test_password_is_never_stored_in_plaintext(temp_accounts):
    """库里存的必须是散列，且**不可能是明文**（这条防线在真库上再验一次）。"""
    _make(temp_accounts, identifier=f"{TEMP_PREFIX}plain", password="plaintext-pw-xyz-123")
    with SessionLocal() as db:
        record = svc.get_account_by_identifier(db, f"{TEMP_PREFIX}plain")
    assert "plaintext-pw-xyz" not in record.password_hash
    assert record.password_hash.startswith("argon2$")


def test_list_and_reconciliation_helpers(temp_accounts):
    """列表与「谁还没开账号」的对账口径。"""
    view = _make(temp_accounts, identifier=f"{TEMP_PREFIX}list", role="customer")
    with SessionLocal() as db:
        listed = svc.list_accounts(db, status=svc.STATUS_ACTIVE, limit=500)
        assert any(item.identifier == f"{TEMP_PREFIX}list" for item in listed)
        assert all(isinstance(item, svc.AccountView) for item in listed)

        # 本用例刚借用了一个客户主体 → 它不该出现在「还没开号」里
        record = svc.get_account(db, view.account_id)
        missing = svc.unaccounted_subjects(db)
        assert record.subject_id not in missing[svc.SUBJECT_CUSTOMER]
        assert isinstance(svc.accounts_total(db), int)


def test_entry_for_admin_account_is_admin(temp_accounts):
    """管理员账号的 `entry` 必须是 `admin`（前端据此渲染入口，服务端是唯一真相）。"""
    view = _make(
        temp_accounts,
        identifier=f"{TEMP_PREFIX}entry",
        role="admin",
        subject_type=svc.SUBJECT_EMPLOYEE,
    )
    with SessionLocal() as db:
        record = svc.get_account(db, view.account_id)
    assert record.entry == svc.ENTRY_ADMIN
    assert record.is_admin is True
    # ⚠️ 用**权限点**确认，而不是看角色名里有没有 "admin"：
    # `has_permission` 才是授权真相（角色名只是键）。
    assert has_permission(record.role, PERM_ADMIN)
