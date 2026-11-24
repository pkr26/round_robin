import json
from datetime import UTC, datetime

from round_robin.database.models import Run
from round_robin.database.services import append_event

from backend.tests.test_api import create, start

pytest_plugins = ("backend.tests.test_api",)


def test_persisted_event_identity_time_and_payload_survive_snapshot_replay_and_sse(application, client):
    meeting = create(client)
    run = start(client, meeting).json()
    payload = {"operation_id": "synthetic_operation", "details": {"cost": "0.42", "certainty": "unknown"}, "references": ["one", "two"]}
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        event = append_event(session, row, "synthetic.cost_settled", payload)
        session.flush()
        expected_time, sequence = event.created_at, event.sequence
        row.status = "completed"
    expected = {"run_id": run["id"], "sequence": sequence, "event_type": "synthetic.cost_settled", "schema_version": "1.0", "payload": payload}
    response = client.get(f"/api/runs/{run['id']}/events", params={"after": sequence - 1})
    assert response.status_code == 200, response.text
    event = response.json()["items"][0]
    assert {key: event[key] for key in expected} == expected
    assert datetime.fromisoformat(event["timestamp"]).astimezone(UTC) == expected_time.replace(tzinfo=UTC)
    assert response.json()["event_cutoff"] == response.json()["next_cursor"] == sequence
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["events"][-1] == event
    stream = client.get(f"/api/runs/{run['id']}/stream", params={"after": sequence - 1})
    assert stream.status_code == 200
    lines = stream.text.splitlines()
    assert f"id: {run['id']}:{sequence}" in lines
    streamed = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
    assert {key: streamed[key] for key in expected} == expected
    assert datetime.fromisoformat(streamed["timestamp"]).astimezone(UTC) == expected_time.replace(tzinfo=UTC)
