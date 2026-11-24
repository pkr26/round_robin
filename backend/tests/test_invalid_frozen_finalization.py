import pytest
from round_robin.database.models import (
    Attempt,
    BudgetOperation,
    EvidenceRecord,
    ReportVersion,
    Run,
    Step,
)
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.orchestration.policies import retryable_steps
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.schemas.runtime import RuntimeDecisionReport
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("valid_resolved", [False, True])
def test_corrupt_frozen_input_publishes_honest_partial_report_and_keeps_exposure(factory, cancelled, valid_resolved):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.stage = "proposals"
        run.status = "cancel_requested" if cancelled else "running"
        if valid_resolved:
            run.configuration = {**run.configuration, "resolved_brief": run.input_snapshot}
        run.input_snapshot = {**run.input_snapshot, "horizon_months": "private-unparseable-input"}
        session.add(BudgetOperation(id="operation_unknown", run_id=run_id, owner_id=run.owner_id,
            kind="model", status="unknown", reserved_usd="0.42", metadata_json={"model_call": True}))
        session.add(Step(id="step_pending", run_id=run_id, stage="proposal", role_slot_id="provider_one",
            provider="openai", status="pending", lease_owner="worker"))
        session.flush()

        advance_run(session, run)

        assert run.status == ("cancelled" if cancelled else "failed")
        assert run.stop_reason == ("user_cancellation" if cancelled else "invalid_frozen_inputs")
        assert run.stage == "finished"
        assert session.get(Step, "step_pending").status == "skipped"
        assert session.get(Step, "step_pending").lease_owner is None
        assert session.get(BudgetOperation, "operation_unknown").status == "unknown"
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content
        RuntimeDecisionReport.model_validate(report)
        assert report["execution_status"] == run.status
        assert report["recommendation"] is None and report["readiness"] is None
        assert report["comparison"] == []
        assert report["usage"]["unknown_operation_ids"] == ["operation_unknown"]
        assert report["usage"]["actual_cost"] is None
        assert report["usage"]["conservative_exposure_usd"] == "0.42"
        assert "private-unparseable-input" not in str(report)
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        readable = read_runtime_report(session, row)
        assert readable["readiness"] is None
        assert readable["usage"]["conservative_exposure_usd"] == "0.42"
        assert "private-unparseable-input" not in str(readable)
        advance_run(session, lock_work_run(session, run_id))
        assert len(list(session.scalars(select(ReportVersion).where(ReportVersion.run_id == run_id)))) == 1


@pytest.mark.parametrize("corrupt_input,corrupt_resolved", [(True, False), (False, True), (True, True)])
def test_partial_report_keeps_committed_evidence_and_contribution_scope(factory, corrupt_input, corrupt_resolved):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        intake = session.scalar(select(Step).where(Step.run_id == run_id, Step.stage == "intake"))
        step_id = intake.id
    execute_step(step_id, session_factory=factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        assert run.configuration["evidence_prepared"] is True
        if corrupt_input:
            run.input_snapshot = {**run.input_snapshot, "horizon_months": "private-unparseable-input"}
        if corrupt_resolved:
            run.configuration = {**run.configuration, "resolved_brief": {**run.configuration["resolved_brief"], "horizon_months": "private-unparseable-input"}}
        if corrupt_input:
            advance_run(session, run)
        else:
            from round_robin.orchestration.controller import finalize_run
            finalize_run(session, run, reason="invalid_frozen_inputs")
        assert run.status == "failed"
        assert run.stop_reason == "invalid_frozen_inputs"
        assert retryable_steps(session, run) == []
    with factory() as session:
        report = read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)))
        assert report["contribution_ids"] == [step_id]
        assert report["evidence_ids"]
        assert report["comparison"] == []


