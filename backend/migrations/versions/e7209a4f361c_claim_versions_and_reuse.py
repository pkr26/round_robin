"""Persist immutable claims and exact selected parent reuse.

Revision ID: e7209a4f361c
Revises: d1260b7f94de
"""
from alembic import op
import sqlalchemy as sa

revision = "e7209a4f361c"
down_revision = "d1260b7f94de"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("claim_versions",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(96), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("meeting_id", sa.String(96), sa.ForeignKey("meetings.id"), nullable=False),
        sa.Column("owner_id", sa.String(96), nullable=False),
        sa.Column("step_id", sa.String(96), sa.ForeignKey("steps.id")),
        sa.Column("alias", sa.String(32)), sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "step_id", "alias", name="uq_step_claim_alias"))
    op.create_table("reuse_records",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("run_id", sa.String(96), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("meeting_id", sa.String(96), sa.ForeignKey("meetings.id"), nullable=False),
        sa.Column("owner_id", sa.String(96), nullable=False),
        sa.Column("parent_run_id", sa.String(96), nullable=False),
        sa.Column("parent_id", sa.String(96), nullable=False),
        sa.Column("parent_version", sa.Integer(), nullable=False),
        sa.Column("parent_sha256", sa.String(64), nullable=False),
        sa.Column("child_id", sa.String(96), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "parent_id", name="uq_run_parent_reuse"))
    for table in ("claim_versions", "reuse_records"):
        for column in ("run_id", "meeting_id", "owner_id"):
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    op.drop_table("reuse_records")
    op.drop_table("claim_versions")
