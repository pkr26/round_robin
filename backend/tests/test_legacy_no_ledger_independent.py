"""Legacy committed critical defects must survive later contribution commits."""
from dataclasses import replace

import pytest
from round_robin.database.models import Attempt, Event, ReportVersion, Run, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("has_issue", [False, True])
def test_legacy_missing_journal_never_silently_loses_a_committed_defect(factory, has_issue):
    class LegacyOpening(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            return replace(result, output=result.output.model_copy(update={"dissent": [], "critical_defects": ["Legacy material safety concern"] if has_issue else []}))

    calls = []

    class Spy(SimulatedProvider):
        def conservative_bound(self, request):
            calls.append("bound")
            return super().conservative_bound(request)

        def generate(self, request):
            calls.append("generate")
            return super().generate(request)

    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        intake_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
    execute_step(intake_id, session_factory=factory)
    with factory() as session:
        opening_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "proposal", Step.provider == "openai"))
    execute_step(opening_id, session_factory=factory, adapter_registry={"openai": LegacyOpening("openai")})
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        configuration = dict(run.configuration)
        configuration.pop("discussion_issues", None)
        configuration.pop("discussion_policy_version", None)
        run.configuration = configuration
        for event in session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type == "discussion.obligation_recorded")):
            session.delete(event)
        # A historical committed contribution may predate the attempt ID key.
        opening = session.get(Step, opening_id)
        opening.result_json = {key: value for key, value in opening.result_json.items() if key != "attempt_id"}
        next_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "proposal", Step.status == "pending").limit(1))

    execute_step(next_id, session_factory=factory, adapter_registry={name: Spy(name) for name in ("openai", "anthropic", "google")})
    with factory() as session:
        run = session.get(Run, run_id)
        if has_issue:
            assert calls == []
            assert (run.status, run.stop_reason) == ("failed", "discussion_integrity_failed")
            assert session.scalar(select(Attempt).where(Attempt.step_id == next_id)) is None
            report = read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)))
            assert report["readiness"] is None and report["comparison"] == []
            assert "discussion ledger cannot be validated" in report["readiness_unavailable_reason"]
            assert report["assessment"]["unresolved_critical_defect"] is True
        else:
            assert calls == ["bound", "generate"]
            assert run.status == "running", (run.status, run.stop_reason)
