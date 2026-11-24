"""Event payload limits and sequence updates are durable as a unit."""
import json

import pytest
from round_robin.database.models import Event, Run
from round_robin.database.services import append_event
from sqlalchemy import select

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import create, start


def payload_with_json_bytes(length, character):
    empty_size = len(json.dumps({"text": ""}, ensure_ascii=False).encode())
    count, remainder = divmod(length - empty_size, len(character.encode()))
    result = {"text": character * count + "x" * remainder}
    assert len(json.dumps(result, ensure_ascii=False).encode()) == length
    return result


def test_event_page_preserves_committed_payload(client, application):
    meeting = create(client)
    response = start(client, meeting)
    assert response.status_code == 202
    run_id = response.json()["id"]
    payload = {"reason": "replaced_by_follow_up", "step_id": "step_synthetic"}
    with application.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        prior = run.event_sequence
        append_event(session, run, "synthetic.notice", payload)
    page = client.get(f"/api/runs/{run_id}/events", params={"after": prior})
    assert page.status_code == 200
    assert len(page.json()["items"]) == 1
    assert page.json()["items"][0]["payload"] == payload


@pytest.mark.parametrize("character", ["a", "é"])
def test_event_exact_utf8_payload_limit_and_rollback_preserve_sequence(client, application, character):
    meeting = create(client)
    response = start(client, meeting)
    assert response.status_code == 202
    run_id = response.json()["id"]
    at_limit = payload_with_json_bytes(16384, character)
    over_limit = payload_with_json_bytes(16385, character)
    with application.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        prior = run.event_sequence
        append_event(session, run, "synthetic.boundary", at_limit)
    with pytest.raises(ValueError, match="Event payload exceeds 16 KiB"), application.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        append_event(session, run, "synthetic.rollback", {"message": "rolled back"})
        append_event(session, run, "synthetic.too_large", over_limit)
    with application.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        assert run.event_sequence == prior + 1
        append_event(session, run, "synthetic.after", {"message": "next committed event"})
    response = client.get(f"/api/runs/{run_id}/events", params={"after": prior})
    assert response.status_code == 200
    events = response.json()["items"]
    assert [event["sequence"] for event in events] == [prior + 1, prior + 2]
    assert [event["event_type"] for event in events] == ["synthetic.boundary", "synthetic.after"]
    assert events[0]["payload"] == at_limit
    with application.state.session_factory() as session:
        assert session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == "synthetic.rollback")) is None
