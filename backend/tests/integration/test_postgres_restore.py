"""Synthetic full PostgreSQL backup/restore, with isolated generated databases."""
from dataclasses import replace
from datetime import timedelta
import os
from uuid import uuid4

from alembic import command
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import BudgetOperation, Command, Draft, Meeting, PolicyBucket, Run, utcnow
from round_robin.database.safety import require_disposable_postgres
from round_robin.fixtures import sample_brief
from round_robin.operations.recovery import alembic_config, backup, restore_verify
from round_robin.orchestration.budget import operations_exposure


def test_actual_postgresql_restore_remains_quarantined_and_reapplies_postbackup_deletion(tmp_path):
    configured = os.environ.get("ROUND_ROBIN_TEST_DATABASE_URL")
    if not configured:
        pytest.skip("Full PostgreSQL restore requires the explicit disposable loopback test profile")
    try:
        url = require_disposable_postgres(configured)
    except ValueError:
        pytest.fail("Refusing to create restore fixtures outside the local test PostgreSQL service")
    identifier = uuid4().hex[:20]
    names = [f"rr_source_{identifier}_test", f"rr_{identifier}_restore_test"]
    administrator = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    created = []
    source_application = None
    target_engine = None
    try:
        with administrator.connect() as connection:
            for name in names:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
                created.append(name)
        source_url = url.set(database=names[0]).render_as_string(hide_password=False)
        target_url = url.set(database=names[1]).render_as_string(hide_password=False)
        command.upgrade(alembic_config(source_url), "head")
        settings = replace(Settings(), artifact_directory=tmp_path / "source_artifacts")
        source_application = create_app(source_url, settings=settings)
        with TestClient(source_application) as client:
            meeting = client.post("/api/meetings", json={"command_id": str(uuid4()), "brief": sample_brief().model_dump(mode="json")}).json()
            quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json={"command_id": str(uuid4()), "expected_draft_revision": 1}).json()
            start = {"command_id": str(uuid4()), "preflight_id": quote["id"], "expected_draft_revision": 1}
            run = client.post(f"/api/meetings/{meeting['id']}/runs", json=start).json()
            with source_application.state.session_factory.begin() as session:
                session.add(BudgetOperation(id="pg_restore_unknown", run_id=run["id"], owner_id="owner_local", kind="model", status="reserved", reserved_usd="0.60", settled_usd=None, authorized_at=utcnow() - timedelta(days=35), metadata_json={"model_call": True}))
            destination = tmp_path / "pg_backup"
            manifest = settings.artifact_directory.parent / "deletion-manifest.jsonl"
            backup(source_application.state.engine, settings.artifact_directory, destination, manifest)
            assert client.request("DELETE", f"/api/meetings/{meeting['id']}", json={"command_id": str(uuid4())}).status_code == 200
        result = restore_verify(destination, target_url, tmp_path / "restored_artifacts", manifest)
        assert result["status"] == "restored_quarantined"
        assert result["provider_requests"] == 0
        target_engine = create_engine(target_url, hide_parameters=True)
        with sessionmaker(bind=target_engine)() as session:
            assert session.get(Draft, meeting["id"]) is None
            assert session.get(Meeting, meeting["id"]).deleted_at is not None
            assert session.get(Command, ("owner_local", start["command_id"])).gone is True
            operation = session.get(BudgetOperation, "pg_restore_unknown")
            assert operation.status == "unknown"
            assert str(operations_exposure([operation], period="daily")) == "0.60"
            policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
            assert policy.spending_locked
            assert policy.metadata_json["restore_quarantine"]
            assert policy.metadata_json["deployment_epoch"] > session.get(Run, run["id"]).deployment_epoch
    finally:
        if source_application:
            source_application.state.engine.dispose()
        if target_engine:
            target_engine.dispose()
        with administrator.connect() as connection:
            for name in created:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        administrator.dispose()
