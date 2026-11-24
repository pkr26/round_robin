"""Routine deletion replay cannot advance past unprocessed batch entries."""

import pytest
from round_robin.app import _record_manifest
from round_robin.database.models import DeletionRecord, PolicyBucket, utcnow
from round_robin.operations.recovery import purge_pending
from sqlalchemy import select

from backend.tests.test_api import application as application  # noqa: PLC0414


def test_already_purged_entry_does_not_skip_later_records(application):
    directory = application.state.settings.artifact_directory
    manifest = directory.parent / "deletion-manifest.jsonl"
    for identity in ("meeting_prior", "meeting_later_one", "meeting_later_two"):
        _record_manifest(manifest, identity, "owner_local", 1)
    with application.state.session_factory.begin() as session:
        session.add(DeletionRecord(meeting_id="meeting_prior", owner_id="owner_local", execution_fence=1, status="purged", purged_at=utcnow()))
    result = purge_pending(application.state.session_factory, directory, manifest, limit=3)
    assert result["records_reapplied"] == 2
    with application.state.session_factory() as session:
        for identity in ("meeting_prior", "meeting_later_one", "meeting_later_two"):
            record = session.get(DeletionRecord, identity)
            assert record is not None and record.status == "purged"
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert policy.metadata_json["deletion_manifest_cursor"]["offset"] == manifest.stat().st_size
    assert purge_pending(application.state.session_factory, directory, manifest, limit=3)["records_reapplied"] == 0


@pytest.mark.parametrize("limit", [1, 100])
def test_routine_batch_inclusive_limit_boundaries(application, limit):
    directory = application.state.settings.artifact_directory
    manifest = directory.parent / "deletion-manifest.jsonl"
    _record_manifest(manifest, "meeting_pending", "owner_local", 1)
    assert purge_pending(application.state.session_factory, directory, manifest, limit=limit)["records_reapplied"] == 1


@pytest.mark.parametrize("limit", [0, 101])
def test_routine_batch_rejects_limits_outside_bounds(application, limit):
    directory = application.state.settings.artifact_directory
    manifest = directory.parent / "deletion-manifest.jsonl"
    _record_manifest(manifest, "meeting_pending", "owner_local", 1)
    with pytest.raises(ValueError):
        purge_pending(application.state.session_factory, directory, manifest, limit=limit)
    with application.state.session_factory() as session:
        assert session.get(DeletionRecord, "meeting_pending") is None
