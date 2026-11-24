"""Actual source reads reject corruption without changing immutable records."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.tests.test_artifacts_api import artifacts
from round_robin.database.models import Attempt, BudgetOperation, Event, EvidenceRecord, ExportRecord, ReportVersion, Run
from round_robin.orchestration.budget import BudgetDenied
from round_robin.orchestration.controller import validated_evidence


MUTATIONS = ["text", "sha256", "id", "owner_id", "run_id", "row_sha256", "row_meeting_id"]


def corrupt(factory, field):
    with factory.begin() as session:
        row = session.get(EvidenceRecord, "passage_offer_a")
        content = deepcopy(row.content)
        if field == "row_sha256":
            row.sha256 = "b" * 64
        elif field == "row_meeting_id":
            row.meeting_id = "meeting_unrelated"
        else:
            content[field] = {"text": "PRIVATE_CORRUPTED_EXCERPT_SENTINEL", "sha256": "b" * 64, "id": "evidence_foreign", "owner_id": "owner_foreign", "run_id": "run_foreign"}[field]
        row.content = content
        return content, row.sha256, row.meeting_id


@pytest.mark.parametrize("field", MUTATIONS)
def test_http_sources_and_saved_report_reject_exact_excerpt_or_scope_corruption(artifacts, field):
    app, client, _, run_id, report = artifacts
    saved = corrupt(app.state.session_factory, field)
    for endpoint in (f"/api/runs/{run_id}/sources", f"/api/runs/{run_id}/sources/passage_offer_a", f"/api/runs/{run_id}/report"):
        response = client.get(endpoint)
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "saved_result_unavailable"
        assert "PRIVATE_CORRUPTED_EXCERPT_SENTINEL" not in response.text
    exported = client.post(f"/api/runs/{run_id}/exports", json={"command_id": str(uuid4()), "report_id": report["id"], "format": "markdown"})
    assert exported.status_code == 409 and exported.json()["error"]["code"] == "report_invalid"
    with app.state.session_factory() as session:
        row = session.get(EvidenceRecord, "passage_offer_a")
        assert (row.content, row.sha256, row.meeting_id) == saved
        assert session.get(ReportVersion, report["id"]).content == report
        assert not list(session.scalars(select(ExportRecord)))
        assert not list(session.scalars(select(Attempt)))
        assert not list(session.scalars(select(BudgetOperation)))


@pytest.mark.parametrize("field", MUTATIONS)
def test_provider_evidence_packet_uses_the_same_exact_scoped_read_gate(artifacts, field):
    app, _, _, run_id, _ = artifacts
    corrupt(app.state.session_factory, field)
    with app.state.session_factory() as session:
        with pytest.raises(BudgetDenied, match="evidence_integrity_failed"):
            validated_evidence(session, session.get(Run, run_id))


@pytest.mark.parametrize("offset", [-1, 1])
def test_report_reader_preserves_original_cutoff_and_ignores_unreferenced_corrupt_rows(artifacts, offset):
    app, client, meeting_id, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        saved = session.get(ReportVersion, report["id"])
        session.add(EvidenceRecord(id="evidence_unreferenced", run_id=run_id, meeting_id=meeting_id, owner_id="owner_local", sha256="f" * 64, content={"text": "Invalid but not referenced by this report"}, created_at=saved.created_at + timedelta(seconds=offset)))
    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 200 and response.json() == report


@pytest.mark.parametrize("payload", [[], {"step_id": []}, {"step_id": ""}, {"step_id": "   "}, {"step_id": 123}, {"step_id": True}])
def test_malformed_committed_event_keeps_run_readable_and_saved_assessment_unavailable(artifacts, payload):
    app, client, meeting_id, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        event = Event(id="event_" + uuid4().hex, run_id=run_id, sequence=2, event_type="content.committed", payload=payload)
        session.add(event)
        event_id = event.id
        session.get(Run, run_id).event_sequence = 2
    for path in (f"/api/runs/{run_id}/snapshot", f"/api/runs/{run_id}/report", f"/api/runs/{run_id}/reports/1"):
        response = client.get(path)
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "saved_result_unavailable"
    run = client.get(f"/api/runs/{run_id}")
    assert run.status_code == 200 and run.json()["status"] == "completed" and run.json()["readiness"] is None
    meeting = client.get(f"/api/meetings/{meeting_id}")
    assert meeting.status_code == 200 and meeting.json()["runs"][0]["readiness"] is None
    with app.state.session_factory() as session:
        assert session.get(Event, event_id).payload == payload
        assert session.get(ReportVersion, report["id"]).content == report
        assert not list(session.scalars(select(Attempt)))
        assert not list(session.scalars(select(BudgetOperation)))


@pytest.mark.parametrize("payload", [{}, {"step_id": None}])
def test_historical_committed_event_without_step_identity_retains_prior_ignored_semantics(artifacts, payload):
    app, client, _, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        session.add(Event(id="event_" + uuid4().hex, run_id=run_id, sequence=2, event_type="content.committed", payload=payload))
    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 200 and response.json() == report
