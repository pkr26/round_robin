import hashlib
import json
from uuid import uuid4

import pytest
from round_robin.database.models import (
    DeletionRecord,
    ExportRecord,
    Meeting,
    PolicyBucket,
    Run,
)
from round_robin.database.visual_models import DesignSession, DesignVersion
from round_robin.operations.recovery import OperationError, purge_pending
from round_robin.reports.storage import ArtifactStore
from sqlalchemy import select

from backend.tests.test_api import command, create, start

pytest_plugins = ("backend.tests.test_api",)


def test_foreign_design_file_is_untouched_and_manifest_not_appended(application, client):
    meeting = create(client)
    namespace = application.state.settings.artifact_directory / "visual" / "foreign_owner" / "design_foreign"
    namespace.mkdir(parents=True)
    path = namespace / "private.png"
    content = b"foreign private pixels"
    path.write_bytes(content)
    with application.state.session_factory.begin() as session:
        session.add(DesignSession(id="design_foreign", meeting_id=meeting["id"], owner_id="foreign_owner", mode="review_only", frozen_brief={}, state={}))
        session.flush()
        session.add(DesignVersion(id="version_foreign", session_id="design_foreign", version=1, asset_path=str(path), canonical_hash=hashlib.sha256(content).hexdigest(), original_hash=hashlib.sha256(content).hexdigest(), width=1, height=1, source_kind="upload", provenance={}))
    calls = []
    application.state.record_deletion_manifest = lambda *args: calls.append(args)
    response = client.request("DELETE", f"/api/meetings/{meeting['id']}", json=command())
    assert response.status_code == 409 and response.json()["error"]["code"] == "deletion_scope_invalid"
    assert calls == [] and path.read_bytes() == content
    with application.state.session_factory() as session:
        assert session.get(DesignVersion, "version_foreign") is not None
        assert session.get(Meeting, meeting["id"]).deleted_at is None


def test_old_manifest_foreign_export_stops_before_unlink_and_cursor_commit(application, client):
    meeting = create(client)
    run_id = start(client, meeting).json()["id"]
    store = ArtifactStore(application.state.settings.artifact_directory)
    key, digest = store.put(b"foreign private export")
    with application.state.session_factory.begin() as session:
        session.add(ExportRecord(id="export_foreign", run_id=run_id, owner_id="foreign_owner", report_version=1, format="json", status="ready", path=key, sha256=digest))
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        old_metadata = dict(policy.metadata_json)
        original = session.get(Run, run_id).status
    manifest = application.state.settings.artifact_directory.parent / f"manifest_{uuid4().hex}.jsonl"
    manifest.write_text(json.dumps({"meeting_id": meeting["id"], "owner_id": "owner_local", "execution_fence": 1}) + "\n")
    with pytest.raises(OperationError):
        purge_pending(application.state.session_factory, store.directory, manifest, limit=100)
    assert store.read(key, digest) == b"foreign private export"
    with application.state.session_factory() as session:
        assert session.get(Run, run_id).status == original
        assert session.get(DeletionRecord, meeting["id"]) is None
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment")).metadata_json == old_metadata


def test_later_corrupt_manifest_record_cannot_unlink_earlier_valid_export(application, client):
    first = create(client)
    first_run = start(client, first).json()["id"]
    second = create(client)
    second_run = start(client, second).json()["id"]
    store = ArtifactStore(application.state.settings.artifact_directory)
    key, digest = store.put(b"first retained export")
    with application.state.session_factory.begin() as session:
        session.add(ExportRecord(id="export_first", run_id=first_run, owner_id="owner_local", report_version=1, format="json", status="ready", path=key, sha256=digest))
        session.get(Run, second_run).owner_id = "foreign_owner"
    manifest = application.state.settings.artifact_directory.parent / f"manifest_{uuid4().hex}.jsonl"
    manifest.write_text("".join(json.dumps({"meeting_id": item["id"], "owner_id": "owner_local", "execution_fence": 1}) + "\n" for item in (first, second)))
    with pytest.raises(OperationError):
        purge_pending(application.state.session_factory, store.directory, manifest, limit=100)
    assert store.read(key, digest) == b"first retained export"


def test_already_purged_record_is_skipped_without_touching_later_foreign_state(application, client):
    meeting = create(client)
    run_id = start(client, meeting).json()["id"]
    store = ArtifactStore(application.state.settings.artifact_directory)
    key, digest = store.put(b"foreign bytes remain")
    with application.state.session_factory.begin() as session:
        session.get(Run, run_id).owner_id = "foreign_owner"
        session.add(ExportRecord(id="export_late", run_id=run_id, owner_id="foreign_owner", report_version=1, format="json", status="ready", path=key, sha256=digest))
        session.add(DeletionRecord(meeting_id=meeting["id"], owner_id="owner_local", execution_fence=1, status="purged"))
    manifest = application.state.settings.artifact_directory.parent / f"manifest_{uuid4().hex}.jsonl"
    manifest.write_text(json.dumps({"meeting_id": meeting["id"], "owner_id": "owner_local", "execution_fence": 1}) + "\n")
    assert purge_pending(application.state.session_factory, store.directory, manifest, limit=100)["records_reapplied"] == 0
    assert store.read(key, digest) == b"foreign bytes remain"
    with application.state.session_factory() as session:
        assert session.get(Run, run_id).owner_id == "foreign_owner"
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment")).metadata_json["deletion_manifest_cursor"]["offset"] > 0
