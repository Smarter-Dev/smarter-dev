"""Add BlogPost model for website blog feature

Revision ID: 530f4ea0d3d6
Revises: a76854db18af
Create Date: 2025-08-08 07:41:18.207989

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '530f4ea0d3d6'
down_revision = 'a76854db18af'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create blog_posts table
    op.create_table('blog_posts',
    sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('slug', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('author', sa.String(length=100), nullable=False),
    sa.Column('is_published', sa.Boolean(), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug', name='uq_blog_posts_slug')
    )
    op.create_index('ix_blog_posts_author', 'blog_posts', ['author'], unique=False)
    op.create_index('ix_blog_posts_created_at', 'blog_posts', ['created_at'], unique=False)
    op.create_index('ix_blog_posts_published', 'blog_posts', ['is_published', 'published_at'], unique=False)
    op.create_index('ix_blog_posts_slug', 'blog_posts', ['slug'], unique=False)


def downgrade() -> None:
    # Drop indexes and table
    op.drop_index('ix_blog_posts_slug', table_name='blog_posts')
    op.drop_index('ix_blog_posts_published', table_name='blog_posts')
    op.drop_index('ix_blog_posts_created_at', table_name='blog_posts')
    op.drop_index('ix_blog_posts_author', table_name='blog_posts')
    op.drop_table('blog_posts')