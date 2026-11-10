"""trade journal

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-04 12:00:00
"""
from alembic import op
import sqlalchemy as sa
import tdserver.models


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('journal_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=8), nullable=False),
    sa.Column('source_key', sa.String(length=80), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('underlying', sa.String(length=16), nullable=False),
    sa.Column('entry_at', sa.String(length=19), nullable=False),
    sa.Column('exit_at', sa.String(length=19), nullable=False),
    sa.Column('net', sa.Float(), nullable=False),
    sa.Column('gross', sa.Float(), nullable=False),
    sa.Column('charges', sa.Float(), nullable=False),
    sa.Column('exit_reason', sa.String(length=40), nullable=True),
    sa.Column('legs', sa.JSON(), nullable=False),
    sa.Column('setup', sa.String(length=40), nullable=True),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('rating', sa.Integer(), nullable=True),
    sa.Column('created_at', tdserver.models.UTCDateTime(timezone=True), nullable=False),
    sa.Column('updated_at', tdserver.models.UTCDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'source', 'source_key', name='uq_journal_source')
    )
    with op.batch_alter_table('journal_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_journal_entries_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_journal_entries_setup'), ['setup'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('journal_entries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_journal_entries_setup'))
        batch_op.drop_index(batch_op.f('ix_journal_entries_user_id'))
    op.drop_table('journal_entries')
