"""live sessions and commands

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-03 13:00:00
"""
from alembic import op
import sqlalchemy as sa
import tdserver.models


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('live_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('strategy_id', sa.Integer(), nullable=True),
    sa.Column('strategy_version', sa.Integer(), nullable=True),
    sa.Column('paper_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('underlying', sa.String(length=16), nullable=False),
    sa.Column('spec', sa.JSON(), nullable=False),
    sa.Column('spec_hash', sa.String(length=16), nullable=False),
    sa.Column('mode', sa.String(length=8), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('armed_day', sa.Date(), nullable=True),
    sa.Column('armed_device', sa.Integer(), nullable=True),
    sa.Column('first_day', sa.Date(), nullable=False),
    sa.Column('start_minute', sa.Integer(), nullable=False),
    sa.Column('last_day', sa.Date(), nullable=True),
    sa.Column('state', sa.JSON(), nullable=False),
    sa.Column('created_at', tdserver.models.UTCDateTime(timezone=True), nullable=False),
    sa.Column('updated_at', tdserver.models.UTCDateTime(timezone=True), nullable=False),
    sa.Column('ended_at', tdserver.models.UTCDateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], name='fk_live_strategy'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('live_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_live_sessions_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_live_sessions_status'), ['status'], unique=False)
    op.create_table('live_commands',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('mode', sa.String(length=8), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('result', sa.String(length=300), nullable=False),
    sa.Column('tries', sa.Integer(), nullable=False),
    sa.Column('created_at', tdserver.models.UTCDateTime(timezone=True), nullable=False),
    sa.Column('done_at', tdserver.models.UTCDateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('live_commands', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_live_commands_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_live_commands_status'), ['status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('live_commands', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_live_commands_status'))
        batch_op.drop_index(batch_op.f('ix_live_commands_user_id'))
    op.drop_table('live_commands')
    with op.batch_alter_table('live_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_live_sessions_status'))
        batch_op.drop_index(batch_op.f('ix_live_sessions_user_id'))
    op.drop_table('live_sessions')
