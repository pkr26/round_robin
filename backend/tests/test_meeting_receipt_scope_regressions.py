from copy import deepcopy

import pytest
from round_robin.api import drafts
from round_robin.api.commands import aware
from round_robin.api.events import run_view
from round_robin.database.models import Command, Draft, Run
from round_robin.schemas.views import MeetingView
from sqlalchemy import select

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import command, create, start


def _previous_unscoped_meeting_view(session, meeting):
    draft = session.get(Draft, meeting.id)
    runs = list(session.scalars(select(Run).where(Run.meeting_id == meeting.id).order_by(Run.created_at.desc(), Run.id.desc()).limit(21)))
    return {"id": meeting.id, "title": meeting.title, "created_at": aware(meeting.created_at).isoformat(), "updated_at": aware(meeting.updated_at).isoformat(), "draft_revision": draft.revision, "draft": draft.payload, "runs": [run_view(session, run) for run in runs[:20]], "next_run_cursor": runs[19].id if len(runs) > 20 else None}


def test_saved_prior_followup_receipt_keeps_foreign_metadata(client, application, monkeypatch):
    meeting = create(client)
    owned = start(client, meeting)
    assert owned.status_code == 202
    owned_id = owned.json()["id"]
    with application.state.session_factory.begin() as session:
        session.get(Run, owned_id).status = "cancelled"
    foreign = start(client, meeting)
    assert foreign.status_code == 202, foreign.text
    foreign_id = foreign.json()["id"]
    with application.state.session_factory.begin() as session:
        row = session.get(Run, foreign_id)
        row.owner_id = "owner_foreign"
        row.status = "cancelled"
        row.stop_reason = "PRIVATE_RECEIPT_SENTINEL"
    payload = command(parent_run_id=owned_id, expected_draft_revision=1, brief=deepcopy(meeting["draft"]))
    url = f"/api/meetings/{meeting['id']}/follow-ups"
    # Generate an authentic old-format saved receipt under the old query.
    with monkeypatch.context() as patch:
        patch.setattr(drafts, "meeting_view", _previous_unscoped_meeting_view)
        created = client.post(url, json=payload)
    assert created.status_code == 201, created.text
    assert "PRIVATE_RECEIPT_SENTINEL" in created.text
    fresh = client.get(f"/api/meetings/{meeting['id']}")
    assert fresh.status_code == 200 and "PRIVATE_RECEIPT_SENTINEL" not in fresh.text
    with application.state.session_factory() as session:
        receipt = session.get(Command, ("owner_local", payload["command_id"]))
        prior = deepcopy(receipt.result)
    response = client.post(url, json=payload)
    assert response.status_code == 409 and "PRIVATE_RECEIPT_SENTINEL" not in response.text
    assert response.json()["error"]["command_outcome"] == "committed"
    for path, key in ((f"/api/commands/{payload['command_id']}", "outcome"),
                      (f"/api/commands/{payload['command_id']}/abandon", "status")):
        response = client.get(path) if key == "outcome" else client.post(path)
        assert response.status_code == 200 and "PRIVATE_RECEIPT_SENTINEL" not in response.text
        assert response.json()[key] == "committed"
        assert response.json()["result"]["receipt_unavailable"] is True
    with application.state.session_factory() as session:
        receipt = session.get(Command, ("owner_local", payload["command_id"]))
        assert receipt.result == prior


@pytest.mark.parametrize("damage", ["run_owner", "missing_run", "bad_meeting", "bad_runs", "bad_cursor", "bad_resource"])
def test_invalid_saved_meeting_scope_is_redacted_without_changing_receipt(client, application, damage):
    meeting = create(client)
    started = start(client, meeting)
    assert started.status_code == 202
    run_id = started.json()["id"]
    with application.state.session_factory.begin() as session:
        session.get(Run, run_id).status = "cancelled"
    payload = command(parent_run_id=run_id, expected_draft_revision=1, brief=deepcopy(meeting["draft"]))
    url = f"/api/meetings/{meeting['id']}/follow-ups"
    initial = client.post(url, json=payload)
    assert initial.status_code == 201, initial.text
    assert client.post(url, json=payload).json() == initial.json()
    with application.state.session_factory.begin() as session:
        row = session.get(Command, ("owner_local", payload["command_id"]))
        data = deepcopy(row.result)
        if damage == "run_owner": session.get(Run, run_id).owner_id = "owner_foreign"
        elif damage == "missing_run": data["runs"][0]["id"] = "run_missing"
        elif damage == "bad_meeting": data["runs"][0]["meeting_id"] = "meeting_other"
        elif damage == "bad_runs": data["runs"] = {"private": "SENSITIVE_RECEIPT_SENTINEL"}
        elif damage == "bad_cursor": data["next_run_cursor"] = "run_missing"
        else: row.resource_id = "meeting_other"
        row.result = data
        expected = deepcopy(data)
    response = client.post(url, json=payload)
    assert response.status_code == 409 and response.json()["error"]["command_outcome"] == "committed"
    for response, key in ((client.get(f"/api/commands/{payload['command_id']}"), "outcome"),
                          (client.post(f"/api/commands/{payload['command_id']}/abandon"), "status")):
        assert response.status_code == 200 and response.json()[key] == "committed"
        assert response.json()["result"]["receipt_unavailable"] is True
        assert "SENSITIVE_RECEIPT_SENTINEL" not in response.text
    with application.state.session_factory() as session:
        assert session.get(Command, ("owner_local", payload["command_id"])).result == expected


def test_valid_create_and_followup_replays_remain_unchanged(client, application):
    from round_robin.fixtures import sample_brief

    payload = command(brief=sample_brief().model_dump(mode="json"))
    first = client.post("/api/meetings", json=payload)
    assert first.status_code == 201
    assert client.post("/api/meetings", json=payload).json() == first.json()
    result = client.get(f"/api/commands/{payload['command_id']}")
    assert result.status_code == 200 and result.json()["outcome"] == "committed"
    assert MeetingView.model_validate(result.json()["result"]).model_dump(mode="json") == first.json()
    meeting = first.json()
    started = start(client, meeting)
    assert started.status_code == 202
    with application.state.session_factory.begin() as session:
        session.get(Run, started.json()["id"]).status = "cancelled"
    followup = command(parent_run_id=started.json()["id"], expected_draft_revision=1, brief=deepcopy(meeting["draft"]))
    url = f"/api/meetings/{meeting['id']}/follow-ups"
    first = client.post(url, json=followup)
    assert first.status_code == 201
    assert client.post(url, json=followup).json() == first.json()
    for response, key in ((client.get(f"/api/commands/{followup['command_id']}"), "outcome"),
                          (client.post(f"/api/commands/{followup['command_id']}/abandon"), "status")):
        assert response.status_code == 200 and response.json()[key] == "committed"
        assert MeetingView.model_validate(response.json()["result"]).model_dump(mode="json") == first.json()
