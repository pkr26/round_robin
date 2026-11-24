"""Malformed stored report bytes must be unavailable without losing run state."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from round_robin.database.models import ReportVersion, Run
from sqlalchemy import text

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


@pytest.mark.parametrize("raw", ['{"private":"PRIVATE_REPORT_SENTINEL",', '[' * 10000 + '0' + ']' * 10000])
def test_invalid_report_json_is_sanitized_and_run_is_inspectable(artifacts, raw):
    app, client, _, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        session.execute(text("UPDATE report_versions SET content=:raw WHERE id=:identity"), {"raw": raw, "identity": report["id"]})
    original = client._transport.raise_server_exceptions
    client._transport.raise_server_exceptions = False
    try:
        for path in (f"/api/runs/{run_id}/snapshot", f"/api/runs/{run_id}/report", f"/api/runs/{run_id}/reports/1"):
            response = client.get(path)
            assert response.status_code == 503, (path, response.status_code, response.text)
            assert response.json()["error"]["code"] == "saved_result_unavailable"
            assert "PRIVATE_REPORT_SENTINEL" not in response.text
        run = client.get(f"/api/runs/{run_id}")
        assert run.status_code == 200 and run.json()["readiness"] is None
        meeting = client.get(f"/api/meetings/{run.json()['meeting_id']}")
        assert meeting.status_code == 200 and meeting.json()["runs"][0]["readiness"] is None
    finally:
        client._transport.raise_server_exceptions = original
    with app.state.session_factory() as session:
        assert session.scalar(text("SELECT content FROM report_versions WHERE id=:identity"), {"identity": report["id"]}) == raw


def test_unavailable_ledger_ignores_corrupt_current_report_but_history_is_sanitized(artifacts):
    app, client, _, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "ledger_integrity_failed": True}
        session.execute(text("UPDATE report_versions SET content=:raw WHERE id=:id"), {"raw": '{"private":"PRIVATE_REPORT_SENTINEL",', "id": report["id"]})
    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"] is None and body["run"]["readiness"] is None
    assert body["run"]["remaining_limits"]["requests"] is None
    current = client.get(f"/api/runs/{run_id}/report")
    assert current.status_code == 503 and current.json()["error"]["code"] == "ledger_integrity_failed"
    historical = client.get(f"/api/runs/{run_id}/reports/1")
    assert historical.status_code == 503 and historical.json()["error"]["code"] == "saved_result_unavailable"
    assert "PRIVATE_REPORT_SENTINEL" not in historical.text


def test_corrupt_old_report_does_not_poison_latest_report_or_its_export(artifacts):
    app, client, _, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        original = session.get(ReportVersion, report["id"])
        content = deepcopy(original.content)
        content.update(id="report_newer", version=2)
        session.add(ReportVersion(id="report_newer", run_id=run_id, version=2, event_cutoff=original.event_cutoff, content=content, created_at=original.created_at + timedelta(seconds=1)))
        session.get(Run, run_id).report_version = 2
        session.execute(text("UPDATE report_versions SET content=:raw WHERE id=:id"), {"raw": '{"private":"PRIVATE_REPORT_SENTINEL",', "id": report["id"]})
    current = client.get(f"/api/runs/{run_id}/report")
    assert current.status_code == 200 and current.json()["id"] == "report_newer"
    assert client.get(f"/api/runs/{run_id}/reports/1").status_code == 503
    assert client.get(f"/api/runs/{run_id}/reports/2").status_code == 200
    calculation = f"/api/runs/{run_id}/calculations/{report['calculations'][0]['id']}"
    assert client.get(calculation).json()["report_id"] == "report_newer"
    assert client.get(calculation, params={"report_id": report["id"]}).status_code == 409
    created = client.post(f"/api/runs/{run_id}/exports", json={"command_id": str(uuid4()), "report_id": "report_newer", "format": "json"})
    assert created.status_code == 200 and created.json()["report_id"] == "report_newer"
