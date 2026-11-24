"""Retention claims work without waiting for rows held by another cleaner."""

from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from round_robin.database.models import Base, Upload
from round_robin.operations.maintenance import maintenance


def test_upload_retention_requests_skip_locked(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    statements = []

    def capture(execute_state):
        statement = execute_state.statement
        if execute_state.is_select and any(
            description.get("entity") is Upload for description in statement.column_descriptions
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
