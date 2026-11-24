"""Exercise operator entry points against disposable records, not paid adapters."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import select, text

from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import BudgetOperation, PolicyBucket
from round_robin.operations.recovery import schema_head
from round_robin import ops


@pytest.fixture
def operator(tmp_path, monkeypatch):
    settings = replace(Settings(), database_url=f"sqlite:///{tmp_path / 'operator.sqlite'}", artifact_directory=tmp_path / "artifacts")
    app = create_app(settings=settings, initialize=True)
    with app.state.engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES (:head)"), {"head": schema_head()})
    monkeypatch.setattr(ops, "Settings", lambda: settings)
    monkeypatch.setattr(ops, "configure_database", lambda url: (app.state.engine, app.state.session_factory))
    monkeypatch.delenv("ROUND_ROBIN_RESTORE_TEST_DATABASE_URL", raising=False)
    yield app, settings
    app.state.engine.dispose()


def execute(capsys, *arguments, status=0):
    assert ops.main(list(arguments)) == status
    return json.loads(capsys.readouterr().out)


def test_operator_checks_and_flags_are_persistent_without_spending(operator, capsys):
    app, settings = operator
    assert execute(capsys, "config-check")["configuration"] == "valid"
    assert execute(capsys, "drain")["draining"] is True
    assert execute(capsys, "drain", "--off")["draining"] is False
    assert execute(capsys, "spend-lock")["spending_locked"] is True
    assert execute(capsys, "usage-inspect")["items"] == []
    assert execute(capsys, "purge-inspect")["items"] == []
    result = execute(capsys, "storage-check")
    assert result["status"] == "storage_checked" and result["provider_requests"] == 0
    with app.state.session_factory() as session:
        deployment = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert deployment.spending_locked and deployment.metadata_json["draining"] is False


def test_operator_conservative_usage_inspection_and_reconciliation(operator, capsys):
    app, _ = operator
    with app.state.session_factory.begin() as session:
        session.add(BudgetOperation(id="operation_cli_unknown", run_id=None, owner_id="owner_local", kind="diagnostic", status="unknown", reserved_usd="0.40", metadata_json={}))
    result = execute(capsys, "usage-reconcile", "operation_cli_unknown", "--conservative-usd", "0.50")
    assert result["status"] == "unknown" and result["uncertainty_released"] is False
    usage = execute(capsys, "usage-inspect")
    assert usage["items"][0]["conservative_booking_usd"] == "0.50"


def test_operator_backup_restore_and_empty_manifest_cleanup(operator, tmp_path, capsys):
    _, settings = operator
    destination = tmp_path / "cli_backup"
    assert execute(capsys, "backup", str(destination))["status"] == "backup_saved"
    assert execute(capsys, "purge-inspect", "--apply")["status"] == "active_storage_purged"
    target = f"sqlite:///{tmp_path / 'restore_test_cli.sqlite'}"
    result = execute(capsys, "restore-verify", str(destination), "--target-url", target, "--target-artifacts", str(tmp_path / "restored"))
    assert result["status"] == "restored_quarantined" and result["provider_requests"] == 0
    assert settings.artifact_directory.parent.joinpath("deletion-manifest.jsonl").exists()


def test_operator_requires_explicit_funding_and_restore_target(operator, tmp_path, capsys):
    assert execute(capsys, "spend-lock", "--unlock", status=1)["status"] == "blocked"
    result = execute(capsys, "restore-verify", str(tmp_path / "missing"), "--target-artifacts", str(tmp_path / "restored"), status=1)
    assert "explicit empty synthetic" in result["reason"]


def test_operator_unlock_uses_frozen_configuration_and_retains_quarantine(operator, tmp_path, monkeypatch, capsys):
    from backend.tests.test_provider_configuration import synthetic_configuration
    app, settings = operator
    path = tmp_path / "synthetic-funding.json"
    path.write_text(json.dumps(synthetic_configuration()))
    settings = replace(settings, paid_access_enabled=True, provider_config_file=path)
    monkeypatch.setattr(ops, "Settings", lambda: settings)
    result = execute(capsys, "spend-lock", "--unlock")
    assert result["spending_locked"] is False and result["provider_requests"] == 0
    with app.state.session_factory.begin() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        policy.metadata_json = {**policy.metadata_json, "restore_quarantine": True}
    assert execute(capsys, "spend-lock", "--unlock", status=1)["status"] == "blocked"


def test_operator_sanitizes_unexpected_failures(operator, monkeypatch, capsys):
    monkeypatch.setattr(ops, "config_check", lambda *args: (_ for _ in ()).throw(RuntimeError("PRIVATE_OPERATOR_EXCEPTION")))
    result = execute(capsys, "config-check", status=1)
    assert result["status"] == "blocked" and "PRIVATE_OPERATOR_EXCEPTION" not in str(result)
