"""Replaying a command cannot execute different or deleted work."""
import pytest
from round_robin.database.models import Draft, Meeting
from round_robin.fixtures import sample_brief
from sqlalchemy import func, select

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import command


@pytest.mark.parametrize("change", ["body", "operation"])
def test_reusing_command_for_different_work_reports_conflict_without_changing_state(client, application, change):
    brief = sample_brief().model_dump(mode="json")
    payload = command(brief=brief)
    created = client.post("/api/meetings", json=payload)
    assert created.status_code == 201
    meeting = created.json()
    assert client.post("/api/meetings", json=payload).json() == meeting
    prior_receipt = client.get(f"/api/commands/{payload['command_id']}").json()

    changed = {**brief, "title": "Different private proposal"}
    if change == "body":
        response = client.post("/api/meetings", json={**payload, "brief": changed})
    else:
        response = client.patch(f"/api/meetings/{meeting['id']}/draft", json={"command_id": payload["command_id"], "expected_revision": 1, "brief": changed})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "command_conflict"
    assert client.get(f"/api/commands/{payload['command_id']}").json() == prior_receipt
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Meeting)) == 1
        draft = session.get(Draft, meeting["id"])
        assert draft.revision == 1 and draft.payload["title"] == brief["title"]


def test_replaying_deleted_meeting_command_returns_gone_without_private_result(client, application):
    payload = command(brief=sample_brief().model_dump(mode="json"))
    created = client.post("/api/meetings", json=payload)
    assert created.status_code == 201
    meeting = created.json()
    assert client.request("DELETE", f"/api/meetings/{meeting['id']}", json=command()).status_code == 200

    response = client.post("/api/meetings", json=payload)
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "command_resource_gone"
    receipt = client.get(f"/api/commands/{payload['command_id']}").json()
    assert receipt["outcome"] == "gone" and receipt["result"] == {}
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Meeting)) == 1
        assert session.get(Meeting, meeting["id"]).deleted_at is not None
