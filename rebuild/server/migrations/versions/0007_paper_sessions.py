"""paper sessions

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03 12:00:00
"""
from alembic import op
import sqlalchemy as sa
import tdserver.models


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('paper_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('strategy_id', sa.Integer(), nullable=True),
    sa.Column('strategy_version', sa.Integer(), nullable=True),
    sa.Column('preset_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('underlying', sa.String(length=16), nullable=False),
    sa.Column('spec', sa.JSON(), nullable=False),
    sa.Column('spec_hash', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('first_day', sa.Date(), nullable=False),
    sa.Column('start_minute', sa.Integer(), nullable=False),
    sa.Column('last_day', sa.Date(), nullable=True),
    sa.Column('rules', sa.JSON(), nullable=False),
    sa.Column('actions', sa.JSON(), nullable=False),
    sa.Column('state', sa.JSON(), nullable=False),
    sa.Column('alerted', sa.JSON(), nullable=False),
    sa.Column('created_at', tdserver.models.UTCDateTime(timezone=True), nullable=False),
    sa.Column('updated_at', tdserver.models.UTCDateTime(timezone=True), nullable=False),
    sa.Column('ended_at', tdserver.models.UTCDateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], name='fk_paper_strategy'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('paper_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_paper_sessions_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_paper_sessions_strategy_id'), ['strategy_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_paper_sessions_status'), ['status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('paper_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_paper_sessions_status'))
        batch_op.drop_index(batch_op.f('ix_paper_sessions_strategy_id'))
        batch_op.drop_index(batch_op.f('ix_paper_sessions_user_id'))
    op.drop_table('paper_sessions')
