"""A regressed clock must stop execution without preventing durable cancellation."""
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.tests.test_api import application, client, command, create, start
from backend.tests.test_orchestration import factory, seed
from round_robin.database.models import Attempt, BudgetOperation, ReportVersion, Run, Step, utcnow
from round_robin.dispatcher.service import reconcile_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import BudgetDenied, authorize_operation, remaining_active
from round_robin.orchestration.controller import advance_run
from round_robin.orchestration.policies import retryable_steps


def test_cancel_with_a_future_active_clock_publishes_without_waiting_for_clock_recovery(client, application):
    accepted = start(client, create(client)).json()
    with application.state.session_factory.begin() as session:
        run = lock_work_run(session, accepted["id"])
        advance_run(session, run)
        run.active_seconds = 17.25
        run.active_started_at = utcnow() + timedelta(hours=1)
        version = run.version
    cancelled = client.post(f"/api/runs/{accepted['id']}/cancel", json=command(expected_run_version=version))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancel_requested"
    reconcile_once(session_factory=application.state.session_factory)
    with application.state.session_factory() as session:
        run = session.get(Run, accepted["id"])
        assert run.status == "cancelled" and run.active_started_at is None
        assert run.active_seconds == 17.25
        assert run.configuration["active_clock_invalid"] is True
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run.id))
        assert report.content["execution_status"] == "cancelled"
        assert report.content["usage"]["request_count"] == 0
        assert session.scalar(select(Attempt)) is None
        assert session.get(BudgetOperation, f"finish_{run.id}").status == "released"
    assert client.get(f"/api/runs/{accepted['id']}/report").status_code == 200


@pytest.mark.parametrize("entrypoint", ["advance", "dispatcher"])
def test_clock_regression_settles_run_preserves_unknown_exposure_and_permanently_denies_retry(factory, monkeypatch, entrypoint):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        authorize_operation(session, run, "attempt_clock_unknown", "model", "0", step=step)
        step.stage, run.stage = "synthesis", "synthesis"
        step.status, step.error_code, step.infrastructure_claims = "failed", "timeout", 1
        session.add(Attempt(id="attempt_clock_unknown", run_id=run_id, step_id=step.id, provider="openai", status="authorized", reserved_usd="0", configuration={"execution_fence": run.execution_fence}))
        run.active_seconds, run.active_started_at = 29.5, utcnow() + timedelta(hours=1)
    if entrypoint == "advance":
        with factory.begin() as session:
            advance_run(session, lock_work_run(session, run_id))
    else:
        reconcile_once(session_factory=factory)
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == "failed" and run.stop_reason == "clock_regression"
        assert run.configuration["active_clock_invalid"] is True
        assert run.active_started_at is None and run.active_seconds == 29.5
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content
        assert report["usage"]["status"] == "unknown" and report["usage"]["actual_cost"] is None
        assert report["usage"]["unknown_operation_ids"] == ["attempt_clock_unknown"]
        assert session.get(BudgetOperation, "attempt_clock_unknown").status == "reserved"
        assert session.get(BudgetOperation, f"finish_{run_id}").status == "released"
        assert len(list(session.scalars(select(Attempt)))) == 1
        # Wall-clock catch-up cannot renew authority or manually revive this run.
        monkeypatch.setattr("round_robin.orchestration.budget.utcnow", lambda: utcnow() + timedelta(hours=2))
        with pytest.raises(BudgetDenied, match="clock_regression"):
            remaining_active(run)
        assert retryable_steps(session, run) == []
