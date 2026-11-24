"""Orphan cleanup preserves referenced exports and skips vanished or non-files."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from round_robin.database.models import Base, ExportRecord, Meeting, Owner, PreflightRecord, Run
from round_robin.operations.maintenance import maintenance


@pytest.fixture
def state(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime(2026, 1, 10, tzinfo=UTC)
    with factory.begin() as session:
        session.add(Owner(id="owner_test"))
        session.flush()
        session.add(Meeting(id="meeting_test", owner_id="owner_test", title="Fixture"))
        session.flush()
        session.add(PreflightRecord(
            id="preflight_test", meeting_id="meeting_test", owner_id="owner_test",
            draft_revision=1, draft_hash="a" * 64, configuration_hash="b" * 64,
            payload={}, expires_at=now,
        ))
        session.flush()
        session.add(Run(
            id="run_test", meeting_id="meeting_test", owner_id="owner_test",
            preflight_id="preflight_test", input_snapshot={}, configuration={},
            initial_admission_expires_at=now,
        ))
    yield factory, tmp_path / "artifacts", now
    engine.dispose()


def old_file(path, now):
    path.write_bytes(b"export")
    timestamp = (now - timedelta(days=8)).timestamp()
    os.utime(path, (timestamp, timestamp))


def test_old_file_referenced_by_live_export_is_not_orphaned(state):
    factory, directory, now = state
    directory.mkdir()
    referenced = directory / ("export_" + "a" * 32 + ".bin")
    orphan = directory / ("export_" + "b" * 32 + ".bin")
    old_file(referenced, now)
    old_file(orphan, now)
    with factory.begin() as session:
        session.add(ExportRecord(
            id="export_test", run_id="run_test", owner_id="owner_test", report_version=1,
            format="json", status="ready", path=referenced.name, created_at=now,
        ))
    assert maintenance(factory, directory, now=now)["orphaned_export_files"] == 1
    assert referenced.read_bytes() == b"export"
    assert not orphan.exists()
    with factory() as session:
        row = session.get(ExportRecord, "export_test")
        assert (row.status, row.path) == ("ready", referenced.name)


@pytest.mark.parametrize("kind", ["vanished", "nonregular"])
def test_orphan_cleanup_continues_after_unavailable_candidate(state, monkeypatch, kind):
    factory, directory, now = state
    directory.mkdir()
    first = directory / ("export_" + "a" * 32 + ".bin")
    second = directory / ("export_" + "b" * 32 + ".bin")
    if kind == "nonregular":
        first.mkdir()
    old_file(second, now)
    original_glob = Path.glob

    def ordered_glob(path, pattern, *args, **kwargs):
        if path == directory and pattern == "export_*.bin*":
            return iter((first, second))
        return original_glob(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", ordered_glob)
    assert maintenance(factory, directory, now=now)["orphaned_export_files"] == 1
    assert not second.exists()
    if kind == "nonregular":
        assert first.is_dir()


def test_pending_orphan_removed_between_stat_and_unlink_does_not_stop_cleanup(state, monkeypatch):
    factory, directory, now = state
    directory.mkdir()
    vanished = directory / ("export_" + "a" * 32 + ".bin.pending")
    remaining = directory / ("export_" + "b" * 32 + ".bin.pending")
    old_file(vanished, now)
    old_file(remaining, now)
    original_glob, original_unlink = Path.glob, Path.unlink

    def ordered_glob(path, pattern, *args, **kwargs):
        if path == directory and pattern == "export_*.bin*":
            return iter((vanished, remaining))
        return original_glob(path, pattern, *args, **kwargs)

    def concurrently_removed(path, *args, **kwargs):
        if path == vanished:
            original_unlink(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", ordered_glob)
    monkeypatch.setattr(Path, "unlink", concurrently_removed)
    result = maintenance(factory, directory, now=now)
    assert result["orphaned_export_files"] == 2
    assert not remaining.exists()


def test_empty_maintenance_reports_no_work_or_provider_requests(state):
    factory, directory, now = state
    assert maintenance(factory, directory, now=now) == {
        "expired_uploads": 0,
        "expired_exports": 0,
        "interrupted_exports": 0,
        "orphaned_export_files": 0,
        "deletion_records_reapplied": 0,
        "provider_requests": 0,
    }
