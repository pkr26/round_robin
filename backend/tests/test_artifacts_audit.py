"""Saved calculation reads must enforce the saved report's integrity boundary."""
import pytest

from backend.tests.test_artifacts_api import artifacts
from round_robin.database.models import ReportVersion


@pytest.mark.parametrize("mutation", [
    {"readiness": "ready_to_choose"},
    {"evidence_ids": ["evidence_foreign_run"]},
    {"event_cutoff": -1},
])
def test_calculation_inspection_rejects_invalid_saved_report(artifacts, mutation):
    app, client, _, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        row = session.get(ReportVersion, report["id"])
        row.content = {**row.content, **mutation}
    response = client.get(f"/api/runs/{run_id}/calculations/{report['calculations'][0]['id']}?report_id={report['id']}")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "report_invalid"


def test_unsupported_pdf_text_saves_specific_terminal_export_error(artifacts):
    from uuid import uuid4
    from round_robin.database.models import Run
    app, client, meeting_id, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        snapshot = dict(run.input_snapshot)
        brief = dict(snapshot.get("brief", snapshot))
        brief["title"] = "中文 report"
        run.input_snapshot = {**snapshot, "brief": brief} if "brief" in snapshot else brief
    response = client.post(f"/api/runs/{run_id}/exports", json={
        "command_id": str(uuid4()), "report_id": report["id"], "format": "pdf",
    })
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == "pdf_font_unsupported"
    assert response.json()["download_url"] is None
