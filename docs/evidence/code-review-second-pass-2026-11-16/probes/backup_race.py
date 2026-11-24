"""Deterministic interleaving of a legal delete/cleanup with an in-flight backup."""
from pathlib import Path
import json
import tempfile
from uuid import uuid4
from unittest.mock import patch

from backend.tests.test_artifacts_api import artifacts
from round_robin.database.models import ExportRecord
from round_robin.operations import recovery
from round_robin.operations.maintenance import maintenance


with tempfile.TemporaryDirectory(prefix="rr-audit2-backup-race-") as directory:
    fixture = artifacts.__wrapped__(Path(directory))
    app, client, meeting_id, run_id, report = next(fixture)
    result = {}
    try:
        response = client.post(f"/api/runs/{run_id}/exports", json={
            "command_id": str(uuid4()), "report_id": report["id"], "format": "json"})
        response.raise_for_status()
        exported = response.json()
        assert exported["status"] == "ready"
        with app.state.session_factory() as session:
            key = session.get(ExportRecord, exported["id"]).path
        artifact_dir = app.state.settings.artifact_directory
        result["ready_before_backup"] = client.get("/ready").status_code
        result["export_exists_before_backup"] = (artifact_dir / key).is_file()
        original = recovery._manifest_snapshot
        interleaved = False

        def after_database_snapshot(path):
            global interleaved
            if not interleaved:
                interleaved = True
                deleted = client.request("DELETE", f"/api/meetings/{meeting_id}", json={"command_id": str(uuid4())})
                result["delete_status"] = deleted.status_code
                assert deleted.status_code == 200
                result["cleanup"] = maintenance(app.state.session_factory, artifact_dir)
                result["export_exists_after_cleanup"] = (artifact_dir / key).is_file()
            return original(path)

        target = Path(directory) / "backup"
        try:
            with patch.object(recovery, "_manifest_snapshot", after_database_snapshot):
                recovery.backup(app.state.engine, artifact_dir, target, artifact_dir.parent / "deletion-manifest.jsonl")
            result["backup_outcome"] = "saved"
        except Exception as exc:
            result["backup_outcome"] = type(exc).__name__
        ready = client.get("/ready")
        result["ready_after_backup"] = ready.status_code
        result["readiness_reason"] = ready.json()
        result["backup_published"] = target.exists()
        assert interleaved
        assert result["backup_outcome"] == "FileNotFoundError"
        assert ready.status_code == 503
        print(json.dumps(result, indent=2))
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
