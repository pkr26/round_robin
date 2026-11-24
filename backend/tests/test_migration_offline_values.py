"""Offline migrations must produce executable SQL without bind parameters."""

import os
from io import StringIO
from pathlib import Path
from shutil import copyfile

from alembic import command
from alembic.config import Config
from round_robin.database.safety import require_disposable_postgres


def test_offline_migration_inlines_data_values(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    script_location = tmp_path / "migrations"
    versions = script_location / "versions"
    versions.mkdir(parents=True)
    copyfile(root / "backend/migrations/env.py", script_location / "env.py")
    (versions / "synthetic.py").write_text(
        '''from alembic import op
import sqlalchemy as sa
revision = "synthetic"
down_revision = None
branch_labels = None
depends_on = None
def upgrade():
    op.execute(sa.table("example", sa.column("value", sa.String)).insert().values(value="literal-sentinel"))
def downgrade():
    pass
'''
    )
    configured = os.environ.get("ROUND_ROBIN_TEST_DATABASE_URL", "postgresql+psycopg://127.0.0.1:1/offline_test")
    url = require_disposable_postgres(configured).render_as_string(hide_password=False)
    output = StringIO()
    config = Config(output_buffer=output)
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    monkeypatch.delenv("ROUND_ROBIN_DATABASE_URL", raising=False)

    command.upgrade(config, "head", sql=True)

    assert "'literal-sentinel'" in output.getvalue()
