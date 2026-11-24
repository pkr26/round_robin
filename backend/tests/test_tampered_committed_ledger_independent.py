"""A committed event must bind its saved obligation before another call."""
from copy import deepcopy
from dataclasses import replace

import pytest
from round_robin.database.models import Attempt, Event, Run, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import validate_output
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.contributions import DiscussionObjection
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("tamper", ["source_provider", "event_provider", "event_response", "missing_issue", "missing_issue_and_event", "missing_step_and_ledger_and_event"])
def test_tampering_an_actual_committed_obligation_fences_later_provider(factory, tamper):
    class OpeningProvider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            source = request.evidence[0]["id"]
            output = result.output.model_copy(update={"objections": [DiscussionObjection(
                alias="new_objection_1", concern="Real recorded concern", severity="material",
                target_reference_id=source, reference_ids=[source],
            )]})
            validate_output(output, request)
            return replace(result, output=output)

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
    execute_step(opening_id, session_factory=factory, adapter_registry={"openai": OpeningProvider("openai")})
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        ledger = deepcopy(run.configuration["discussion_issues"])
        issue = next(item for item in ledger if item["concern"] == "Real recorded concern")
        if tamper == "source_provider":
            issue["source_provider"] = "anthropic"
        elif tamper == "event_provider":
            issue["events"][0]["provider"] = "anthropic"
        elif tamper == "event_response":
            issue["events"][0]["response"] = "Not the actual recorded concern"
        else:
            ledger = [item for item in ledger if item["id"] != issue["id"]]
            if tamper in {"missing_issue_and_event", "missing_step_and_ledger_and_event"}:
                for event in session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type == "discussion.obligation_recorded")):
                    if event.payload.get("objection_id") == issue["id"]:
                        session.delete(event)
            if tamper == "missing_step_and_ledger_and_event":
                session.delete(session.get(Step, opening_id))
        run.configuration = {**run.configuration, "discussion_issues": ledger}
        next_step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "proposal", Step.status == "pending").limit(1))

    registry = {name: Spy(name) for name in ("openai", "anthropic", "google")}
    execute_step(next_step_id, session_factory=factory, adapter_registry=registry)
    assert calls == []
    with factory() as session:
        assert session.scalar(select(Attempt).where(Attempt.step_id == next_step_id)) is None
        assert session.get(Run, run_id).status == "failed"