def test_invalid_inputs_do_not_offer_retry_or_keep_unused_finish_hold(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.stage = "proposals"
        run.status = "running"
        run.configuration = {**run.configuration, "mode": "live", "funded": True, "finish_policy": {**run.configuration["finish_policy"], "reserved_usd": "0.40"}}
        run.input_snapshot = {**run.input_snapshot, "horizon_months": "invalid"}
        session.add(Step(id="step_failed_audit", run_id=run_id, stage="audit", role_slot_id="audit",
            provider="google", status="failed", error_code="timeout"))
        session.add(BudgetOperation(id=f"finish_{run_id}", run_id=run_id, owner_id=run.owner_id,
            kind="finish_hold", status="held", reserved_usd="0.40", metadata_json={"roles_remaining": ["audit"], "initial_reserved_usd": "0.40"}))
        session.flush()
        advance_run(session, run)
        assert run.status == "failed"
        assert run.stop_reason == "invalid_frozen_inputs"
        assert retryable_steps(session, run) == []
        assert session.get(BudgetOperation, f"finish_{run_id}").status == "released"


@pytest.mark.parametrize("corrupt", ["input", "resolved"])
def test_worker_fences_corrupt_frozen_input_before_authorization(factory, corrupt):
    class ProviderSpy(SimulatedProvider):
        def conservative_bound(self, request):
            raise AssertionError("must not evaluate or call a provider")

    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        intake = session.scalar(select(Step).where(Step.run_id == run_id, Step.stage == "intake"))
        intake_id = intake.id
    execute_step(intake_id, session_factory=factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        assert run.configuration["evidence_prepared"] is True
        if corrupt == "input":
            run.input_snapshot = {**run.input_snapshot, "horizon_months": "private-unparseable-input"}
        else:
            run.configuration = {**run.configuration, "resolved_brief": {**run.configuration["resolved_brief"], "horizon_months": "private-unparseable-input"}}
        proposal_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "proposal"))
        prior_attempts = set(session.scalars(select(Attempt.id).where(Attempt.run_id == run_id)))
        prior_operations = set(session.scalars(select(BudgetOperation.id).where(BudgetOperation.run_id == run_id)))
    registry = {provider: ProviderSpy(provider) for provider in ("openai", "anthropic", "google")}
    result = execute_step(proposal_id, session_factory=factory, adapter_registry=registry)
    assert result == {"status": "policy_blocked", "code": "invalid_frozen_inputs"}
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == "failed" and run.stop_reason == "invalid_frozen_inputs"
        assert set(session.scalars(select(Attempt.id).where(Attempt.run_id == run_id))) == prior_attempts
        assert set(session.scalars(select(BudgetOperation.id).where(BudgetOperation.run_id == run_id))) == prior_operations
        report = read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)))
        assert report["comparison"] == [] and report["readiness"] is None
        assert retryable_steps(session, run) == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_corruption_during_provider_call_fences_content_and_reconciles_usage(factory, cancelled):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        intake_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
    execute_step(intake_id, session_factory=factory)
    with factory() as session:
        proposal_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "proposal"))

    class CorruptingProvider(SimulatedProvider):
        def generate(self, request):
            with factory.begin() as session:
                run = lock_work_run(session, run_id)
                run.input_snapshot = {**run.input_snapshot, "horizon_months": "private-unparseable-input"}
                if cancelled:
                    run.status = "cancel_requested"
            return super().generate(request)

    registry = {provider: CorruptingProvider(provider) for provider in ("openai", "anthropic", "google")}
    assert execute_step(proposal_id, session_factory=factory, adapter_registry=registry)["status"] == "late_result_fenced"
    with factory() as session:
        run = session.get(Run, run_id)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == proposal_id))
        assert run.status == ("cancelled" if cancelled else "failed")
        assert run.stop_reason == ("user_cancellation" if cancelled else "invalid_frozen_inputs")
        assert attempt.usage_certainty == "reported"
        assert attempt.response_json["content_discarded"] is True
        report = read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)))
        assert report["comparison"] == [] and report["usage"]["actual_cost"] == "0"


def test_cancelled_worker_with_corrupt_inputs_preserves_cancellation(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
        run = session.get(Run, run_id)
        run.input_snapshot = {**run.input_snapshot, "horizon_months": "private-unparseable-input"}
        run.status = "cancel_requested"
    execute_step(step_id, session_factory=factory)
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == "cancelled" and run.stop_reason == "user_cancellation"
        assert session.get(Step, step_id).status == "skipped"
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None


def test_cancelled_invalid_inputs_with_unreadable_evidence_keep_reason_and_partial_report(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.status = "cancel_requested"
        run.input_snapshot = {**run.input_snapshot, "horizon_months": "invalid"}
        session.add(EvidenceRecord(id="evidence_bad", run_id=run.id, meeting_id=run.meeting_id,
            owner_id=run.owner_id, content={}, sha256="0" * 64))
        session.flush()
        advance_run(session, run)
        assert run.status == "cancelled" and run.stop_reason == "user_cancellation"
        assert run.configuration["comparison_unavailable_reason"] == "evidence_integrity_failed"
    with factory() as session:
        run = session.get(Run, run_id)
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        report = read_runtime_report(session, row)
        assert report["stop_reason"] == "user_cancellation"
        assert report["evidence_ids"] == []
        assert "Stored evidence could not be validated and is unavailable in this report." in report["main_reasons"]
        assert session.get(EvidenceRecord, "evidence_bad").content == {}
        assert run.configuration["invalid_frozen_inputs"] is True


@pytest.mark.parametrize("corrupt", ["input", "resolved"])
def test_corruption_after_finalization_revokes_visible_retry(factory, corrupt):
    from decimal import Decimal

    from round_robin.database.models import Event
    from round_robin.providers.base import ProviderFailure

    from backend.tests.test_orchestration import drain

    class AuditTimeout(SimulatedProvider):
        def generate(self, request):
            if request.stage == "audit":
                raise ProviderFailure("timeout", retryable=False, cost_usd=Decimal(0))
            return super().generate(request)

    run_id = seed(factory)
    drain(factory, {name: AuditTimeout(name) for name in ("openai", "anthropic", "google")})
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        assert run.status == "completed_with_limitations"
        assert session.scalar(select(ReportVersion.id).where(ReportVersion.run_id == run_id))
        step = session.scalar(select(Step).where(Step.run_id == run_id, Step.stage == "audit"))
        assert step.status == "failed" and step.error_code == "timeout"
        committed = session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type == "content.committed"))
        assert not any(event.payload.get("step_id") == step.id for event in committed)
        assert retryable_steps(session, run) == [step]
        if corrupt == "input":
            run.input_snapshot = {**run.input_snapshot, "horizon_months": "private-unparseable-input"}
        else:
            run.configuration = {**run.configuration, "resolved_brief": {**run.configuration["resolved_brief"], "horizon_months": "private-unparseable-input"}}
        assert retryable_steps(session, run) == []
