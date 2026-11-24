import os
from datetime import UTC, datetime, timedelta

import pytest
from round_robin.database.models import Base, Meeting, Owner, Upload
from round_robin.operations.maintenance import maintenance
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def state(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Owner(id="owner_test"))
        session.flush()
        session.add(Meeting(id="meeting_test", owner_id="owner_test", title="Fixture"))
    yield factory, tmp_path / "artifacts"
    engine.dispose()


def test_upload_expiration_is_strict_and_scrubs_failed_temporary_data(state):
    factory, directory = state
    now = datetime(2026, 1, 2, 12, tzinfo=UTC)
    with factory.begin() as session:
        for identity, status, seconds in [("old", "failed", 3601), ("edge", "temporary", 3600), ("recent", "failed", 3599), ("ready", "ready", 5000)]:
            session.add(Upload(id=identity, meeting_id="meeting_test", owner_id="owner_test", status=status, filename="sensitive.txt", media_type="text/plain", canonical_text="sensitive", byte_size=9, created_at=now - timedelta(seconds=seconds)))
    counts = maintenance(factory, directory, now=now)
    assert counts["expired_uploads"] == 1
    with factory() as session:
        old = session.get(Upload, "old")
        assert (old.status, old.filename, old.canonical_text, old.byte_size) == ("purged", "", None, 0)
        for identity in ("edge", "recent", "ready"):
            row = session.get(Upload, identity)
            assert row.canonical_text == "sensitive" and row.filename == "sensitive.txt"


def test_upload_batch_processes_oldest_100_and_leaves_next_for_later(state):
    factory, directory = state
    now = datetime(2026, 1, 2, 12, tzinfo=UTC)
    with factory.begin() as session:
        for i in range(101):
            session.add(Upload(id=f"u_{i:03}", meeting_id="meeting_test", owner_id="owner_test", status="temporary", filename="private", media_type="text/plain", canonical_text="private", byte_size=7, created_at=now - timedelta(hours=2, seconds=i)))
    assert maintenance(factory, directory, now=now)["expired_uploads"] == 100
    with factory() as session:
        assert session.get(Upload, "u_000").status == "temporary"
        assert len(list(session.scalars(select(Upload).where(Upload.status == "purged")))) == 100
    assert maintenance(factory, directory, now=now)["expired_uploads"] == 1


def test_orphan_ages_are_strict_and_other_namespaces_stay_untouched(state):
    factory, directory = state
    directory.mkdir()
    now = datetime(2026, 1, 2, 12, tzinfo=UTC)
    cases = [("a", ".bin.pending", 3601, False), ("b", ".bin.pending", 3600, True), ("c", ".bin", 604801, False), ("d", ".bin", 604800, True)]
    for character, suffix, age, _ in cases:
        path = directory / ("export_" + character * 32 + suffix)
        path.write_bytes(b"private")
        os.utime(path, (now.timestamp() - age, now.timestamp() - age))
    outside = directory / ("visual_" + "e" * 32 + ".bin")
    outside.write_bytes(b"retained")
    os.utime(outside, (now.timestamp() - 604801, now.timestamp() - 604801))
    assert maintenance(factory, directory, now=now)["orphaned_export_files"] == 2
    for character, suffix, _, remains in cases:
        assert (directory / ("export_" + character * 32 + suffix)).exists() is remains
    assert outside.read_bytes() == b"retained"


def test_orphan_batch_stops_at_200_then_clears_remainder(state):
    factory, directory = state
    directory.mkdir()
    now = datetime(2026, 1, 2, 12, tzinfo=UTC)
    for i in range(201):
        path = directory / f"export_{i:032x}.bin"
        path.write_bytes(b"private")
        os.utime(path, (now.timestamp() - 604801, now.timestamp() - 604801))
    assert maintenance(factory, directory, now=now)["orphaned_export_files"] == 200
    assert len(list(directory.glob("export_*.bin"))) == 1
    assert maintenance(factory, directory, now=now)["orphaned_export_files"] == 1
