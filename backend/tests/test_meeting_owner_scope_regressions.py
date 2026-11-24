from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from round_robin.api.auth import require_owner
from round_robin.api.commands import owned_meeting
from round_robin.database.models import Command, Meeting, Owner, Run, utcnow
from round_robin.database.services import meeting_for_owner
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql

pytest_plugins = ("backend.tests.test_operations",)


@pytest.mark.parametrize("locked", [False, True])
def test_exact_meeting_and_owner_scope(prepared, locked):
    app, meeting, *_ = prepared
    with app.state.session_factory.begin() as session:
        session.add(Owner(id="owner_other")); session.flush()
        session.add(Meeting(id="meeting_other", owner_id="owner_other", title="Private synthetic")); session.flush()
        assert meeting_for_owner(session, meeting["id"], "owner_local", lock=locked).id == meeting["id"]
        assert meeting_for_owner(session, meeting["id"], "owner_other", lock=locked) is None
        assert meeting_for_owner(session, "meeting_other", "owner_local", lock=locked) is None
        assert meeting_for_owner(session, "meeting_missing", "owner_local", lock=locked) is None


def test_foreign_owner_cannot_read_or_preflight_a_meeting(prepared):
    app, meeting, *_ = prepared
    with app.state.session_factory() as session:
        runs_before = len(list(session.scalars(select(Run))))
    with TestClient(app) as client:
        assert client.get(f"/api/meetings/{meeting['id']}").status_code == 200
        app.dependency_overrides[require_owner] = lambda: "owner_other"
        try:
            assert client.get(f"/api/meetings/{meeting['id']}").status_code == 404
            command = str(uuid4())
            response = client.post(f"/api/meetings/{meeting['id']}/preflight", json={"command_id": command, "expected_draft_revision": 1})
            assert response.status_code == 404
            assert "Private synthetic" not in response.text
        finally:
            app.dependency_overrides.clear()
    with app.state.session_factory() as session:
        assert session.get(Command, ("owner_other", command)) is None
        assert len(list(session.scalars(select(Run)))) == runs_before


def test_locked_lookup_refreshes_cached_deletion_before_authorization(prepared):
    app, meeting, *_ = prepared
    with app.state.session_factory() as session, session.begin():
        cached = session.get(Meeting, meeting["id"])
        assert cached.deleted_at is None
        session.execute(update(Meeting).where(Meeting.id == cached.id).values(deleted_at=utcnow()).execution_options(synchronize_session=False))
        assert cached.deleted_at is None
        refreshed = meeting_for_owner(session, cached.id, "owner_local", lock=True)
        assert refreshed is cached and refreshed.deleted_at is not None
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            owned_meeting(session, cached.id, "owner_local", lock=True)
        assert error.value.status_code == 410


@pytest.mark.parametrize("locked", [False, True])
def test_lock_option_emits_postgresql_lock_and_refresh_without_connection(locked):
    captured = []
    class InspectSession:
        def scalar(self, statement):
            captured.append(statement)
    assert meeting_for_owner(InspectSession(), "meeting_synthetic", "owner_synthetic", lock=locked) is None
    assert len(captured) == 1
    query = captured[0]
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert ("FOR UPDATE" in sql) is locked
    assert bool(query.get_execution_options().get("populate_existing")) is locked


def test_default_read_lookup_does_not_request_write_lock():
    captured = []
    class InspectSession:
        def scalar(self, statement):
            captured.append(statement)
    assert meeting_for_owner(InspectSession(), "meeting_synthetic", "owner_synthetic") is None
    assert len(captured) == 1
    assert "FOR UPDATE" not in str(captured[0].compile(dialect=postgresql.dialect()))
    assert not captured[0].get_execution_options().get("populate_existing")
