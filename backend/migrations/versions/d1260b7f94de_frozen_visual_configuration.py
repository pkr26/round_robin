"""Freeze nonsecret operator visual configuration with each design.

Revision ID: d1260b7f94de
Revises: c6917df240ab
"""
from alembic import op
import sqlalchemy as sa

revision = "d1260b7f94de"
down_revision = "c6917df240ab"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("design_sessions", sa.Column("configuration", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade():
    op.drop_column("design_sessions", "configuration")
