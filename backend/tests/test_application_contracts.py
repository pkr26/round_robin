from copy import deepcopy

from sqlalchemy import select

from backend.tests.test_api import application, client, command, create, start
from backend.tests.test_orchestration import drain
from round_robin.database.models import ReportVersion, Run
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.schemas.runtime import RuntimeDecisionReport
from round_robin.schemas.views import EventPage, MeetingView, Snapshot


def test_persisted_runtime_snapshot_and_report_use_authoritative_application_types(client, application):
    meeting = create(client)
    run = start(client, meeting).json()
    drain(application.state.session_factory)
    response = client.get(f"/api/runs/{run['id']}/snapshot")
    assert response.status_code == 200, response.text
    saved = Snapshot.model_validate(response.json())
    assert isinstance(saved.report, RuntimeDecisionReport)
    assert saved.report.simulated and saved.report.readiness != "ready_to_choose"
    assert saved.report.run_id == saved.run.id
    assert saved.report.usage.request_count == 6
    assert all(item.run_id == saved.run.id for item in saved.evidence)
    latest = client.get(f"/api/runs/{run['id']}/report")
    version = client.get(f"/api/runs/{run['id']}/reports/{saved.report.version}")
    assert latest.status_code == version.status_code == 200
    assert latest.json() == version.json() == saved.report.model_dump(mode="json")
    events = EventPage.model_validate(client.get(f"/api/runs/{run['id']}/events").json())
    assert [event.sequence for event in events.items] == sorted({event.sequence for event in events.items})
    restored = MeetingView.model_validate(client.get(f"/api/meetings/{meeting['id']}").json())
    assert restored.runs[0].report_version == saved.report.version


def test_historical_runtime_scope_adaptation_does_not_modify_saved_payload(client, application):
    meeting = create(client)
    run = start(client, meeting).json()
    drain(application.state.session_factory)
    with application.state.session_factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run["id"]))
        historical = deepcopy(row.content)
        historical.pop("evidence_ids")
        historical.pop("contribution_ids")
        row.content = historical
    response = client.get(f"/api/runs/{run['id']}/report")
    assert response.status_code == 200, response.text
    restored = RuntimeDecisionReport.model_validate(response.json())
    assert restored.evidence_ids and restored.contribution_ids
    with application.state.session_factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run["id"]))
        assert "evidence_ids" not in row.content
        assert "contribution_ids" not in row.content


def test_cancel_before_first_provider_preserves_typed_unavailable_partial(client, application):
    meeting = create(client)
    run = start(client, meeting).json()
    cancelled = client.post(f"/api/runs/{run['id']}/cancel", json=command(expected_run_version=run["version"]))
    assert cancelled.status_code == 200
    with application.state.session_factory.begin() as session:
        row = lock_work_run(session, run["id"])
        advance_run(session, row)
    response = client.get(f"/api/runs/{run['id']}/snapshot")
    assert response.status_code == 200, response.text
    saved = Snapshot.model_validate(response.json())
    assert saved.run.status == "cancelled"
    assert saved.report.execution_status == "cancelled"
    assert saved.report.recommendation is None and saved.report.readiness is None
    assert saved.report.usage.request_count == 0
