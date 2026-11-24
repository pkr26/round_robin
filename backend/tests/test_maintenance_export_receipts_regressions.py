"""Export cleanup updates only live receipts for the matching export."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from round_robin.database.models import (
    Base,
    Command,
    ExportRecord,
    Meeting,
    Owner,
    PreflightRecord,
    Run,
)
from round_robin.operations.maintenance import maintenance
from round_robin.reports.storage import ArtifactStore


def test_export_cleanup_updates_only_matching_live_receipts(tmp_path):
    now = datetime(2026, 1, 2, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact_keys = []
    old_result = {
        "status": "ready",
        "error": None,
        "download_url": "/download",
        "preserved": "value",
    }
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
        session.flush()
        exports = [
            ("pending_1", "pending", 2), ("pending_2", "pending", 2),
            ("ready_1", "ready", 200), ("ready_2", "ready", 200),
        ]
        for identity, status, age in exports:
            key, digest = store.put(identity.encode())
            artifact_keys.append(key)
            session.add(ExportRecord(
                id=identity, run_id="run_test", owner_id="owner_test", report_version=1,
                format="json", status=status, path=key, sha256=digest,
                created_at=now - timedelta(hours=age),
            ))
        commands = [
            ("1", "pending_1", False), ("2", "ready_1", False),
            ("3", "ready_1", True), ("4", "unrelated", False),
        ]
        for identity, resource, gone in commands:
            session.add(Command(
                owner_id="owner_test", command_id=identity, command_type="export",
                body_hash="c" * 64, resource_id=resource, meeting_id="meeting_test",
                gone=gone, result=dict(old_result),
            ))
    assert len(set(artifact_keys)) == len(exports)
    assert all((store.directory / key).is_file() for key in artifact_keys)
    try:
        counts = maintenance(factory, store.directory, now=now)
        assert (counts["interrupted_exports"], counts["expired_exports"]) == (2, 2)
        assert all(not (store.directory / key).exists() for key in artifact_keys)
        with factory() as session:
            for identity, _, _ in exports:
                assert session.get(ExportRecord, identity).path is None
            pending = session.get(Command, ("owner_test", "1"))
            ready = session.get(Command, ("owner_test", "2"))
            assert pending.result == {
                **old_result, "status": "failed", "error": "export_interrupted",
                "download_url": None,
            }
            assert ready.result == {
                **old_result, "status": "purged", "error": "export_expired",
                "download_url": None,
            }
            assert session.get(Command, ("owner_test", "3")).result == old_result
            assert session.get(Command, ("owner_test", "4")).result == old_result
    finally:
        engine.dispose()
