"""账号服务：登录认证与账号生命周期（变更 001，决策 1/3/4/5/9）。

本模块是**「凭据 → 身份」的唯一真相**（`app/db/models.Account` 的 docstring 写了为什么
要单独一张表）。对外只暴露「意图级」的函数，不暴露 ORM 会话，调用方（路由、脚本）
拿到的都是 `AccountRecord` / `AccountView` 这两个不可变数据。

## 四个必须弄懂的设计口径

### ① 防枚举：未知账号与错口令必须**不可区分**

`authenticate()` 在「账号不存在」这条路径上**也要跑一次等价耗时的散列计算**
（对 `_DUMMY_HASH` 验一次），否则响应耗时会泄露「这个账号存不存在」——
攻击者据此可以枚举出全部有效标识，再对它们集中爆破（那比盲猜高效得多）。

同时对外只抛一个 `AccountAuthError`，**文案里不区分**两种原因。这不是「信息少一点更好」，
而是「少给的那个信息恰好是攻击者最需要的」。

### ② 口令散列只在登录成功的那一刻**原地升级**（决策 4）

旧的 `sha256$...` 值仍然可验。验过之后 `needs_rehash()` 会告诉我们要不要重写——
**登录成功是唯一能拿到明文口令的时机**，错过它就只能要求用户重置口令了。
升级会 `token_version + 1`（理由见 ③）。

### ③ 任何影响鉴权的变更都要 `token_version + 1`

改密、改角色、停用**都要自增**。为什么：令牌是无状态的，光改库里的行不会让已经发出去的
令牌失效；而 `app/api/deps.py::require_principal` 会比对令牌里的 `ver` 与库里的值 ——
自增就是「把这个账号的所有在途令牌一次性作废」。

⚠️ 反过来说：**`last_login_at` 这种纯统计字段绝不能自增它**，否则每次登录都会把用户
自己刚拿到的令牌作废（症状是「登录成功但立刻 401」，非常难查）。

### ④ 管理员不能把自己锁在门外

`update_account()` 里有一条硬约束：**不能把最后一个启用的管理员**停用或降级成非管理员。
只防「不能停用自己」是不够的——两个管理员互相停用同样会把系统锁死，
而那是一个**运维事故**，不是一次授权判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ACCOUNT_STATUSES, Account
from app.security.auth import (
    DEFAULT_TOKEN_VERSION,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.security.rbac import (
    PERM_ADMIN,
    PERM_READ_ALL_ORDERS,
    ROLE_PERMISSIONS,
    has_permission,
)

# ── 取值常量（与 `app/db/models.py` 的 CHECK 约束同源）───────────────────────

STATUS_ACTIVE: Final[str] = "active"
STATUS_DISABLED: Final[str] = "disabled"

SUBJECT_CUSTOMER: Final[str] = "customer"
SUBJECT_EMPLOYEE: Final[str] = "employee"

# ── 入口（entry）──────────────────────────────────────────────────────────────
# 服务端算好入口并随登录响应返回，前端不复制这套分流规则。
# 判据全部来自 `rbac` 的**能力**（权限点），不按角色名硬编码 ——
# 这样 `ROLE_PERMISSIONS` 新增一个角色时，入口自动就对了。
ENTRY_ADMIN: Final[str] = "admin"
ENTRY_INTERNAL: Final[str] = "internal"
ENTRY_CUSTOMER: Final[str] = "customer"

#: 新账号的初始世代号。用 `auth.DEFAULT_TOKEN_VERSION`（=0），不另起一个值：
#: 两处各写一个「初始值」迟早会漂移成「有的账号从 0 开始、有的从 1 开始」。
INITIAL_TOKEN_VERSION: Final[int] = DEFAULT_TOKEN_VERSION


class AccountError(Exception):
    """账号操作的通用失败（参数不对、主体不存在、状态冲突）。调用方映射成 4xx。"""


class AccountAuthError(Exception):
    """**认证**失败。只表示「这组凭据不对」，不区分账号不存在与口令错误（防枚举，见模块 ①）。"""


class AccountPermissionError(AccountError):
    """操作被规则拒绝（例如「不能停用最后一个管理员」）。调用方映射成 403/422。"""


#: 防枚举用的假存储值：形状与真值完全一致（argon2id、同样参数），
#: 所以「验它」与「验一个真值」耗时相当。口令本身无意义。
_DUMMY_HASH: Final[str] = hash_password("dummy-password-for-timing-equalization")


@dataclass(frozen=True)
class AccountRecord:
    """账号的**完整**行（含口令散列）。

    ⚠️ **绝不把这个对象交给路由层去序列化**：它带着 `password_hash`。
    对外的出口是 `AccountView`（`as_view()`），那条路连字段都没有。
    """

    account_id: int
    identifier: str
    password_hash: str
    role: str
    status: str
    subject_type: str
    subject_id: int
    token_version: int
    created_at: datetime | None = None
    last_login_at: datetime | None = None

    @property
    def subject(self) -> str:
        """主体标识串（`c7` / `e7`）——`thread_id` 派生的依据。"""
        from app.security.auth import subject_for

        return subject_for(self.subject_type, self.subject_id)

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def is_admin(self) -> bool:
        return has_permission(self.role, PERM_ADMIN)

    @property
    def entry(self) -> str:
        """登录后该进哪个界面（**服务端唯一的入口分流真相**）。"""
        return entry_for_role(self.role)

    def signing_identity(self) -> Any:
        """转成 `auth.SigningIdentity`（签发令牌用）。"""
        from app.security.auth import SigningIdentity

        return SigningIdentity(
            subject_type=self.subject_type,
            subject_id=self.subject_id,
            role=self.role,
            account_id=self.account_id,
            token_version=self.token_version,
        )

    def as_view(self) -> AccountView:
        return AccountView(
            account_id=self.account_id,
            identifier=self.identifier,
            role=self.role,
            status=self.status,
            subject_type=self.subject_type,
            subject_id=self.subject_id,
            created_at=self.created_at,
            last_login_at=self.last_login_at,
        )


@dataclass(frozen=True)
class AccountView:
    """账号的**对外**形态：**没有** `password_hash`、**没有** `token_version`。

    为什么连 `token_version` 都不给：它是「令牌作废计数」，对外没有任何用途，
    而暴露它等于告诉攻击者「这个账号最近被改过几次」。少一个字段就少一处要审的出口。
    """

    account_id: int
    identifier: str
    role: str
    status: str
    subject_type: str
    subject_id: int
    created_at: datetime | None = None
    last_login_at: datetime | None = None

    @property
    def is_admin(self) -> bool:
        return has_permission(self.role, PERM_ADMIN)


def entry_for_role(role: str) -> str:
    """角色 → 入口。判据是**能力**（权限点），不是角色名（见模块常量区的注释）。"""
    if has_permission(role, PERM_ADMIN):
        return ENTRY_ADMIN
    if has_permission(role, PERM_READ_ALL_ORDERS):
        return ENTRY_INTERNAL
    return ENTRY_CUSTOMER


def _to_record(row: Account) -> AccountRecord:
    return AccountRecord(
        account_id=int(row.account_id),
        identifier=str(row.identifier),
        password_hash=str(row.password_hash),
        role=str(row.role),
        status=str(row.status),
        subject_type=str(row.subject_type),
        subject_id=int(row.subject_id),
        token_version=int(row.token_version),
        created_at=row.created_at,
        last_login_at=row.last_login_at,
    )


# ── 读 ───────────────────────────────────────────────────────────────────────


def normalize_identifier(identifier: str) -> str:
    """登录标识归一化：去空白 + **转小写**。

    为什么统一小写：员工用户名（`zhangsan`）打错大小写是常态，而 PostgreSQL 的
    `VARCHAR` 默认**区分**大小写 —— 不做归一就会出现「Zhangsan 登不上、zhangsan 能登」，
    用户完全看不出原因。手机号不受影响。
    """
    return str(identifier or "").strip().lower()


def get_account(db: Session, account_id: int) -> AccountRecord | None:
    """按主键取账号（**鉴权路径的热函数**：每请求一次索引点查，见变更单 §3.4）。"""
    row = db.execute(select(Account).where(Account.account_id == int(account_id))).scalar_one_or_none()
    return _to_record(row) if row is not None else None


def get_account_by_identifier(db: Session, identifier: str) -> AccountRecord | None:
    row = db.execute(
        select(Account).where(Account.identifier == normalize_identifier(identifier))
    ).scalar_one_or_none()
    return _to_record(row) if row is not None else None


def get_account_for_subject(db: Session, subject_type: str, subject_id: int) -> AccountRecord | None:
    """按业务主体取账号（一个主体最多一个账号，由唯一约束保证）。"""
    row = db.execute(
        select(Account).where(
            Account.subject_type == str(subject_type), Account.subject_id == int(subject_id)
        )
    ).scalar_one_or_none()
    return _to_record(row) if row is not None else None


def list_accounts(
    db: Session,
    *,
    role: str | None = None,
    status: str | None = None,
    subject_type: str | None = None,
    limit: int = 100,
) -> list[AccountView]:
    """列账号（管理后台用）。默认按「最近创建」排——运营最关心的就是这个顺序。"""
    stmt = select(Account).order_by(Account.created_at.desc(), Account.account_id.desc())
    if role:
        stmt = stmt.where(Account.role == role)
    if status:
        stmt = stmt.where(Account.status == status)
    if subject_type:
        stmt = stmt.where(Account.subject_type == subject_type)
    stmt = stmt.limit(max(1, int(limit)))
    return [_to_record(row).as_view() for row in db.execute(stmt).scalars()]


def count_active_admins(db: Session) -> int:
    """当前**启用的管理员**数量（自我保护规则的判据，见模块 ④）。"""
    admins = [
        row
        for row in db.execute(
            select(Account).where(Account.status == STATUS_ACTIVE)
        ).scalars()
    ]
    return sum(1 for row in admins if has_permission(str(row.role), PERM_ADMIN))


# ── 认证 ─────────────────────────────────────────────────────────────────────


def authenticate(db: Session, identifier: str, password: str) -> AccountRecord:
    """用「标识 + 口令」换一个账号记录。失败一律抛 `AccountAuthError`（防枚举，见模块 ①）。

    成功时会做两件副作用（都在调用方的事务里，由调用方 commit）：

    1. 写 `last_login_at`（**不动 `token_version`**，见模块 ③ 的警告）；
    2. 若口令散列需要升级（旧 `sha256$` 或参数落后），**原地重写**并自增 `token_version`。

    ⚠️ 第 2 条的 `token_version + 1` 是**故意的**：它会把该账号此前所有在途令牌作废。
    升级口令散列正是「影响鉴权的变更」，所以它必须与停用/改密同一处理方式。
    """
    normalized = normalize_identifier(identifier)
    if not normalized or not password:
        # ⚠️ 即使这里提前返回，也要跑一次假校验：否则「空标识」会变成一条**更快**的路径，
        # 攻击者用它能区分出「这个标识根本不存在对应的账号记录」。
        verify_password(password or "", _DUMMY_HASH)
        raise AccountAuthError("账号或口令不正确")

    record = get_account_by_identifier(db, normalized)
    if record is None:
        # 防枚举的关键一步：未知账号也付出一次等价耗时的散列验证
        verify_password(password, _DUMMY_HASH)
        raise AccountAuthError("账号或口令不正确")

    if not verify_password(password, record.password_hash):
        raise AccountAuthError("账号或口令不正确")

    row = db.get(Account, record.account_id)
    if row is None:  # pragma: no cover - 上面刚查到，这里的竞态只可能来自并发删除
        raise AccountAuthError("账号或口令不正确")

    # ⚠️ 停用账号：**在口令校验之后**才报「已停用」。反过来（先查状态）会让
    # 「账号存在但被停用」变成一条不需要口令就能探测到的路径。
    if str(row.status) != STATUS_ACTIVE:
        raise AccountAuthError("账号已停用，请联系管理员")

    row.last_login_at = datetime.now(UTC)
    if needs_rehash(str(row.password_hash)):
        row.password_hash = hash_password(password)
        row.token_version = int(row.token_version) + 1
    return _to_record(row)


# ── 写 ───────────────────────────────────────────────────────────────────────


def create_account(
    db: Session,
    *,
    identifier: str,
    password: str,
    role: str,
    subject_type: str,
    subject_id: int,
    status: str = STATUS_ACTIVE,
) -> AccountView:
    """建账号。参数不合法/主体不存在/标识或主体重复 → `AccountError`。

    ## 三条校验，每条都对应一次真实的误操作

    1. **角色必须在 `ROLE_PERMISSIONS` 里**：否则会建出一个「词表里没有的角色」，
       而它按默认拒绝是**零权限**。用户会觉得「账号建好了但什么都点不动」。
    2. **主体必须存在**：`subject_id` 没有外键（见 `models.Account` 的注释），
       所以「指向一个不存在的客户」数据库拦不住 —— 只能在这里拦。
       不拦的后果是：账号能登录，但对客路径按一个不存在的 user_id 过滤，永远查不到东西。
    3. **标识与主体都唯一**：标识有唯一约束；主体有复合唯一约束。
       这里**先查再插**是为了给出可读的错误，最终仍由数据库约束兜底（并发下唯一约束才是真相）。
    """
    if role not in ROLE_PERMISSIONS:
        raise AccountError(
            f"未登记的角色 {role!r}：只接受 {sorted(ROLE_PERMISSIONS)}"
            "（未登记角色按默认拒绝是零权限，建出来也是个点不动的账号）"
        )
    if status not in ACCOUNT_STATUSES:
        raise AccountError(f"非法的账号状态 {status!r}：只接受 {list(ACCOUNT_STATUSES)}")
    if subject_type not in (SUBJECT_CUSTOMER, SUBJECT_EMPLOYEE):
        raise AccountError(
            f"非法的 subject_type {subject_type!r}：只接受 {[SUBJECT_CUSTOMER, SUBJECT_EMPLOYEE]}"
        )
    if not password:
        raise AccountError("口令不能为空")

    normalized = normalize_identifier(identifier)
    if not normalized:
        raise AccountError("登录标识不能为空")
    if get_account_by_identifier(db, normalized) is not None:
        raise AccountError(f"登录标识 {normalized!r} 已被占用")
    if get_account_for_subject(db, subject_type, int(subject_id)) is not None:
        raise AccountError(
            f"主体 {subject_type}#{subject_id} 已经有账号了（一个主体最多一个账号）"
        )
    _assert_subject_exists(db, subject_type, int(subject_id))

    row = Account(
        identifier=normalized,
        password_hash=hash_password(password),
        role=str(role),
        status=str(status),
        subject_type=str(subject_type),
        subject_id=int(subject_id),
        token_version=INITIAL_TOKEN_VERSION,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:  # 并发下的兜底：唯一约束才是真相
        db.rollback()
        raise AccountError("登录标识或主体已被占用（并发创建）") from exc
    return _to_record(row).as_view()


def update_account(
    db: Session,
    account_id: int,
    *,
    actor_account_id: int,
    status: str | None = None,
    role: str | None = None,
) -> AccountView:
    """改状态 / 改角色。**任何实际变化都会 `token_version + 1`**（模块 ③）。

    ## 自我保护规则（模块 ④）

    两条，都必须有：

    - **不能操作自己**：改自己的角色/状态 = 一次误点就把自己关在门外；
    - **不能把最后一个启用的管理员**停用或降级：两个管理员互相停用同样会锁死系统。
      防「自己」是不够的，那只是这条规则的一个特例。

    ⚠️ 「最后一个管理员」的统计包含**目标账号自己**：若目标就是唯一的管理员，
    那么停用/降级之后启用管理员数会变成 0（对自己操作时还要减去它）。
    """
    if status is not None and status not in ACCOUNT_STATUSES:
        raise AccountError(f"非法的账号状态 {status!r}：只接受 {list(ACCOUNT_STATUSES)}")
    if role is not None and role not in ROLE_PERMISSIONS:
        raise AccountError(f"未登记的角色 {role!r}：只接受 {sorted(ROLE_PERMISSIONS)}")

    row = db.get(Account, int(account_id))
    if row is None:
        raise AccountError(f"账号 {account_id} 不存在")

    current_role = str(row.role)
    target_role = str(role) if role is not None else current_role
    target_status = str(status) if status is not None else str(row.status)

    # 「这次操作会不会让系统少掉一个**启用的管理员**」。两种途径：
    #   ① 把管理员降级成非管理员（`losing_admin`）；
    #   ② 把**一个管理员**停用（`target_is_admin` + `being_disabled`）。
    #
    # ⚠️ **第 ② 条必须带 `target_is_admin`**（实测抓到的 bug）：第一版写成
    # `losing_admin or being_disabled`，于是只要库里只剩一个管理员，**任何账号**
    # （哪怕是个客户）都停用不了 —— 报错还说「不能停用最后一个启用的管理员」，
    # 而那个账号根本不是管理员。判据要连在「这次操作真的会让管理员数减少」上，
    # 不能连在「有人被停用」上。
    target_is_admin = has_permission(current_role, PERM_ADMIN)
    losing_admin = target_is_admin and not has_permission(target_role, PERM_ADMIN)
    being_disabled = target_status != STATUS_ACTIVE and str(row.status) == STATUS_ACTIVE
    shrinks_admins = losing_admin or (being_disabled and target_is_admin)

    if int(account_id) == int(actor_account_id):
        # 只拦「会伤到自己」的改动：给自己改个非管理员角色、或把自己停用
        if losing_admin or being_disabled:
            raise AccountPermissionError("不能停用或降级自己的账号（那会把自己关在门外）")

    if shrinks_admins and count_active_admins(db) <= 1:
        raise AccountPermissionError(
            "不能停用或降级最后一个启用的管理员（那会把系统锁死，没人能再开账号）"
        )

    changed = False
    if status is not None and str(row.status) != str(status):
        row.status = str(status)
        changed = True
    if role is not None and str(row.role) != str(role):
        row.role = str(role)
        changed = True
    if changed:
        # 唯一一处自增：只在这里、只在真的改了东西时
        row.token_version = int(row.token_version) + 1
        row.updated_at = datetime.now(UTC)
        db.flush()
    return _to_record(row).as_view()


def reset_password(db: Session, account_id: int, new_password: str) -> AccountView:
    """管理员重置口令。**就地设一个新口令**，自增 `token_version`（作废在途令牌）。

    ## 为什么不做「生成一次性口令并回显」

    一次性口令必然要在响应里**明文出现一次**，而它会落在响应日志、浏览器历史、
    截图、聊天记录里。更糟的是：能在响应里看到口令的人（管理员、以及任何能读日志的人）
    随时能冒用那个账号 —— 而真实业务里「谁重置了谁的口令」往往是要审计的动作，
    不该同时具备「知道新口令」这个能力。

    「管理员把新口令告诉用户」这件事在本仓是**演示场景**的必然（没有邮件/短信通道，
    见变更单 §8 遗留 3），所以本函数不做任何「回显」；调用方（管理后台）由管理员
    自己输入新口令，并对该动作写审计日志。
    """
    if not new_password:
        raise AccountError("新口令不能为空")
    row = db.get(Account, int(account_id))
    if row is None:
        raise AccountError(f"账号 {account_id} 不存在")
    row.password_hash = hash_password(new_password)
    row.token_version = int(row.token_version) + 1
    row.updated_at = datetime.now(UTC)
    db.flush()
    return _to_record(row).as_view()


def change_own_password(
    db: Session, account_id: int, *, current_password: str, new_password: str
) -> AccountView:
    """本人改口令：**必须先验旧口令**，成功后自增 `token_version`。

    为什么不复用 `reset_password`：那条路不需要旧口令（管理员用），
    如果本人改口令也不需要旧口令，那么「偷到一条令牌」就等于「永久占有账号」——
    改口令本来该是**夺回账号**的手段，不能反过来变成攻击者的加固手段。
    """
    if not new_password:
        raise AccountError("新口令不能为空")
    row = db.get(Account, int(account_id))
    if row is None:
        raise AccountError(f"账号 {account_id} 不存在")
    if not verify_password(current_password or "", str(row.password_hash)):
        raise AccountAuthError("当前口令不正确")
    row.password_hash = hash_password(new_password)
    row.token_version = int(row.token_version) + 1
    row.updated_at = datetime.now(UTC)
    db.flush()
    return _to_record(row).as_view()


# ── 内部：主体存在性校验（没有外键，只能在这里拦）──────────────────────────


def _assert_subject_exists(db: Session, subject_type: str, subject_id: int) -> None:
    """校验 `subject_id` 在对应业务表里真的存在。

    ⚠️ 这里 import 模型要在函数内做（`app.db.models` 已经在本模块顶部 import 了，
    所以只是取属性）；写成一个函数是为了让「没有外键」这件事只有一处补偿点。
    """
    from app.db.models import Employee, User

    if subject_type == SUBJECT_CUSTOMER:
        exists = db.execute(select(User.user_id).where(User.user_id == subject_id)).first()
        table = "users.user_id"
    else:
        exists = db.execute(select(Employee.emp_id).where(Employee.emp_id == subject_id)).first()
        table = "employees.emp_id"
    if exists is None:
        raise AccountError(
            f"主体不存在：{table} 里没有 id={subject_id} 的行"
            "（accounts.subject_id 没有外键——它指向两张表之一，PostgreSQL 不支持——"
            "所以这条校验只能由应用层做）"
        )


def subject_exists(db: Session, subject_type: str, subject_id: int) -> bool:
    """主体是否存在（不抛异常版，给「先探测再建号」的调用方用）。"""
    try:
        _assert_subject_exists(db, subject_type, int(subject_id))
        return True
    except AccountError:
        return False


def unaccounted_subjects(db: Session, *, limit: int = 50) -> dict[str, list[int]]:
    """还没开账号的业务主体（对账用：`scripts/seed.py` 与「谁还没号」都靠它）。

    返回 `{"customer": [2, 3], "employee": [1, 4]}`。为什么需要它：
    `accounts.subject_id` 没有外键，所以「有几个客户/员工还没账号」这个统计
    只能由应用层算 —— 而它正是运维最想看的那个数。
    """
    from app.db.models import Employee, User

    taken: dict[str, set[int]] = {SUBJECT_CUSTOMER: set(), SUBJECT_EMPLOYEE: set()}
    for row in db.execute(select(Account.subject_type, Account.subject_id)).all():
        kind = str(row[0])
        if kind in taken:
            taken[kind].add(int(row[1]))

    users = [int(v) for v in db.execute(select(User.user_id).order_by(User.user_id)).scalars()]
    employees = [int(v) for v in db.execute(select(Employee.emp_id).order_by(Employee.emp_id)).scalars()]
    return {
        SUBJECT_CUSTOMER: [v for v in users if v not in taken[SUBJECT_CUSTOMER]][:limit],
        SUBJECT_EMPLOYEE: [v for v in employees if v not in taken[SUBJECT_EMPLOYEE]][:limit],
    }


def accounts_total(db: Session) -> int:
    """账号总数（`scripts/db_check.py` 的现场核查用）。"""
    return int(db.execute(select(func.count()).select_from(Account)).scalar_one())


__all__ = [
    "ENTRY_ADMIN",
    "ENTRY_CUSTOMER",
    "ENTRY_INTERNAL",
    "INITIAL_TOKEN_VERSION",
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "SUBJECT_CUSTOMER",
    "SUBJECT_EMPLOYEE",
    "AccountAuthError",
    "AccountError",
    "AccountPermissionError",
    "AccountRecord",
    "AccountView",
    "accounts_total",
    "authenticate",
    "change_own_password",
    "count_active_admins",
    "create_account",
    "entry_for_role",
    "get_account",
    "get_account_by_identifier",
    "get_account_for_subject",
    "list_accounts",
    "normalize_identifier",
    "reset_password",
    "subject_exists",
    "unaccounted_subjects",
    "update_account",
]
