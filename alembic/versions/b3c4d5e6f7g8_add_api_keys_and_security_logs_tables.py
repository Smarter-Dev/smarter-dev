"""Add api_keys and security_logs tables

These tables were included in the squashed initial migration but were
never created in databases that predated the squash.

Revision ID: b3c4d5e6f7g8
Revises: a1b2c3d4e5f6
Create Date: 2026-03-07

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3c4d5e6f7g8'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create api_keys table (if not exists for safety)
    op.create_table('api_keys',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('key_prefix', sa.String(length=12), nullable=False),
        sa.Column('scopes', sa.JSON(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('usage_count', sa.Integer(), nullable=False),
        sa.Column('rate_limit_per_second', sa.Integer(), nullable=False),
        sa.Column('rate_limit_per_minute', sa.Integer(), nullable=False),
        sa.Column('rate_limit_per_15_minutes', sa.Integer(), nullable=False),
        sa.Column('rate_limit_per_hour', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_api_keys')),
        sa.UniqueConstraint('key_hash', name='uq_api_keys_hash'),
        sa.UniqueConstraint('key_hash', name=op.f('uq_api_keys_key_hash')),
    )
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.create_index('ix_api_keys_active', ['is_active'], unique=False, postgresql_where='is_active = true')
        batch_op.create_index('ix_api_keys_created_by', ['created_by'], unique=False)
        batch_op.create_index('ix_api_keys_hash', ['key_hash'], unique=False)
        batch_op.create_index('ix_api_keys_prefix', ['key_prefix'], unique=False)

    # Create security_logs table (has FK to api_keys)
    op.create_table('security_logs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('action', sa.String(length=100), nullable=False),
        sa.Column('success', sa.Boolean(), nullable=False),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.String(length=500), nullable=True),
        sa.Column('user_identifier', sa.String(length=100), nullable=True),
        sa.Column('api_key_id', sa.UUID(), nullable=True),
        sa.Column('request_id', sa.String(length=100), nullable=True),
        sa.Column('resource', sa.String(length=200), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['api_key_id'], ['api_keys.id'], name=op.f('fk_security_logs_api_key_id_api_keys'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_security_logs')),
    )
    with op.batch_alter_table('security_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_security_logs_action'), ['action'], unique=False)
        batch_op.create_index('ix_security_logs_action_timestamp', ['action', 'timestamp'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_logs_api_key_id'), ['api_key_id'], unique=False)
        batch_op.create_index('ix_security_logs_api_key_timestamp', ['api_key_id', 'timestamp'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_logs_ip_address'), ['ip_address'], unique=False)
        batch_op.create_index('ix_security_logs_ip_timestamp', ['ip_address', 'timestamp'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_logs_request_id'), ['request_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_logs_success'), ['success'], unique=False)
        batch_op.create_index('ix_security_logs_success_timestamp', ['success', 'timestamp'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_logs_timestamp'), ['timestamp'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_logs_user_identifier'), ['user_identifier'], unique=False)
        batch_op.create_index('ix_security_logs_user_timestamp', ['user_identifier', 'timestamp'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('security_logs', schema=None) as batch_op:
        batch_op.drop_index('ix_security_logs_user_timestamp')
        batch_op.drop_index(batch_op.f('ix_security_logs_user_identifier'))
        batch_op.drop_index(batch_op.f('ix_security_logs_timestamp'))
        batch_op.drop_index('ix_security_logs_success_timestamp')
        batch_op.drop_index(batch_op.f('ix_security_logs_success'))
        batch_op.drop_index(batch_op.f('ix_security_logs_request_id'))
        batch_op.drop_index('ix_security_logs_ip_timestamp')
        batch_op.drop_index(batch_op.f('ix_security_logs_ip_address'))
        batch_op.drop_index('ix_security_logs_api_key_timestamp')
        batch_op.drop_index(batch_op.f('ix_security_logs_api_key_id'))
        batch_op.drop_index('ix_security_logs_action_timestamp')
        batch_op.drop_index(batch_op.f('ix_security_logs_action'))
    op.drop_table('security_logs')

    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index('ix_api_keys_prefix')
        batch_op.drop_index('ix_api_keys_hash')
        batch_op.drop_index('ix_api_keys_created_by')
        batch_op.drop_index('ix_api_keys_active', postgresql_where='is_active = true')
    op.drop_table('api_keys')
