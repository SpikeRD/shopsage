"""add accounts table

变更 001 的唯一一次结构变更：账号表 `accounts`（身份基础设施，**不是**业务表）。

三条设计口径（与 `app/db/models.Account` 的注释一致）：

1. **「凭据 → 身份」只有一处真相**。新增账号表而不是给 `users` / `employees` 各加口令列，
   理由是后者会让登录分叉成「先查客户、再查员工」两条路径 —— 而两处真相的典型故障
   （客户侧加了口令校验、员工侧忘了加）不会有任何报错。详见变更单 §3.1 与
   `models.Account` 的 docstring。
2. **`subject_id` 不能声明外键**：PostgreSQL 不支持「指向多张表之一」的外键，
   `customer → users.user_id` 与 `employee → employees.emp_id` 只能靠应用层校验。
   这是本表唯一的硬伤，换来的收益是不引入第三种身份类别（管理员就是
   `employees` 里 `role='admin'` 的一行，于是审计外键天然可用）。
3. **`token_version` 是「立即失效」的落点**：停用/改密/改角色时 `+1`，所有在途令牌
   （access 与 refresh 都带 `ver` 声明）在下一次鉴权时立刻失效。
   只查 `status` 挡得住登录，但挡不住已发出的 refresh 令牌继续换 access。

⚠️ **`accounts` 刻意不进 `app/security/policy.ALLOWED_TABLES`**（给 LLM 自由 SQL 用的表白名单）：
`password_hash` 绝不能被文本转 SQL 读到。有测试断言这一点（变更单 §5 风险 2）。

⚠️ 这一版**不做数据回填**：本表是新增的，老数据里不存在等价记录
   （变更 001 之前根本没有账号概念，登录靠 `scripts/dev_token.py` 现场签发）。
   `downgrade()` 只删表 —— 它带走的只有「已开通的账号」，也就是**所有人会立刻无法登录**，
   回滚前应先确认 `scripts/dev_token.py` 仍可用（它不依赖本表）。

Revision ID: f3b8c41d9e27
Revises: b3c7f1a92d40
Create Date: 2026-10-15 10:12:44.318902

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f3b8c41d9e27"
down_revision: Union[str, Sequence[str], None] = "b3c7f1a92d40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "accounts",
        sa.Column("account_id", sa.BigInteger(), autoincrement=True, nullable=False),
        # 登录标识：员工填用户名，客户填手机号。唯一约束 = 「一个标识只能有一个账号」，
        # 它同时是登录查询的索引（唯一约束自带 B-tree），所以不再单独建索引。
        sa.Column("identifier", sa.String(length=64), nullable=False),
        # 自描述散列：`argon2$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>`，
        # 兼容旧值 `sha256$<salt>$<digest>`（登录成功后原地升级）。
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        # 授权角色：取值必须是 `rbac.ROLE_PERMISSIONS` 的键（未登记角色 → 零权限，默认拒绝）。
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("token_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name=op.f("ck_accounts_status_valid")
        ),
        sa.CheckConstraint(
            "subject_type IN ('customer', 'employee')",
            name=op.f("ck_accounts_subject_type_valid"),
        ),
        sa.CheckConstraint("token_version >= 0", name=op.f("ck_accounts_token_version_non_negative")),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_accounts")),
        sa.UniqueConstraint("identifier", name=op.f("uq_accounts_identifier")),
        # 一个业务主体最多一个账号：防止「同一个客户两条凭据」这种谁都不知道哪条有效的状态
        sa.UniqueConstraint(
            "subject_type", "subject_id", name=op.f("uq_accounts_subject_type_subject_id")
        ),
    )
    # 角色的查询路径（管理后台「按角色筛账号」）。
    # ⚠️ 这里**不建** `(subject_type, subject_id)` 索引：上面那条唯一约束已经提供了
    # 同一个 B-tree，重复建是浪费写入。模型里显式声明那个 Index 是为了让
    # `compare_metadata` 的口径一致，见 `models.Account.__table_args__` 的注释。
    op.create_index("ix_accounts_role", "accounts", ["role"], unique=False)
    # 「最近开的号」是管理后台的默认排序（与 `created_at` 上的 index=True 对应）
    op.create_index("ix_accounts_created_at", "accounts", ["created_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_accounts_created_at", table_name="accounts")
    op.drop_index("ix_accounts_role", table_name="accounts")
    op.drop_table("accounts")
