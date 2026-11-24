"""The run API reports the remaining active-time allowance conservatively."""

from datetime import timedelta

from backend.tests.test_api import application as application
from backend.tests.test_api import client as client
from backend.tests.test_api import create, start
from round_robin.api import events
from round_robin.database.models import Run, utcnow


def test_run_reports_active_time_and_fails_closed_for_invalid_clock(client, application, monkeypatch):
    meeting = create(client)
    run_id = start(client, meeting).json()["id"]
    now = utcnow()
    monkeypatch.setattr(events, "utcnow", lambda: now)

    with application.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "limits": {**run.configuration.get("limits", {}), "active_seconds": 120}}
        run.active_seconds = 20
        run.active_started_at = now - timedelta(seconds=15)

    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    assert response.json()["active_seconds"] == 35
    assert response.json()["remaining_limits"]["active_seconds"] == 85

    with application.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        run.active_started_at = now + timedelta(seconds=30)

    assert client.get(f"/api/runs/{run_id}").json()["remaining_limits"]["active_seconds"] == 100

    with application.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "active_clock_invalid": True}

    assert client.get(f"/api/runs/{run_id}").json()["remaining_limits"]["active_seconds"] == 0
