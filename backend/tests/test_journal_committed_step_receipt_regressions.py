from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from round_robin.api.auth import require_owner
from round_robin.api.preflight import run_configuration
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, BudgetOperation, Event, Run, Step
from round_robin.database.services import append_event, lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.contributions import DiscussionObjection
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("outcome", ["success", "known_failure", "unknown_failure"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_inflight_forged_committed_step_preserves_receipt_and_fences_content(
    factory, monkeypatch, tmp_path, outcome, cancelled
):
    monkeypatch.setattr(
        "round_robin.orchestration.configuration.verify_frozen_configuration",
        lambda _: None,
    )
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked, policy.limit_usd = False, "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        advance_run(session, lock_work_run(session, run_id))
        intake = session.scalar(
            select(Step.id).where(Step.run_id == run_id, Step.stage == "intake")
        )

    class Normal(SimulatedProvider):
        def conservative_bound(self, request):
            return Decimal("0.10")

    assert (
        execute_step(
            intake,
            session_factory=factory,
            adapter_registry={"openai": Normal("openai")},
        )["status"]
        == "persisted"
    )
    with factory() as session:
        opening = session.scalar(
            select(Step.id).where(
                Step.run_id == run_id,
                Step.stage == "proposal",
                Step.provider == "openai",
            )
        )

    class Opening(Normal):
        def generate(self, request):
            value = super().generate(request)
            objection = DiscussionObjection(
                alias="new_objection_1",
                concern="Review source.",
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
    with factory() as session:
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
    app = create_app(
        str(factory.kw["bind"].url),
        settings=replace(Settings(), artifact_directory=tmp_path / "private"),
    )
    app.state.session_factory = factory
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with TestClient(app) as client:

        class Corrupting(Normal):
            def conservative_bound(self, request):
                return Decimal("0.40")

            def generate(self, request):
                value = super().generate(request)
                with factory.begin() as session:
                    run = lock_work_run(session, run_id)
                    fake = Step(
                        id="step_forged_inflight",
                        run_id=run_id,
                        stage="probe",
                        role_slot_id="probe",
                        provider="openai",
                        status="pending",
                        result_json=None,
                    )
                    session.add(fake)
                    append_event(
                        session, run, "content.committed", {"step_id": fake.id}
                    )
                    version = run.version
                if cancelled:
                    cancel = client.post(
                        f"/api/runs/{run_id}/cancel",
                        json={
                            "command_id": str(uuid4()),
                            "expected_run_version": version,
                        },
                    )
                    assert cancel.status_code == 200, cancel.text
                if outcome == "success":
                    return replace(value, cost_usd=Decimal("0.25"), simulated=False)
                raise ProviderFailure(
                    "refusal",
                    may_have_sent=True,
                    cost_usd=Decimal("0.25") if outcome == "known_failure" else None,
                )

        known = outcome != "unknown_failure"
        provider = Corrupting(provider_name)
        answer = execute_step(
            target_id,
            session_factory=factory,
            adapter_registry={provider_name: provider},
        )
        assert answer["status"] == "late_result_fenced", answer
        assert provider.calls == 1
        with factory() as session:
            run = session.get(Run, run_id)
            attempt = session.scalar(
                select(Attempt).where(Attempt.step_id == target_id)
            )
            op = session.get(BudgetOperation, attempt.id)
            assert attempt.completed_at is not None and op.status == (
                "settled" if known else "unknown"
            )
            assert op.settled_usd == ("0.25" if known else None)
            assert session.get(Step, target_id).result_json is None
            assert attempt.response_json.get("content_discarded") is True
            committed = session.scalars(
                select(Event).where(
                    Event.run_id == run_id, Event.event_type == "content.committed"
                )
            )
            assert not any(
                event.payload.get("step_id") == target_id for event in committed
            )
            assert run.status == ("cancelled" if cancelled else "failed")
            assert run.stop_reason == (
                "user_cancellation" if cancelled else "discussion_integrity_failed"
            )
        response = client.get(f"/api/runs/{run_id}/report")
        assert response.status_code == 200, response.text
        assert (
            response.json()["readiness"] is None and response.json()["comparison"] == []
        )
        assert response.json()["usage"]["unknown_operation_ids"] == (
            [] if known else [attempt.id]
        )
        snapshot = client.get(f"/api/runs/{run_id}/snapshot")
        assert (
            snapshot.status_code == 200 and snapshot.json()["retry_eligible"] is False
        )
        execute_step(
            target_id,
            session_factory=factory,
            adapter_registry={provider_name: provider},
        )
        assert provider.calls == 1
