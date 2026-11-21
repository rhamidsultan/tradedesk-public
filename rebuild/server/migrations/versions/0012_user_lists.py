"""user lists (each login's order for templates, Home cards, index tiles, paper sessions)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-21 23:40:00
"""
from alembic import op
import sqlalchemy as sa
import tdserver.models


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('user_lists',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.Column('ids', sa.JSON(), nullable=False),
    sa.Column('updated_at', tdserver.models.UTCDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'name')
    )


def downgrade() -> None:
    op.drop_table('user_lists')
