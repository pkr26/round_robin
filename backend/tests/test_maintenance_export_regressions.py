"""Export cleanup keeps bounded oldest-first claims and nonblocking row locks."""

from datetime import UTC, datetime, timedelta

from round_robin.database.models import Base, ExportRecord, Meeting, Owner, PreflightRecord, Run
from round_robin.operations.maintenance import maintenance
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker


def test_export_cleanup_claims_oldest_100_then_remainder(tmp_path):
    now = datetime(2026, 1, 2, tzinfo=UTC)
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
        # Insert newest first so row order alone cannot satisfy oldest-first cleanup.
        for i in range(101):
            session.add(ExportRecord(
                id=f"export_{i:03}", run_id="run_test", owner_id="owner_test",
                report_version=1, format="json", status="pending",
                created_at=now - timedelta(hours=2, seconds=i),
            ))
    try:
        assert maintenance(factory, tmp_path / "artifacts", now=now)["interrupted_exports"] == 100
        with factory() as session:
            assert session.get(ExportRecord, "export_000").status == "pending"
            for i in range(1, 101):
                row = session.get(ExportRecord, f"export_{i:03}")
                assert (row.status, row.error_code) == ("failed", "export_interrupted")
        assert maintenance(factory, tmp_path / "artifacts", now=now)["interrupted_exports"] == 1
    finally:
        engine.dispose()


def test_export_retention_requests_skip_locked(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    statements = []

    def capture(execute_state):
        statement = execute_state.statement
        if execute_state.is_select and any(
            description.get("expr") is ExportRecord for description in statement.column_descriptions
        ):
            statements.append(str(statement.compile(dialect=postgresql.dialect())))

    event.listen(factory.class_, "do_orm_execute", capture)
    try:
        maintenance(factory, tmp_path / "artifacts", now=datetime(2026, 1, 2, tzinfo=UTC))
    finally:
        event.remove(factory.class_, "do_orm_execute", capture)
        engine.dispose()
    assert len(statements) == 1
    assert "FOR UPDATE SKIP LOCKED" in statements[0]
