"""Migration logging must not expose bound database values."""

import logging
import os
from pathlib import Path
from shutil import copyfile
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from round_robin.database.safety import require_disposable_postgres
from sqlalchemy import create_engine, text


def test_online_migration_hides_bound_parameter_values(tmp_path, caplog):
    configured = os.environ.get("ROUND_ROBIN_TEST_DATABASE_URL")
    if not configured or os.environ.get("ROUND_ROBIN_TEST_ALLOW_LOOPBACK") != "1":
        pytest.skip("Migration log check requires the explicit disposable PostgreSQL profile")
    url = require_disposable_postgres(configured)
    admin = create_engine(url, hide_parameters=True)
    schema = "rr_migration_privacy_" + uuid4().hex
    script_location = tmp_path / "migrations"
    versions = script_location / "versions"
    versions.mkdir(parents=True)
    copyfile(Path(__file__).resolve().parents[3] / "backend/migrations/env.py", script_location / "env.py")
    (versions / "synthetic.py").write_text(
        '''from alembic import op
import sqlalchemy as sa
revision = "synthetic"
down_revision = None
branch_labels = None
depends_on = None
def upgrade():
    table = op.create_table("secret_probe", sa.Column("value", sa.String(), nullable=False))
    op.execute(table.insert().values(value="SYNTHETIC_PRIVATE_PARAMETER"))
def downgrade():
    op.drop_table("secret_probe")
'''
    )
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        scoped = url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
        config = Config()
        config.set_main_option("script_location", str(script_location))
        config.attributes["url_override"] = scoped
        caplog.set_level(logging.INFO, logger="sqlalchemy.engine.Engine")

        command.upgrade(config, "head")

        assert "SQL parameters hidden due to hide_parameters=True" in caplog.text
        assert "SYNTHETIC_PRIVATE_PARAMETER" not in caplog.text
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
