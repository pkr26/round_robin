from round_robin.database.models import Command, Draft, PreflightRecord
from round_robin.schemas.briefs import DraftBrief
from sqlalchemy import select

from backend.tests.test_api import command, create

pytest_plugins = ("backend.tests.test_api",)


def test_legacy_brief_without_attachment_field_can_preflight(application, client):
    meeting = create(client)
    with application.state.session_factory.begin() as session:
        draft = session.get(Draft, meeting["id"])
        payload = dict(draft.payload)
        payload.pop("selected_attachment_ids", None)
        assert DraftBrief.model_validate(payload).selected_attachment_ids == []
        draft.payload = payload
    response = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1))
    assert response.status_code == 200
    assert response.json()["input_manifest"] == []


def test_missing_attachment_returns_a_useful_error_without_committing(application, client):
    meeting = create(client)
    with application.state.session_factory.begin() as session:
        draft = session.get(Draft, meeting["id"])
        draft.payload = {**draft.payload, "selected_attachment_ids": ["upload_missing"]}
    request = command(expected_draft_revision=1)
    response = client.post(f"/api/meetings/{meeting['id']}/preflight", json=request)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "input_scope_invalid"
    assert isinstance(error["message"], str) and error["message"].strip()
    assert error["command_outcome"] == "not_committed"
    with application.state.session_factory() as session:
        assert session.scalar(select(PreflightRecord)) is None
        assert session.get(Command, ("owner_local", request["command_id"])) is None
