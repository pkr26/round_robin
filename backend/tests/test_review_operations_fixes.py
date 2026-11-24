from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.tests.test_api import application
from backend.tests.test_artifacts_api import artifacts
from round_robin.app import _record_manifest
from round_robin.database.models import DeletionRecord, ExportRecord, Meeting, Run, utcnow
from round_robin.operations.maintenance import maintenance
from round_robin.operations.recovery import backup, read_manifest
from round_robin.reports.storage import ArtifactStore


def test_routine_purge_reads_bounded_new_records_and_skips_completed_history(application, monkeypatch):
    settings = application.state.settings
    manifest = settings.artifact_directory.parent / 'deletion-manifest.jsonl'
    for number in range(205):
        _record_manifest(manifest, f'meeting_old_{number}', 'owner_local', 1)
    with application.state.session_factory.begin() as session:
        session.add_all(DeletionRecord(meeting_id=f'meeting_old_{number}', owner_id='owner_local', execution_fence=1, status='purged', purged_at=utcnow()) for number in range(205))
    from round_robin.operations import recovery
    original = recovery.purge_record
    applied = []
    def observe(session, meeting, *args):
        applied.append(meeting)
        return original(session, meeting, *args)
    monkeypatch.setattr(recovery, 'purge_record', observe)
    offsets = []
    from round_robin.database.models import PolicyBucket
    for _ in range(3):
        assert maintenance(application.state.session_factory, settings.artifact_directory)['deletion_records_reapplied'] == 0
        with application.state.session_factory() as session:
            offsets.append(session.scalar(select(PolicyBucket).where(PolicyBucket.key == 'deployment')).metadata_json['deletion_manifest_cursor']['offset'])
    assert 0 < offsets[0] < offsets[1] < offsets[2] == manifest.stat().st_size
    assert not applied
    # A manifest append survived but its original database transaction did not.
    _record_manifest(manifest, 'meeting_new_pending', 'owner_local', 2)
    assert maintenance(application.state.session_factory, settings.artifact_directory)['deletion_records_reapplied'] == 1
    assert applied == ['meeting_new_pending']
    assert maintenance(application.state.session_factory, settings.artifact_directory)['deletion_records_reapplied'] == 0
    assert len(read_manifest(manifest)) == 206  # Independent restore history stays complete.


@pytest.mark.parametrize('replacement', ['new_inode', 'truncated', 'rewritten'])
def test_routine_purge_recovers_after_manifest_replacement(application, replacement):
    directory = application.state.settings.artifact_directory
    manifest = directory.parent / 'deletion-manifest.jsonl'
    _record_manifest(manifest, 'meeting_old_record', 'owner_local', 1)
    assert maintenance(application.state.session_factory, directory)['deletion_records_reapplied'] == 1
    original_size = manifest.stat().st_size
    fresh_id = 'meeting_new' if replacement == 'truncated' else 'meeting_new_record'
    fresh = manifest.with_name('replacement.jsonl')
    _record_manifest(fresh, fresh_id, 'owner_local', 1)
    if replacement == 'new_inode':
        os.replace(fresh, manifest)
    else:
        manifest.write_bytes(fresh.read_bytes())
        if replacement == 'truncated':
            assert manifest.stat().st_size < original_size
        else:
            assert manifest.stat().st_size == original_size
    assert maintenance(application.state.session_factory, directory)['deletion_records_reapplied'] == 1
    with application.state.session_factory() as session:
        assert session.get(DeletionRecord, fresh_id).status == 'purged'
    assert maintenance(application.state.session_factory, directory)['deletion_records_reapplied'] == 0


@pytest.mark.parametrize('damaged_tail', [b'{"meeting_id":', b' ' * 1024 + b'\n'])
def test_invalid_manifest_append_cannot_partially_purge_or_advance_cursor(application, damaged_tail):
    from round_robin.database.models import PolicyBucket
    from round_robin.operations.recovery import OperationError
    directory = application.state.settings.artifact_directory
    manifest = directory.parent / 'deletion-manifest.jsonl'
    _record_manifest(manifest, 'meeting_old_record', 'owner_local', 1)
    maintenance(application.state.session_factory, directory)
    with application.state.session_factory() as session:
        prior = dict(session.scalar(select(PolicyBucket).where(PolicyBucket.key == 'deployment')).metadata_json['deletion_manifest_cursor'])
    _record_manifest(manifest, 'meeting_new_record', 'owner_local', 2)
    valid = manifest.read_bytes()
    with manifest.open('ab') as handle:
        handle.write(damaged_tail)
    with pytest.raises(OperationError, match='incomplete or oversized'):
        maintenance(application.state.session_factory, directory)
    with application.state.session_factory() as session:
        assert session.get(DeletionRecord, 'meeting_new_record') is None
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == 'deployment')).metadata_json['deletion_manifest_cursor'] == prior
    manifest.write_bytes(valid)
    assert maintenance(application.state.session_factory, directory)['deletion_records_reapplied'] == 1


def test_backup_and_legal_delete_cleanup_share_one_consistent_artifact_snapshot(artifacts, tmp_path, monkeypatch):
    app, client, meeting_id, run_id, report = artifacts
    exported = client.post(f'/api/runs/{run_id}/exports', json={'command_id': str(uuid4()), 'report_id': report['id'], 'format': 'json'}).json()
    assert exported['status'] == 'ready'
    with app.state.session_factory() as session:
        key = session.get(ExportRecord, exported['id']).path
    directory = app.state.settings.artifact_directory
    copying, allow_copy, purge_waiting = threading.Event(), threading.Event(), threading.Event()
    from round_robin.operations import artifact_lifecycle
    read = ArtifactStore.read
    hold = artifact_lifecycle.hold_artifact_lifecycle
    def paused_read(store, requested, digest):
        if requested == key and not copying.is_set():
            copying.set()
            assert allow_copy.wait(5)
        return read(store, requested, digest)
    def waiting_hold(session, path):
        purge_waiting.set()
        return hold(session, path)
    monkeypatch.setattr(ArtifactStore, 'read', paused_read)
    monkeypatch.setattr(artifact_lifecycle, 'hold_artifact_lifecycle', waiting_hold)
    def delete_and_clean():
        response = client.request('DELETE', f'/api/meetings/{meeting_id}', json={'command_id': str(uuid4())})
        assert response.status_code == 200, response.text
        return maintenance(app.state.session_factory, directory)
    target = tmp_path / 'consistent_backup'
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = pool.submit(backup, app.state.engine, directory, target, directory.parent / 'deletion-manifest.jsonl')
        try:
            assert copying.wait(5)
            removed = pool.submit(delete_and_clean)
            assert purge_waiting.wait(5)
            assert not removed.done() and (directory / key).exists()
        finally:
            allow_copy.set()
        assert saved.result(timeout=10)['status'] == 'backup_saved'
        assert removed.result(timeout=10)['deletion_records_reapplied'] == 1
    assert not (directory / key).exists()
    assert (target / 'artifacts' / key).is_file()
    assert client.get('/ready').status_code == 200
    assert read_manifest(directory.parent / 'deletion-manifest.jsonl')[0]['meeting_id'] == meeting_id
