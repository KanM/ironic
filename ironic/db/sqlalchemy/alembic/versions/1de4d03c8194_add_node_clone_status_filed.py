"""add node.clone_status filed

Revision ID: 1de4d03c8194
Revises: 48d6c242bb9b
Create Date: 2016-02-19 07:10:06.350639

"""

# revision identifiers, used by Alembic.
revision = '1de4d03c8194'
down_revision = '48d6c242bb9b'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('nodes',
                  sa.Column('clone_state', sa.String(length=15),
                  nullable=True))
    op.add_column('nodes',
                  sa.Column('target_clone_state', sa.String(length=15),
                  nullable=True))
    op.add_column('nodes',
                  sa.Column('clone_step', sa.Text(),
                  nullable=True))
    op.add_column('nodes',
                  sa.Column('clone_time', sa.DateTime(),
                  nullable=True))

def downgrade():
    op.drop_column('nodes', 'clone_state')
    op.drop_column('nodes', 'target_clone_state')
    op.drop_column('nodes', 'clone_step')
    op.drop_column('nodes', 'clone_time')
