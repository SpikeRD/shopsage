"""add queue dead letters table

第 12 步（queue）的唯一一次结构变更：死信表 `queue_dead_letters` + 两个索引。

三条设计口径（与 `app/db/models.QueueDeadLetter` 的注释一致）：

1. **只有失败的任务落库，成功的任务不落**。成功的任务每天可能几十万条，
   而它的价值只有「结果」（已经在 Redis 里，TTL 24h，且已经回给调用方）。
   失败的任务必须留下来 —— 它是「零任务丢失」这条验收的落点，也是人工兜底的入口，
   而「人工」意味着**人能筛、能查、能标记已处理**，Redis 的字符串结果做不到。
2. **`job_id` 唯一**：同一个任务重复失败只更新现场，不新增行。重放也用这个键做幂等，
   所以「同一条死信被点两次」不会跑两个任务（A2）。
3. **`payload` / `policy` 用 JSONB**：payload 要能被**原样喂回**任务（重放的输入），
   policy 要能回答「当时用的是哪套退避」（策略改了之后回看旧记录仍然说得通）。
   用 JSONB 而不是 Text 的额外好处是能按字段查、写入时即校验。

⚠️ 这一版**刻意不加**数据迁移（没有 UPDATE 老行、没有回填）：本表是新增的，
   老数据里没有任何等价的记录（第 12 步之前队列根本不存在）。
   `downgrade()` 只删表 —— 它带走的只有「还没人处理完的失败现场」，
   回滚前应当先 `scripts/queue_replay.py --list` 看一眼。

Revision ID: 7164220823a3
Revises: a1f0c3d5b7e9
Create Date: 2026-10-11 19:21:36.457125

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7164220823a3'
down_revision: Union[str, Sequence[str], None] = 'a1f0c3d5b7e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('queue_dead_letters',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('job_id', sa.String(length=96), nullable=False),
    sa.Column('function', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='1', nullable=False),
    sa.Column('error_type', sa.String(length=64), server_default='', nullable=False),
    sa.Column('error_message', sa.Text(), server_default='', nullable=False),
    sa.Column('error_traceback', sa.Text(), server_default='', nullable=False),
    sa.Column('retryable', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('policy', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('enqueued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=True),
    sa.Column('replay_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('replayed_job_id', sa.String(length=96), server_default='', nullable=False),
    sa.Column('resolution', sa.String(length=32), server_default='', nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('report', 'agent', 'kb_rebuild')", name=op.f('ck_queue_dead_letters_kind_valid')),
    sa.CheckConstraint('attempts >= 1', name=op.f('ck_queue_dead_letters_attempts_positive')),
    sa.CheckConstraint('replay_count >= 0', name=op.f('ck_queue_dead_letters_replay_count_non_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_queue_dead_letters')),
    sa.UniqueConstraint('job_id', name=op.f('uq_queue_dead_letters_job_id'))
    )
    # 排障的主查询：「最近还有哪些没处理」→ (resolution, failed_at)；
    # 第二个索引给「按任务种类看失败面」用（`kind` 的基数极低，前缀选择性差，
    # 所以它总是与 failed_at 一起用）。
    op.create_index('ix_queue_dead_letters_kind_failed_at', 'queue_dead_letters', ['kind', 'failed_at'], unique=False)
    op.create_index('ix_queue_dead_letters_resolution_failed_at', 'queue_dead_letters', ['resolution', 'failed_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_queue_dead_letters_resolution_failed_at', table_name='queue_dead_letters')
    op.drop_index('ix_queue_dead_letters_kind_failed_at', table_name='queue_dead_letters')
    op.drop_table('queue_dead_letters')
