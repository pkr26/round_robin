"""Host-only API regressions for the follow-up parent replacement contract."""
import pytest
from round_robin.database.models import Command, Outbox, Run
from sqlalchemy import select

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import command, create, preflight, start
from backend.tests.test_exhaustive_lineage import followup


@pytest.mark.parametrize("changed_status", ["running", "completed"])
def test_parent_status_change_invalidates_approval_without_partial_replacement(client, application, changed_status):
    meeting = create(client)
    parent = start(client, meeting).json()
    linked = followup(client, meeting, parent)
    quote = preflight(client, linked)
    with application.state.session_factory.begin() as session:
        session.get(Run, parent["id"]).status = changed_status

    body = command(preflight_id=quote["id"], expected_draft_revision=2, replace_parent_run_version=1)
    response = client.post(f"/api/meetings/{meeting['id']}/runs", json=body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "parent_replacement_stale"
    with application.state.session_factory() as session:
        row = session.get(Run, parent["id"])
        assert row.status == changed_status and row.execution_fence == 0 and row.version == 1
        assert len(list(session.scalars(select(Run)))) == 1
        assert session.get(Command, ("owner_local", body["command_id"])) is None
        assert session.scalar(select(Outbox).where(Outbox.run_id == row.id, Outbox.kind == "finalize")) is None


def test_replacement_advances_existing_fence_and_publishes_reason(client, application):
    meeting = create(client)
    parent = start(client, meeting).json()
    with application.state.session_factory.begin() as session:
        row = session.get(Run, parent["id"])
        row.status, row.version, row.execution_fence = "running", 5, 7
    linked = followup(client, meeting, parent)
    quote = preflight(client, linked)
    body = command(preflight_id=quote["id"], expected_draft_revision=2, replace_parent_run_version=5)
    response = client.post(f"/api/meetings/{meeting['id']}/runs", json=body)
    assert response.status_code == 202, response.text
    with application.state.session_factory() as session:
        row = session.get(Run, parent["id"])
        assert row.execution_fence == 8 and row.version == 6
    events = client.get(f"/api/runs/{parent['id']}/events").json()["items"]
    cancelled = [event for event in events if event["event_type"] == "run.cancel_requested"]
    assert len(cancelled) == 1
    assert cancelled[0]["payload"]["reason"] == "replaced_by_follow_up"
