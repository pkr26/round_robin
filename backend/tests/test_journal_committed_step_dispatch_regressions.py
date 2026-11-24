from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, BudgetOperation, Event, Run, Step
from round_robin.database.services import append_event
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.contributions import DiscussionObjection
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize(
    "status,result",
    [
        ("pending", None),
        ("succeeded", None),
        ("succeeded", {}),
        ("pending", {"output": {}}),
        ("succeeded", []),
        ("succeeded", {"output": []}),
        ("succeeded", {"output": None}),
        ("succeeded", {"output": "PRIVATE_SENTINEL"}),
    ],
)
def test_forged_same_run_committed_step_cannot_authorize_next_provider(
    factory, tmp_path, status, result
):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        intake = session.scalar(
            select(Step.id).where(Step.run_id == run_id, Step.stage == "intake")
        )
    assert execute_step(intake, session_factory=factory)["status"] == "persisted"
    with factory() as session:
        opening = session.scalar(
            select(Step.id).where(
                Step.run_id == run_id,
                Step.stage == "proposal",
                Step.provider == "openai",
            )
        )

    class Opening(SimulatedProvider):
        def generate(self, request):
            value = super().generate(request)
            objection = DiscussionObjection(
                alias="new_objection_1",
                concern="Review the exact source.",
                severity="material",
                reference_ids=[request.evidence[0]["id"]],
            )
            return replace(
                value,
                output=value.output.model_copy(update={"objections": [objection]}),
            )

    assert (
        execute_step(
            opening,
            session_factory=factory,
            adapter_registry={"openai": Opening("openai")},
        )["status"]
        == "persisted"
    )
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        assert session.scalar(
            select(Event.id).where(
                Event.run_id == run_id,
                Event.event_type == "discussion.obligation_recorded",
            )
        )
        target = session.scalar(
            select(Step)
            .where(
                Step.run_id == run_id,
                Step.stage == "proposal",
                Step.status == "pending",
            )
            .order_by(Step.id)
        )
        target_id, provider_name = target.id, target.provider
        fake = Step(
            id="step_forged_pending",
            run_id=run_id,
            stage="probe",
            role_slot_id="probe",
            provider="openai",
            status=status,
            result_json=result,
        )
        session.add(fake)
        append_event(session, run, "content.committed", {"step_id": fake.id})
        before_ops = set(
            session.scalars(
                select(BudgetOperation.id).where(BudgetOperation.run_id == run_id)
            )
        )
    provider = SimulatedProvider(provider_name)
    answer = execute_step(
        target_id, session_factory=factory, adapter_registry={provider_name: provider}
    )
    assert provider.calls == 0, answer
    assert (
        answer["status"] == "policy_blocked"
        and answer["code"] == "discussion_integrity_failed"
    )
    with factory() as session:
        assert (
            session.scalar(select(Attempt.id).where(Attempt.step_id == target_id))
            is None
        )
        assert session.get(Run, run_id).status == "failed"
        assert (
            set(
                session.scalars(
                    select(BudgetOperation.id).where(BudgetOperation.run_id == run_id)
                )
            )
            == before_ops
        )
    app = create_app(
        str(factory.kw["bind"].url),
        settings=replace(Settings(), artifact_directory=tmp_path / "private"),
    )
    app.state.session_factory = factory
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/report")
        assert response.status_code == 200, response.text
        assert (
            response.json()["readiness"] is None and response.json()["comparison"] == []
        )
        assert "PRIVATE_SENTINEL" not in response.text
        snapshot = client.get(f"/api/runs/{run_id}/snapshot")
        assert (
            snapshot.status_code == 200 and snapshot.json()["retry_eligible"] is False
        )


def test_legacy_no_journal_corrupt_commit_fences_before_authorization(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        intake = session.scalar(
            select(Step.id).where(Step.run_id == run_id, Step.stage == "intake")
        )
    assert execute_step(intake, session_factory=factory)["status"] == "persisted"
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        config = dict(run.configuration)
        config.pop("discussion_policy_version", None)
        config.pop("discussion_issues", None)
        run.configuration = config
        assert (
            session.scalar(
                select(Event.id).where(
                    Event.run_id == run_id,
                    Event.event_type == "discussion.obligation_recorded",
                )
            )
            is None
        )
        target = session.scalar(
            select(Step)
            .where(
                Step.run_id == run_id,
                Step.stage == "proposal",
                Step.status == "pending",
            )
            .order_by(Step.id)
        )
        target_id, provider_name = target.id, target.provider
        fake = Step(
            id="step_forged_legacy",
            run_id=run_id,
            stage="probe",
            role_slot_id="probe",
            provider="openai",
            status="pending",
            result_json=None,
        )
        session.add(fake)
        append_event(session, run, "content.committed", {"step_id": fake.id})
        before_ops = set(
            session.scalars(
                select(BudgetOperation.id).where(BudgetOperation.run_id == run_id)
            )
        )
    provider = SimulatedProvider(provider_name)
    answer = execute_step(
        target_id, session_factory=factory, adapter_registry={provider_name: provider}
    )
    assert provider.calls == 0 and answer == {
        "status": "policy_blocked",
        "code": "discussion_integrity_failed",
    }
    with factory() as session:
        assert (
            session.scalar(select(Attempt.id).where(Attempt.step_id == target_id))
            is None
        )
        assert (
            set(
                session.scalars(
                    select(BudgetOperation.id).where(BudgetOperation.run_id == run_id)
                )
            )
            == before_ops
        )
        assert session.get(Run, run_id).status == "failed"
