"""Append-only submitted and confirmed brief history."""
from alembic import op
import sqlalchemy as sa

revision = "a41e349d12ac"
down_revision = "f973196b6714"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("brief_revisions", sa.Column("id", sa.String(96), primary_key=True), sa.Column("run_id", sa.String(96), sa.ForeignKey("runs.id"), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("kind", sa.String(32), nullable=False), sa.Column("payload", sa.JSON(), nullable=False), sa.Column("command_id", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("run_id", "version", name="uq_brief_revision"))
    op.create_index("ix_brief_revisions_run_id", "brief_revisions", ["run_id"])


def downgrade():
    op.drop_index("ix_brief_revisions_run_id", table_name="brief_revisions")
    op.drop_table("brief_revisions")
