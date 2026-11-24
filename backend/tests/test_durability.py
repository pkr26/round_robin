import errno
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from backend.tests.test_artifacts_api import artifacts
from backend.tests.test_operations import prepared
from round_robin.database.models import BudgetOperation, ExportRecord, PolicyBucket, ReportVersion, Run
from round_robin.dispatcher.service import dispatch_once
from round_robin.operations.durability import mark_failure, storage_check
from round_robin.operations.recovery import backup
from round_robin.reports.storage import ArtifactStore, StorageFailure


def test_disk_full_export_never_claims_saved_and_locks_writes_and_dispatch(artifacts, monkeypatch):
    app, client, meeting, run, report = artifacts

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "PRIVATE_STORAGE_PATH_SENTINEL")

    monkeypatch.setattr(ArtifactStore, "put", disk_full)
    identity = str(uuid4())
    response = client.post(f"/api/runs/{run}/exports", json={"command_id": identity, "report_id": report["id"], "format": "json"})
    assert response.status_code == 200
    assert response.json()["status"] == "failed" and response.json()["download_url"] is None
    assert "PRIVATE_STORAGE_PATH_SENTINEL" not in response.text
    assert client.get("/ready").json() == {"status": "not_ready", "reason": "durability_compromised"}
    assert client.get(f"/api/runs/{run}/report").json() == report
    assert client.get(f"/api/commands/{identity}").json()["result"]["status"] == "failed"
    assert client.post(f"/api/meetings/{meeting}/preflight", json={"command_id": str(uuid4()), "expected_draft_revision": 1}).status_code == 503
    sends = []
    assert dispatch_once(session_factory=app.state.session_factory, sender=lambda *args: sends.append(args)) == 0
    assert not sends
    with app.state.session_factory() as session:
        assert session.get(ReportVersion, report["id"]).content == report
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert policy.spending_locked and policy.metadata_json["dispatch_locked"]


def test_failed_backup_is_not_published_and_requires_verified_replacement_before_clear(prepared, tmp_path, monkeypatch):
    app, meeting, run, _, settings = prepared
    from round_robin.operations import recovery
    target = tmp_path / "failed_backup"
    manifest = settings.artifact_directory.parent / "deletion-manifest.jsonl"
    original = recovery._private_write

    def full_snapshot(path, content):
        if path.name == "snapshot.json":
            raise OSError(errno.ENOSPC, "synthetic disk full")
        return original(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "_private_write", full_snapshot)
        with pytest.raises(OSError):
            backup(app.state.engine, settings.artifact_directory, target, manifest)
    assert not target.exists() and not list(tmp_path.glob(".backup_pending_*"))
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 503
        assert client.get(f"/api/meetings/{meeting['id']}").status_code == 200
    with pytest.raises(ValueError, match="new verified backup"):
        storage_check(app.state.session_factory, settings.artifact_directory, clear=True)
    result = backup(app.state.engine, settings.artifact_directory, tmp_path / "verified_replacement", manifest)
    assert result["status"] == "backup_saved"
    assert storage_check(app.state.session_factory, settings.artifact_directory, clear=True)["durability_gate_cleared"]
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
    with app.state.session_factory() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert policy.spending_locked and not policy.metadata_json["dispatch_locked"]
        assert session.get(BudgetOperation, "operation_unknown").status == "unknown"


def test_storage_recovery_probe_does_not_clear_corrupt_retained_artifacts(prepared):
    app, _, run, _, settings = prepared
    store = ArtifactStore(settings.artifact_directory)
    key, digest = store.put(b"synthetic immutable export")
    with app.state.session_factory.begin() as session:
        session.add(ExportRecord(id="export_storage_probe", run_id=run["id"], owner_id="owner_local", report_version=1, format="json", status="ready", path=key, sha256=digest))
    mark_failure(app.state.session_factory, "artifact_storage_failed", artifact_directory=settings.artifact_directory)
    (settings.artifact_directory / key).write_bytes(b"corrupt")
    with pytest.raises(StorageFailure):
        storage_check(app.state.session_factory, settings.artifact_directory, clear=True)
    with app.state.session_factory() as session:
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment")).metadata_json["durability_compromised"]
    assert not list(settings.artifact_directory.glob(".durability_probe_*"))


def test_uncaught_filesystem_failure_is_sanitized_and_persistently_closed(prepared, caplog):
    app, _, run, _, _ = prepared

    @app.post("/api/test-storage-fault")
    def failing_write():
        raise OSError(errno.EROFS, "PRIVATE_READONLY_PATH_SENTINEL")

    with TestClient(app) as client:
        response = client.post("/api/test-storage-fault")
        assert response.status_code == 503 and response.json()["error"]["command_outcome"] == "unknown"
        assert "PRIVATE_READONLY_PATH_SENTINEL" not in response.text + caplog.text
        assert client.get("/ready").status_code == 503
        stopped = client.post(f"/api/runs/{run['id']}/cancel", json={"command_id": str(uuid4()), "expected_run_version": run["version"]})
        assert stopped.status_code == 200 and stopped.json()["status"] == "cancel_requested"


