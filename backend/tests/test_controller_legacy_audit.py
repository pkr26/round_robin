"""Audit decisions must also handle saved runs predating the issue ledger."""

from dataclasses import replace

import pytest
from round_robin.database.models import Event, ReportVersion, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.research.claims import ClaimRejected
from round_robin.research.discussion import discussion_issues
from round_robin.worker.tasks import execute_step
from sqlalchemy import delete, select

from backend.tests.test_orchestration import elapse_retry_wait, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def _prepare_saved_audit(factory, monkeypatch, *, critical_defects):
    class AuditProvider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            if request.stage == "audit":
                output = result.output.model_copy(
                    update={"audit_passed": True, "critical_defects": critical_defects}
                )
                return replace(result, output=output)
            return result

    run_id = seed(factory)
    registry = {provider: AuditProvider(provider) for provider in ("openai", "anthropic", "google")}
    for _ in range(30):
        with factory() as session:
            pending = list(session.scalars(select(Step.id).where(Step.run_id == run_id, Step.status == "pending")))
        with factory.begin() as session:
            advance_run(session, lock_work_run(session, run_id))
        for step_id in pending:
            elapse_retry_wait(factory, step_id)
            with factory() as session:
                audit = session.get(Step, step_id).stage == "audit"
            if audit:
                # Old saved contributions may have no materialized issue ledger.
                # Stop only the immediate continuation after the real worker
                # commits the audited output, then simulate that legacy state.
                with monkeypatch.context() as patch:
                    patch.setattr("round_robin.worker.tasks.advance_run", lambda session, run: None)
                    execute_step(step_id, session_factory=factory, adapter_registry=registry)
                with factory.begin() as session:
                    run = lock_work_run(session, run_id)
                    run.configuration = {key: value for key, value in run.configuration.items() if key not in {"discussion_issues", "discussion_policy_version"}}
                    session.execute(delete(Event).where(Event.run_id == run_id, Event.event_type == "discussion.obligation_recorded"))
                return run_id
            execute_step(step_id, session_factory=factory, adapter_registry=registry)
    raise AssertionError("audit did not become ready")


def test_legacy_run_without_obligations_can_complete(factory, monkeypatch):
    run_id = _prepare_saved_audit(factory, monkeypatch, critical_defects=[])
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        assert run.status == "completed"
        assert run.stop_reason == "protocol_complete"


def test_legacy_positive_audit_with_critical_defect_is_not_certified(factory, monkeypatch):
    run_id = _prepare_saved_audit(factory, monkeypatch, critical_defects=["The documented delivery date is unsupported."])
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        assert run.status == "failed"
        assert run.stop_reason == "discussion_integrity_failed"


def test_legacy_critical_defect_cannot_be_reported_as_completed_when_correction_unavailable(factory, monkeypatch):
    run_id = _prepare_saved_audit(factory, monkeypatch, critical_defects=["The documented delivery date is unsupported."])
    monkeypatch.setattr("round_robin.orchestration.controller._reserve_correction", lambda session, run: False)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        assert run.status == "failed"
        assert run.stop_reason == "discussion_integrity_failed"


def test_open_material_objection_triggers_correction_even_when_audit_passes(factory, monkeypatch):
    from backend.tests.test_discussion_integrity import prepare_resolved_issue
    run_id = prepare_resolved_issue(factory, monkeypatch, target_status="open")
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        assert run.status == "running"
        assert run.stage == "synthesis"
        assert run.configuration["stage_iteration"] == 1


def test_invalid_saved_input_error_does_not_expose_validation_details(factory, monkeypatch):
    run_id = seed(factory)

    def reject_invalid_input(session, run):
        raise ValueError("PRIVATE_INPUT_DIAGNOSTIC_SENTINEL")

    monkeypatch.setattr("round_robin.orchestration.controller._ensure_steps", reject_invalid_input)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        assert run.status == "failed"
        assert run.stop_reason == "invalid_frozen_inputs"
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        events = list(session.scalars(select(Event).where(Event.run_id == run_id)))
        assert "PRIVATE_INPUT_DIAGNOSTIC_SENTINEL" not in str(report.content)
        assert all("PRIVATE_INPUT_DIAGNOSTIC_SENTINEL" not in str(event.payload) for event in events)


def test_dictionary_issue_ledger_is_retained_when_audit_finalizes(factory, monkeypatch):
    from backend.tests.test_discussion_integrity import prepare_resolved_issue
    run_id = prepare_resolved_issue(factory, monkeypatch, target_status="open")
    monkeypatch.setattr("round_robin.orchestration.controller._reserve_correction", lambda session, run: False)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        issues = run.configuration["discussion_issues"]
        run.configuration = {**run.configuration, "discussion_issues": {issue["id"]: issue for issue in issues}}

        advance_run(session, run)

        assert run.status == "completed_with_limitations"
        assert run.stop_reason == "independent_audit_incomplete_or_negative"
    with factory() as session:
        saved = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        report = read_runtime_report(session, saved)
        assert any(issue["concern"] == "Delivery exclusions need a response." for issue in report["issues"])


def test_dictionary_issue_ledger_does_not_accept_malformed_values(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.configuration = {**run.configuration, "discussion_issues": {"issue_open": {"id": "issue_open"}}}
        with pytest.raises(ClaimRejected):
            discussion_issues(run)
