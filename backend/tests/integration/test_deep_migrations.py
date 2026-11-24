"""Exercise every migration upgrade/downgrade in a disposable PostgreSQL schema."""
import os
from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import create_engine, inspect, text

from round_robin.database.safety import require_disposable_postgres
from round_robin.operations.recovery import alembic_config, schema_head


def test_all_migrations_round_trip_without_schema_drift():
    configured = os.environ.get("ROUND_ROBIN_TEST_DATABASE_URL")
    if not configured or os.environ.get("ROUND_ROBIN_TEST_ALLOW_LOOPBACK") != "1":
        pytest.skip("Migration rollback requires the explicit disposable PostgreSQL profile")
    url = require_disposable_postgres(configured)
    admin = create_engine(url, hide_parameters=True)
    schema = "rr_migration_audit_" + uuid4().hex
    engine = None
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        scoped = url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
        config = alembic_config(scoped)
        command.upgrade(config, "head")
        command.check(config)
        engine = create_engine(scoped, hide_parameters=True)
        expected = set(inspect(engine).get_table_names())
        assert {"owners", "runs", "brief_revisions", "design_versions", "design_sessions"} <= expected
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == schema_head()
        command.downgrade(config, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        command.upgrade(config, "head")
        command.check(config)
        assert set(inspect(engine).get_table_names()) == expected
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
