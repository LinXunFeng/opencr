"""新增重试输入快照与来源运行关联

Revision ID: 64db99aae2b1
Revises: a83e6e396079
Create Date: 2026-09-09 11:24:11.327014
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '64db99aae2b1'
down_revision: Union[str, None] = 'a83e6e396079'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """增加可空字段，旧运行不伪造历史范围。"""
    with op.batch_alter_table('review_run', schema=None) as batch_op:
        batch_op.add_column(sa.Column('review_input', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('retry_of_uid', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('retry_scope', sa.String(length=16), nullable=True))



def downgrade() -> None:
    """移除重试元数据，保留原有运行与审查发现。"""
    with op.batch_alter_table('review_run', schema=None) as batch_op:
        batch_op.drop_column('retry_scope')
        batch_op.drop_column('retry_of_uid')
        batch_op.drop_column('review_input')
