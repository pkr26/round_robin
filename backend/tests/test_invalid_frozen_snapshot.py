"""Saved recovery state stays readable without exposing invalid run inputs."""

import pytest
from round_robin.database.models import EvidenceRecord
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import finalize_run

pytest_plugins = ("backend.tests.test_artifacts_api",)


@pytest.mark.parametrize("corrupt", ["original", "resolved", "both"])
def test_snapshot_exposes_partial_report_without_invalid_brief(artifacts, corrupt):
    app, client, _, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.status = "running"
        run.configuration = {**run.configuration, "resolved_brief": dict(run.input_snapshot)}
        if corrupt in {"original", "both"}:
            run.input_snapshot = {**run.input_snapshot, "horizon_months": "synthetic-invalid-secret"}
        if corrupt in {"resolved", "both"}:
            run.configuration = {**run.configuration, "resolved_brief": {**run.configuration["resolved_brief"], "horizon_months": "synthetic-invalid-secret"}}
        finalize_run(session, run)
        assert run.status == "failed"

    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["brief"] is None
    assert body["run"]["status"] == "failed"
    assert body["report"]["readiness"] is None and body["report"]["comparison"] == []
    assert body["retry_eligible"] is False and body["retryable_stages"] == []
    assert "synthetic-invalid-secret" not in response.text


def test_snapshot_keeps_valid_saved_brief(artifacts):
    app, client, _, run_id, _ = artifacts
    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 200, response.text
    with app.state.session_factory() as session:
        run = lock_work_run(session, run_id)
        assert response.json()["brief"] == run.configuration.get("resolved_brief", run.input_snapshot)


def test_snapshot_does_not_expose_corruption_before_finalization(artifacts):
    app, client, _, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.status = "running"
        run.configuration = {**run.configuration, "resolved_brief": {**run.input_snapshot, "horizon_months": "synthetic-invalid-secret"}}
    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 200, response.text
    assert response.json()["brief"] is None
    assert "synthetic-invalid-secret" not in response.text


def test_existing_report_remains_a_saved_record_if_brief_later_becomes_invalid(artifacts):
    app, client, _, run_id, saved = artifacts
    with app.state.session_factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.configuration = {**run.configuration, "resolved_brief": {**run.input_snapshot, "horizon_months": "synthetic-invalid-secret"}}
    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["brief"] is None and body["report"]["id"] == saved["id"]
    assert body["run"]["status"] == "completed" and body["retry_eligible"] is False
    assert "synthetic-invalid-secret" not in response.text


def test_cancelled_partial_snapshot_explicitly_marks_unreadable_evidence(artifacts):
    app, client, meeting_id, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.status = "cancel_requested"
        run.input_snapshot = {**run.input_snapshot, "horizon_months": "synthetic-invalid-secret"}
        session.add(EvidenceRecord(id="evidence_bad", run_id=run_id, meeting_id=meeting_id, owner_id=run.owner_id,
            content={"private": "synthetic-bad-evidence"}, sha256="0" * 64))
        session.flush()
        finalize_run(session, run)
        assert run.status == "cancelled" and run.stop_reason == "user_cancellation"
    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["brief"] is None and body["evidence"] is None
    assert body["report"]["execution_status"] == "cancelled"
    assert body["report"]["evidence_ids"] == [] and body["report"]["comparison"] == []
    assert "Stored evidence could not be validated and is unavailable in this report." in body["report"]["main_reasons"]
    assert "synthetic-invalid-secret" not in response.text and "synthetic-bad-evidence" not in response.text
    with app.state.session_factory() as session:
        assert session.get(EvidenceRecord, "evidence_bad").content == {"private": "synthetic-bad-evidence"}
    with app.state.session_factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.status = "failed"
    stale = client.get(f"/api/runs/{run_id}/snapshot")
    assert stale.status_code == 503
    assert "synthetic-bad-evidence" not in stale.text


@pytest.mark.parametrize("forged_marker", [False, True])
def test_unrecognized_evidence_corruption_remains_fail_closed(artifacts, forged_marker):
    app, client, meeting_id, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        run = lock_work_run(session, run_id)
        if forged_marker:
            run.configuration = {**run.configuration, "invalid_frozen_inputs": True, "comparison_unavailable_reason": "evidence_integrity_failed"}
        session.add(EvidenceRecord(id="evidence_bad", run_id=run_id, meeting_id=meeting_id, owner_id=run.owner_id,
            content={"private": "synthetic-bad-evidence"}, sha256="0" * 64))
    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "saved_result_unavailable"
    assert "synthetic-bad-evidence" not in response.text
