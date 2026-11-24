"""Retention must commit its file/row changes before a backup can snapshot them."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import json
import threading
from uuid import uuid4

from backend.tests.test_artifacts_api import artifacts
from round_robin.database.models import ExportRecord, utcnow
from round_robin.operations import recovery
from round_robin.operations.maintenance import maintenance
from round_robin.reports.storage import ArtifactStore


def test_backup_waits_for_retention_file_deletion_and_database_commit(artifacts, tmp_path, monkeypatch):
    app, client, _, run_id, report = artifacts
    response = client.post(f"/api/runs/{run_id}/exports", json={
        "command_id": str(uuid4()), "report_id": report["id"], "format": "json",
    })
    assert response.status_code == 200, response.text
    exported = response.json()
    assert exported["status"] == "ready"
    with app.state.session_factory.begin() as session:
        row = session.get(ExportRecord, exported["id"])
        row.created_at = utcnow() - timedelta(days=8)
        key = row.path

    directory = app.state.settings.artifact_directory
    deleted, allow_commit = threading.Event(), threading.Event()
    backup_waiting, backup_acquired = threading.Event(), threading.Event()
    purge = ArtifactStore.purge
    lifecycle = recovery.artifact_lifecycle

    def paused_purge(store, requested):
        purge(store, requested)
        if requested == key:
            # The bytes are gone, but the surrounding maintenance transaction
            # has not yet changed/committed the ready export row.
            deleted.set()
            assert allow_commit.wait(5)

    @contextmanager
    def observed_backup_guard(path):
        backup_waiting.set()
        with lifecycle(path):
            backup_acquired.set()
            yield

    monkeypatch.setattr(ArtifactStore, "purge", paused_purge)
    monkeypatch.setattr(recovery, "artifact_lifecycle", observed_backup_guard)
    target = tmp_path / "retention_consistent_backup"
    with ThreadPoolExecutor(max_workers=2) as pool:
        cleaning = pool.submit(maintenance, app.state.session_factory, directory)
        try:
            assert deleted.wait(5)
            assert not (directory / key).exists()
            saving = pool.submit(recovery.backup, app.state.engine, directory, target,
                                 directory.parent / "deletion-manifest.jsonl")
            assert backup_waiting.wait(5)
            assert not backup_acquired.wait(.1)
            assert not saving.done()
        finally:
            allow_commit.set()
        assert cleaning.result(timeout=10)["expired_exports"] == 1
        saved = saving.result(timeout=10)

    assert backup_acquired.is_set()
    assert saved["status"] == "backup_saved" and saved["artifacts"] == 0
    snapshot = json.loads((target / "snapshot.json").read_text())
    stored = next(row for row in snapshot["tables"]["exports"] if row["id"] == exported["id"])
    assert stored["status"] == "purged" and stored["path"] is None
    assert not (target / "artifacts" / key).exists()
    assert client.get("/ready").status_code == 200