def test_repeated_failures_preserve_backup_obligation_and_prior_dispatch_lock(prepared, tmp_path):
    app, _, _, _, settings = prepared
    factory = app.state.session_factory
    mark_failure(factory, "backup_failed", artifact_directory=settings.artifact_directory)
    mark_failure(factory, "artifact_storage_failed", artifact_directory=settings.artifact_directory)
    with pytest.raises(ValueError, match="new verified backup"):
        storage_check(factory, settings.artifact_directory, clear=True)
    backup(app.state.engine, settings.artifact_directory, tmp_path / "new_verified_backup", settings.artifact_directory.parent / "deletion-manifest.jsonl")
    storage_check(factory, settings.artifact_directory, clear=True)
    with factory.begin() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        policy.metadata_json = {**policy.metadata_json, "dispatch_locked": True, "draining": True}
    mark_failure(factory, "artifact_storage_failed", artifact_directory=settings.artifact_directory)
    storage_check(factory, settings.artifact_directory, clear=True)
    with factory.begin() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert policy.spending_locked and policy.metadata_json["dispatch_locked"] and policy.metadata_json["draining"]
        policy.metadata_json = {**policy.metadata_json, "restore_quarantine": True}
    with pytest.raises(ValueError, match="restoration quarantine"):
        storage_check(factory, settings.artifact_directory, clear=True)


def test_independent_marker_survives_database_recording_failure_and_restart(prepared):
    from sqlalchemy.exc import SQLAlchemyError
    from round_robin.app import create_app
    from round_robin.operations.durability import marker_pending, control_path
    from round_robin.orchestration.admission import check_eligible
    from round_robin.orchestration.budget import BudgetDenied
    app, meeting, run, _, settings = prepared

    class UnavailableDatabase:
        def begin(self):
            raise SQLAlchemyError("synthetic database outage")

    assert mark_failure(UnavailableDatabase(), "artifact_storage_failed", artifact_directory=settings.artifact_directory)
    directory = control_path(settings.artifact_directory)
    marker = next(directory.glob("failure_*.json"))
    assert marker.stat().st_mode & 0o077 == 0
    assert set(__import__("json").loads(marker.read_text())) == {"code", "timestamp"}
    with pytest.raises(SQLAlchemyError):
        storage_check(UnavailableDatabase(), settings.artifact_directory, clear=True)
    assert marker_pending(settings.artifact_directory)
    # Recreate the application after the DB is usable: no DB flag was saved.
    restarted = create_app(str(app.state.engine.url), settings=settings)
    with TestClient(restarted) as client:
        assert client.get("/ready").status_code == 503
        assert client.get(f"/api/meetings/{meeting['id']}").status_code == 200
        assert client.post(f"/api/meetings/{meeting['id']}/preflight", json={"command_id": str(uuid4()), "expected_draft_revision": 1}).status_code == 503
        assert dispatch_once(session_factory=restarted.state.session_factory, sender=lambda *args: pytest.fail("must not publish")) == 0
        with restarted.state.session_factory() as session:
            with pytest.raises(BudgetDenied, match="durability_compromised"):
                check_eligible(session, session.get(Run, run["id"]))
        storage_check(restarted.state.session_factory, settings.artifact_directory, clear=True)
        assert not marker_pending(settings.artifact_directory)
        assert client.get("/ready").status_code == 200
    with restarted.state.session_factory() as session:
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment")).spending_locked


def test_independent_backup_marker_stays_sticky_when_database_was_unavailable(prepared, tmp_path):
    from sqlalchemy.exc import SQLAlchemyError
    app, _, _, _, settings = prepared
    class UnavailableDatabase:
        def begin(self):
            raise SQLAlchemyError("synthetic outage")
    mark_failure(UnavailableDatabase(), "backup_failed", artifact_directory=settings.artifact_directory)
    mark_failure(app.state.session_factory, "artifact_storage_failed", artifact_directory=settings.artifact_directory)
    with pytest.raises(ValueError, match="new verified backup"):
        storage_check(app.state.session_factory, settings.artifact_directory, clear=True)
    backup(app.state.engine, settings.artifact_directory, tmp_path / "replacement_backup", settings.artifact_directory.parent / "deletion-manifest.jsonl")
    storage_check(app.state.session_factory, settings.artifact_directory, clear=True)


def test_separate_operator_marker_blocks_image_authority(tmp_path):
    from types import SimpleNamespace
    from sqlalchemy.exc import SQLAlchemyError
    from round_robin.worker.image_tasks import _authority
    from round_robin.visual.iterations import VisualLimit
    class UnavailableDatabase:
        def begin(self):
            raise SQLAlchemyError("synthetic outage")
    artifacts = tmp_path / "private" / "artifacts"
    control = tmp_path / "independent_operator_control"
    mark_failure(UnavailableDatabase(), "artifact_storage_failed", artifact_directory=artifacts, control_directory=control)
    session = SimpleNamespace(info={"artifact_directory": artifacts, "durability_control_directory": control})
    with pytest.raises(VisualLimit, match="Independent storage recovery"):
        _authority(None, None, None, session=session, artifact_directory=artifacts)
