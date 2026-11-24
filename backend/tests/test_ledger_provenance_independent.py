"""Independent pre-authorization provenance probe; no external provider calls."""
from round_robin.database.models import Attempt, BudgetOperation, Run, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.research.discussion import input_hash
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


def test_invalid_saved_provenance_is_fenced_before_any_provider_authorization(factory):
    run_id = seed(factory)
    calls = []

    class Spy(SimulatedProvider):
        def conservative_bound(self, request):
            calls.append("bound")
            return super().conservative_bound(request)

        def generate(self, request):
            calls.append("generate")
            return super().generate(request)

    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id, Step.stage == "intake"))
        step_id = step.id
        digest = input_hash(run)
        issue = {
            "id": "issue_invalid_source", "concern": "Unverified material concern", "severity": "material",
            "status": "resolved", "origin": "structured", "source_step_id": "step_does_not_exist",
            "source_provider": "openai", "source_stage": "proposal", "events": [
                {"id": "event_answer", "step_id": "step_does_not_exist", "provider": "openai",
                 "stage": "synthesis", "disposition": "answered", "response": "An answer",
                 "reference_ids": ["evidence_does_not_exist"], "input_hash": digest},
                {"id": "event_resolution", "step_id": "step_also_missing", "provider": "anthropic",
                 "stage": "audit", "disposition": "resolved", "response": "A resolution",
                 "reference_ids": ["evidence_does_not_exist"], "input_hash": digest},
            ],
        }
        run.configuration = {**run.configuration, "discussion_issues": [issue]}

    registry = {name: Spy(name) for name in ("openai", "anthropic", "google")}
    execute_step(step_id, session_factory=factory, adapter_registry=registry)

    assert calls == []
    with factory() as session:
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None
        assert not list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run_id, BudgetOperation.kind == "model")))
        assert session.get(Run, run_id).status == "failed"
