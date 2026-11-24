"""Corrupt frozen inputs fence late content without erasing payment uncertainty."""

import pytest
from round_robin.database.models import Attempt, BudgetOperation, ReportVersion, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import seed

pytest_plugins = ("backend.tests.test_orchestration",)


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("may_have_sent", [False, True])
@pytest.mark.parametrize("corrupt", ["original", "resolved"])
def test_inflight_failure_preserves_uncertainty_with_corrupt_inputs(factory, cancelled, may_have_sent, corrupt):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        intake_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
    execute_step(intake_id, session_factory=factory)
    with factory() as session:
        proposal_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "proposal"))

    class CorruptAndFail(SimulatedProvider):
        def generate(self, request):
            with factory.begin() as session:
                run = lock_work_run(session, run_id)
                if corrupt == "original":
                    run.input_snapshot = {**run.input_snapshot, "horizon_months": "synthetic-invalid-secret"}
                else:
                    run.configuration = {**run.configuration, "resolved_brief": {**run.configuration["resolved_brief"], "horizon_months": "synthetic-invalid-secret"}}
                if cancelled:
                    run.status = "cancel_requested"
            raise ProviderFailure("timeout", may_have_sent=may_have_sent)

    registry = {provider: CorruptAndFail(provider) for provider in ("openai", "anthropic", "google")}
    result = execute_step(proposal_id, session_factory=factory, adapter_registry=registry)
    assert result["status"] == "late_result_fenced"
    with factory() as session:
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == proposal_id))
        operation = session.get(BudgetOperation, attempt.id)
        report = read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)))
        assert report["execution_status"] == ("cancelled" if cancelled else "failed")
        assert report["stop_reason"] == ("user_cancellation" if cancelled else "invalid_frozen_inputs")
        assert attempt.usage_certainty == ("unknown" if may_have_sent else "proven_not_sent")
        assert attempt.response_json["content_discarded"] is True
        assert operation.status == ("unknown" if may_have_sent else "settled")
        assert report["usage"]["status"] == ("unknown" if may_have_sent else "reported")
        assert (attempt.id in report["usage"]["unknown_operation_ids"]) == may_have_sent
        assert report["comparison"] == [] and report["readiness"] is None
        assert "synthetic-invalid-secret" not in str(report)
