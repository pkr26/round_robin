"""Export retention respects strict age limits and terminal states."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from round_robin.database.models import Base, ExportRecord, Meeting, Owner, PreflightRecord, Run
from round_robin.operations.maintenance import maintenance


@pytest.mark.parametrize(
    ("status", "age", "expected_status", "expected_code", "counts"),
    [
        ("pending", timedelta(minutes=59), "pending", None, (0, 0)),
        ("pending", timedelta(hours=1), "pending", None, (0, 0)),
        ("pending", timedelta(hours=1, microseconds=1), "failed", "export_interrupted", (1, 0)),
        ("ready", timedelta(days=7), "ready", None, (0, 0)),
        ("ready", timedelta(days=7, microseconds=1), "purged", "export_expired", (0, 1)),
        ("failed", timedelta(days=8), "failed", "previous_failure", (0, 0)),
    ],
)
def test_export_retention_strict_thresholds_and_terminal_state(
    tmp_path, status, age, expected_status, expected_code, counts
):
    now = datetime(2026, 1, 10, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
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
        session.add(ExportRecord(
            id="export_test", run_id="run_test", owner_id="owner_test",
            report_version=1, format="json", status=status,
            error_code="previous_failure" if status == "failed" else None,
            created_at=now - age,
        ))
    try:
        result = maintenance(factory, tmp_path / "artifacts", now=now)
        assert (result["interrupted_exports"], result["expired_exports"]) == counts
        with factory() as session:
            row = session.get(ExportRecord, "export_test")
            assert (row.status, row.error_code) == (expected_status, expected_code)
    finally:
        engine.dispose()
