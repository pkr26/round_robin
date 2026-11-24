"""Optional immutable visual sessions, versions, reviews and operations.

Revision ID: c6917df240ab
Revises: a41e349d12ac
"""
from alembic import op
import sqlalchemy as sa

revision = "c6917df240ab"
down_revision = "a41e349d12ac"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("design_sessions", sa.Column("id", sa.String(96), primary_key=True), sa.Column("meeting_id", sa.String(96), sa.ForeignKey("meetings.id"), nullable=False), sa.Column("owner_id", sa.String(96), nullable=False), sa.Column("mode", sa.String(24), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("revision", sa.Integer(), nullable=False), sa.Column("execution_fence", sa.Integer(), nullable=False), sa.Column("deployment_epoch", sa.Integer(), nullable=False), sa.Column("frozen_brief", sa.JSON(), nullable=False), sa.Column("recipients", sa.JSON(), nullable=False), sa.Column("maximum_usd", sa.String(64), nullable=False), sa.Column("state", sa.JSON(), nullable=False), sa.Column("preflight", sa.JSON(), nullable=True), sa.Column("selected_version_id", sa.String(96), nullable=True), sa.Column("selection_recorded", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_design_sessions_meeting_id", "design_sessions", ["meeting_id"])
    op.create_index("ix_design_sessions_owner_id", "design_sessions", ["owner_id"])
    op.create_table("design_versions", sa.Column("id", sa.String(96), primary_key=True), sa.Column("session_id", sa.String(96), sa.ForeignKey("design_sessions.id"), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("parent_version_id", sa.String(96), nullable=True), sa.Column("canonical_hash", sa.String(64), nullable=False), sa.Column("original_hash", sa.String(64), nullable=False), sa.Column("asset_path", sa.Text(), nullable=False), sa.Column("width", sa.Integer(), nullable=False), sa.Column("height", sa.Integer(), nullable=False), sa.Column("generator_provider", sa.String(32), nullable=True), sa.Column("source_kind", sa.String(24), nullable=False), sa.Column("provenance", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("session_id", "version", name="uq_design_version"))
    op.create_index("ix_design_versions_session_id", "design_versions", ["session_id"])
    op.create_table("design_reviews", sa.Column("id", sa.String(96), primary_key=True), sa.Column("session_id", sa.String(96), sa.ForeignKey("design_sessions.id"), nullable=False), sa.Column("version_id", sa.String(96), sa.ForeignKey("design_versions.id"), nullable=False), sa.Column("provider", sa.String(32), nullable=False), sa.Column("receipt", sa.JSON(), nullable=False), sa.Column("critique", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("version_id", "provider", name="uq_design_critic"))
    op.create_index("ix_design_reviews_session_id", "design_reviews", ["session_id"])
    op.create_index("ix_design_reviews_version_id", "design_reviews", ["version_id"])
    op.create_table("design_operations", sa.Column("id", sa.String(96), primary_key=True), sa.Column("session_id", sa.String(96), sa.ForeignKey("design_sessions.id"), nullable=False), sa.Column("provider", sa.String(32), nullable=False), sa.Column("payload", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_design_operations_session_id", "design_operations", ["session_id"])


def downgrade():
    op.drop_table("design_operations")
    op.drop_table("design_reviews")
    op.drop_table("design_versions")
    op.drop_table("design_sessions")
