"""add refund decision columns and status index

变更 003 的唯一一次结构变更：给 `refunds` 加**审批留痕**四列 + 一条复合索引。

四条设计口径（与 `app/db/models.Refund` 的注释一致）：

1. **四列全部 nullable，且不做数据回填**。历史行（本次迁移之前建的单）**没有**审批记录，
   给它们一个默认值等于伪造「有人批过」。NULL 的语义就是「还没人批 / 不知道谁批的」。
   这与变更 001 的 `accounts` 表（新表、无老数据）不同：这次是给**已有行**加列，
   所以「不回填」是一个刻意的决定，不是遗漏。

2. **`decided_by` 指向 `employees.emp_id`，`ON DELETE SET NULL`**。
   与 `tickets.assignee_id` / `stock_movements.operator_id` 同一口径（追到「哪个人」）。
   用 SET NULL 而不是 CASCADE：员工离职删行**不该把审批记录一起删掉** ——
   那会让一笔资金动作的审批人凭空消失，审计上等于这笔审批没发生过。
   ⚠️ 注意与 `accounts.subject_id` 的区别：那里**不能**声明外键（它指向两张表之一），
   这里可以（审批人必然是员工）。

3. **`approver_role` 是快照，不是外键**。角色可以事后被改，审计要看的是
   「他当时是什么角色」。存成文本快照，一次改角色不会让历史审批记录的解释跟着变。

4. **`ix_refunds_status_created_at`** 覆盖员工审批台的默认查询
   （「按状态筛 + 按时间倒序」），与 `ix_orders_status_created_at` 同一手法。
   没有它时 `refunds` 会走顺序扫描 —— 现在是演示库的几十行，但这是**查询形状**问题，
   不是数据量问题，索引与查询同批加才不会被忘记。

⚠️ `downgrade()` 会**丢掉审批留痕**（四列的数据）。它是可逆的（列被删掉、
其余数据完好），但「谁批过哪一笔」这件事**不可恢复**。
回滚前若要留档，先把 `decided_by / decided_at / decision_note / approver_role`
导出一份。

Revision ID: c7a4e2f9b1d8
Revises: f3b8c41d9e27
Create Date: 2026-10-16 11:20:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c7a4e2f9b1d8"
down_revision: Union[str, Sequence[str], None] = "f3b8c41d9e27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("refunds", sa.Column("decided_by", sa.BigInteger(), nullable=True))
    op.add_column(
        "refunds",
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("refunds", sa.Column("decision_note", sa.String(length=255), nullable=True))
    op.add_column("refunds", sa.Column("approver_role", sa.String(length=32), nullable=True))
    # 外键单独建：与 models.Refund 的 ForeignKey(..., ondelete="SET NULL") 逐项对应。
    # 名字显式给出（不靠命名约定）—— 约定变了之后 downgrade 会找不到它。
    op.create_foreign_key(
        "fk_refunds_decided_by_employees",
        "refunds",
        "employees",
        ["decided_by"],
        ["emp_id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_refunds_status_created_at", "refunds", ["status", "created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_refunds_status_created_at", table_name="refunds")
    op.drop_constraint("fk_refunds_decided_by_employees", "refunds", type_="foreignkey")
    op.drop_column("refunds", "approver_role")
    op.drop_column("refunds", "decision_note")
    op.drop_column("refunds", "decided_at")
    op.drop_column("refunds", "decided_by")
