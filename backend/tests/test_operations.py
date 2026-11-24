from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, BudgetOperation, Command, Draft, ExportRecord, Meeting, PolicyBucket, Run, Step, utcnow
from round_robin.fixtures import sample_brief
from round_robin.operations.recovery import OperationError, backup, read_manifest, reconcile, restore_verify
from round_robin.ops import funding_configuration, set_policy_flag
from round_robin.orchestration.budget import operations_exposure
from round_robin.reports.storage import ArtifactStore


@pytest.fixture
def prepared(tmp_path):
    settings = replace(Settings(), artifact_directory=tmp_path / "artifacts")
    app = create_app(f"sqlite:///{tmp_path/'source.db'}", settings=settings, initialize=True)
    with TestClient(app) as client:
        meeting = client.post("/api/meetings", json={"command_id": str(uuid4()), "brief": sample_brief().model_dump(mode="json")}).json()
        quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json={"command_id": str(uuid4()), "expected_draft_revision": 1}).json()
        start = {"command_id": str(uuid4()), "preflight_id": quote["id"], "expected_draft_revision": 1}
        run = client.post(f"/api/meetings/{meeting['id']}/runs", json=start).json()
    with app.state.session_factory.begin() as session:
        session.add(BudgetOperation(id="operation_unknown", run_id=run["id"], owner_id="owner_local", kind="model", status="unknown", reserved_usd="0.40", settled_usd=None, authorized_at=utcnow() - timedelta(days=35), metadata_json={"model_call": True}))
    return app, meeting, run, start, settings


def test_backup_is_private_integrity_bound_and_never_overwrites(prepared, tmp_path):
    app, meeting, run, _, settings = prepared
    store = ArtifactStore(settings.artifact_directory)
    key, sha = store.put(b"Synthetic saved report")
    with app.state.session_factory.begin() as session:
        session.add(ExportRecord(id="export_test", run_id=run["id"], owner_id="owner_local", report_version=1, format="json", status="ready", path=key, sha256=sha))
    target = tmp_path / "backup"
    result = backup(app.state.engine, settings.artifact_directory, target, settings.artifact_directory.parent / "deletion-manifest.jsonl")
    assert result["status"] == "backup_saved"
    assert result["artifacts"] == 1
    assert (target / "artifacts" / key).read_bytes() == b"Synthetic saved report"
    assert os.stat(target / "snapshot.json").st_mode & 0o077 == 0
    assert os.stat(target).st_mode & 0o077 == 0
    with pytest.raises(OperationError, match="never overwritten"):
        backup(app.state.engine, settings.artifact_directory, target, settings.artifact_directory.parent / "deletion-manifest.jsonl")


def test_restore_reapplies_newer_deletions_preserves_unknowns_and_quarantines(prepared, tmp_path):
    app, meeting, run, start, settings = prepared
    source = tmp_path / "backup"
    manifest = settings.artifact_directory.parent / "deletion-manifest.jsonl"
    backup(app.state.engine, settings.artifact_directory, source, manifest)
    with TestClient(app) as client:
        assert client.request("DELETE", f"/api/meetings/{meeting['id']}", json={"command_id": str(uuid4())}).status_code == 200
    target = f"sqlite:///{tmp_path/'restore_test.db'}"
    result = restore_verify(source, target, tmp_path / "restored_artifacts", manifest)
    assert result["status"] == "restored_quarantined"
    assert result["deletion_records_reapplied"] == 1
    engine = create_engine(target)
    with sessionmaker(bind=engine)() as session:
        assert session.get(Draft, meeting["id"]) is None
        assert session.get(Meeting, meeting["id"]).deleted_at is not None
        assert session.get(Command, ("owner_local", start["command_id"])).gone is True
        operation = session.get(BudgetOperation, "operation_unknown")
        assert operation.status == "unknown"
        assert str(operations_exposure([operation], period="daily")) == "0.40"
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert policy.spending_locked is True
        assert policy.metadata_json["dispatch_locked"] is True
        assert policy.metadata_json["restore_quarantine"] is True
        assert policy.metadata_json["deployment_epoch"] > session.get(Run, run["id"]).deployment_epoch
    engine.dispose()


def test_restore_refuses_missing_corrupt_manifest_existing_or_production_target(prepared, tmp_path):
    app, _, _, _, settings = prepared
    source = tmp_path / "backup"
    manifest = settings.artifact_directory.parent / "deletion-manifest.jsonl"
    backup(app.state.engine, settings.artifact_directory, source, manifest)
    with pytest.raises(OperationError, match="manifest"):
        restore_verify(source, f"sqlite:///{tmp_path/'restore_test_1.db'}", tmp_path / "target", tmp_path / "missing.jsonl")
    with pytest.raises(OperationError, match="Refusing"):
        restore_verify(source, "postgresql+psycopg://operator@production.example/customer_data", tmp_path / "target", manifest)
    existing = tmp_path / "restore_test_existing.db"
    existing.touch()
    with pytest.raises(OperationError, match="new restore_test"):
        restore_verify(source, f"sqlite:///{existing}", tmp_path / "target", manifest)
    manifest.write_text("not valid JSON\n")
    with pytest.raises(OperationError, match="corrupt"):
        read_manifest(manifest)


def test_conservative_operator_booking_cannot_settle_or_renew_period_headroom(prepared):
    app, _, _, _, _ = prepared
    result = reconcile(app.state.session_factory, "operation_unknown", conservative_usd="0.50")
    assert result["status"] == "unknown"
    assert result["uncertainty_released"] is False
    with app.state.session_factory() as session:
        operation = session.get(BudgetOperation, "operation_unknown")
        assert str(operations_exposure([operation], period="monthly")) == "0.50"


def test_final_operation_bound_receipt_can_reconcile_provider_accounting(prepared, tmp_path):
    app, _, run, _, _ = prepared
    with app.state.session_factory.begin() as session:
        session.add(Step(id="step_unknown", run_id=run["id"], stage="synthesis", role_slot_id="synthesis", provider="openai", status="failed"))
        session.flush()
        session.add(Attempt(id="operation_unknown", run_id=run["id"], step_id="step_unknown", provider="openai", status="unknown", provider_request_id="synthetic-provider-request", reserved_usd="0.40", usage_certainty="unknown"))
    receipt = tmp_path / "provider-final.json"
    receipt.write_text(json.dumps({"operation_id": "operation_unknown", "final": True, "provider": "openai", "provider_request_id": "synthetic-provider-request", "reported_usd": "0.30", "usage_evidence": {"input_tokens": 10, "output_tokens": 2}}))
    result = reconcile(app.state.session_factory, "operation_unknown", provider_receipt=receipt)
    assert result["status"] == "settled"
    assert result["uncertainty_released"] is True


def test_unbound_receipt_cannot_release_unknown_exposure(prepared, tmp_path):
    app, _, _, _, _ = prepared
    receipt = tmp_path / "unbound-receipt.json"
    receipt.write_text(json.dumps({"operation_id": "operation_unknown", "final": True, "provider": "openai", "provider_request_id": "unrelated-request", "reported_usd": "0", "usage_evidence": {"input_tokens": 0}}))
    with pytest.raises(OperationError, match="unresolved"):
        reconcile(app.state.session_factory, "operation_unknown", provider_receipt=receipt)
    with app.state.session_factory() as session:
        assert session.get(BudgetOperation, "operation_unknown").status == "unknown"


def test_corrupt_artifact_restore_stays_quarantined_and_protected_reads_are_closed(prepared, tmp_path):
    app, _, run, _, settings = prepared
    store = ArtifactStore(settings.artifact_directory)
    key, sha = store.put(b"Synthetic saved report")
    with app.state.session_factory.begin() as session:
        session.add(ExportRecord(id="export_corrupt", run_id=run["id"], owner_id="owner_local", report_version=1, format="json", status="ready", path=key, sha256=sha))
    source = tmp_path / "backup_corrupt"
    manifest = settings.artifact_directory.parent / "deletion-manifest.jsonl"
    backup(app.state.engine, settings.artifact_directory, source, manifest)
    (source / "artifacts" / key).write_bytes(b"tampered bytes")
    target = f"sqlite:///{tmp_path/'restore_test_failed.db'}"
    with pytest.raises(OperationError, match="artifact integrity"):
        restore_verify(source, target, tmp_path / "failed_restore_artifacts", manifest)
    restored = create_app(target, settings=replace(Settings(), artifact_directory=tmp_path / "failed_restore_artifacts"))
    with TestClient(restored) as client:
        result = client.get("/api/meetings")
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "restore_quarantine"
    with restored.state.session_factory() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert policy.metadata_json["restore_quarantine"] and policy.spending_locked
        assert list(session.scalars(select(Meeting))) == []


def test_drain_and_kill_switch_are_durable_and_do_not_spend(prepared):
    app, _, _, _, _ = prepared
    assert set_policy_flag(app.state.session_factory, name="draining", value=True)["draining"]
    assert set_policy_flag(app.state.session_factory, name="spending_locked", value=True)["spending_locked"]
    with app.state.session_factory() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert policy.spending_locked and policy.metadata_json["draining"]


def test_explicit_image_only_funding_and_both_modalities_choose_lower_caps(monkeypatch, tmp_path):
    from round_robin.api import preflight
    from round_robin.providers import images
    image = {"version": "synthetic-image-v1", "deployment_daily_usd": "6", "deployment_monthly_usd": "30", "owner_daily_usd": "4", "owner_monthly_usd": "20"}
    monkeypatch.setenv("ROUND_ROBIN_IMAGE_CONFIG_FILE", str(tmp_path / "operator-image.json"))
    monkeypatch.setattr(images, "load_image_configuration", lambda settings: image)
    settings = replace(Settings(), paid_access_enabled=True, provider_config_file=None)
    assert funding_configuration(settings)["caps"]["owner_daily_usd"] == "4"
    monkeypatch.setattr(preflight, "run_configuration", lambda settings, mode: {"funded": True, "version": "synthetic-text-v1", "aggregate_funding": {"deployment_daily_usd": "3", "deployment_monthly_usd": "40", "owner_daily_usd": "8", "owner_monthly_usd": "10"}})
    settings = replace(settings, provider_config_file=tmp_path / "operator-text.json")
    merged = funding_configuration(settings)
    assert merged["caps"] == {"deployment_daily_usd": "3", "deployment_monthly_usd": "30", "owner_daily_usd": "4", "owner_monthly_usd": "10"}
    assert len(merged["versions"]) == 2
