"""Controller deliveries respect cancellation and input-wait fences."""

from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest
from sqlalchemy import delete, select

from backend.tests.test_orchestration import drain, factory, seed
from backend.tests.test_runtime_algorithm_fixes import proposals
from round_robin.database.models import Event, Meeting, Outbox, Owner, ReportVersion, Run, Step
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import aware
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


def test_cancellation_finalizes_even_when_dispatch_is_paused(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.status = "cancel_requested"
        deployment, _ = lock_policies(session, run.owner_id)
        deployment.metadata_json = {**deployment.metadata_json, "dispatch_locked": True}

        advance_run(session, run)

        assert run.status == "cancelled"
        assert run.stage == "finished"
        assert run.stop_reason == "user_cancellation"
        assert session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)) is not None


def test_awaiting_input_delivery_does_not_enqueue_another_advance(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.status = "awaiting_input"
        session.execute(delete(Outbox).where(Outbox.run_id == run_id))
        session.flush()

        advance_run(session, run)

        assert run.status == "awaiting_input"
        assert session.scalar(select(Outbox.id).where(Outbox.run_id == run_id)) is None


def test_draining_admission_schedules_a_five_second_retry(factory, monkeypatch):
    run_id = seed(factory)
    now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("round_robin.orchestration.controller.utcnow", lambda: now)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        deployment, _ = lock_policies(session, run.owner_id)
        deployment.metadata_json = {**deployment.metadata_json, "draining": True}
        session.execute(delete(Outbox).where(Outbox.run_id == run_id))
        session.flush()

        advance_run(session, run)

        assert run.status == "queued"
        retry = session.scalar(select(Outbox).where(Outbox.run_id == run_id, Outbox.kind == "advance"))
        assert retry is not None
        assert aware(retry.due_at) == now + timedelta(seconds=5)


@pytest.mark.parametrize("tail,delay,remaining,cutoff", [
    (350, 0, 300, True), (190, 150, 300, True),
    (None, 0, 215, True), (None, 0, 215.1, False),
])
def test_optional_cutoff_preserves_configured_finish_tail_and_capacity(factory, monkeypatch, tail, delay, remaining, cutoff):
    run_id, steps = proposals(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        claimed = session.get(Step, steps["openai"])
        claimed.status, claimed.lease_owner = "claimed", "worker-claimed"
        authorized = session.get(Step, steps["anthropic"])
        authorized.status, authorized.lease_owner = "authorized", "worker-authorized"
        claimed.lease_expires_at = authorized.lease_expires_at = datetime(2026, 11, 17, tzinfo=timezone.utc)
        configuration = dict(run.configuration)
        if tail is None:
            configuration.pop("finish_policy", None)
        else:
            configuration["finish_policy"] = {**configuration["finish_policy"], "reserved_active_seconds": tail}
        run.configuration = configuration
        monkeypatch.setattr("round_robin.orchestration.controller.remaining_active", lambda _: remaining)
        monkeypatch.setattr("round_robin.orchestration.controller.capacity_delay_seconds", lambda _: delay)

        advance_run(session, run)

        assert all(
            (session.get(Step, step_id).error_code == "mandatory_finish_cutoff") is cutoff
            for step_id in steps.values()
        )
        if cutoff:
            assert all(session.get(Step, step_id).status == "skipped" for step_id in steps.values())
            assert claimed.lease_owner is None and claimed.lease_expires_at is None
            assert authorized.lease_owner is None and authorized.lease_expires_at is None
            events = list(session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type == "step.skipped")))
            assert {event.payload["step_id"] for event in events} == set(steps.values())
            for event in events:
                step = session.get(Step, event.payload["step_id"])
                assert event.payload == {
                    "step_id": step.id, "stage": step.stage, "provider": step.provider,
                    "error_code": "mandatory_finish_cutoff",
                }


def test_new_iteration_is_not_blocked_by_prior_closed_barrier(factory):
    run_id, steps = proposals(factory)
    execute_step(steps["openai"], session_factory=factory, adapter_registry={"openai": SimulatedProvider("openai")})
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        generation = run.recovery_generation
        run.configuration = {
            **run.configuration, "stage_iteration": 1,
            "closed_barriers": [f"proposals:{generation}:0"],
        }
        for step_id in steps.values():
            step = session.get(Step, step_id)
            step.stage_iteration = 1
            if step_id != steps["openai"]:
                step.status = "failed"
        session.flush()

        advance_run(session, run)

        assert run.status == "completed_with_limitations"
        assert f"proposals:{generation}:1" in run.configuration["closed_barriers"]
        settled = session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == "stage.settled"))
        assert settled is not None
        assert settled.payload == {"stage": "proposals", "succeeded": 1, "scheduled": len(steps)}


def test_confirmation_quota_ignores_other_owners_and_nonwaiting_runs(factory, monkeypatch):
    run_id = seed(factory)
    now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("round_robin.orchestration.controller.utcnow", lambda: now)
    with factory.begin() as session:
        session.add(Owner(id="other_owner"))
    for index in range(3):
        other_id = seed(factory, suffix=f"other{index}")
        with factory.begin() as session:
            other = session.get(Run, other_id)
            other.owner_id, other.status = "other_owner", "awaiting_input"
            session.get(Meeting, other.meeting_id).owner_id = "other_owner"
        completed_id = seed(factory, suffix=f"completed{index}")
        with factory.begin() as session:
            session.get(Run, completed_id).status = "completed"
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.status = "succeeded"
        step.result_json = {"output": {"questions": ["Confirm the horizon"]}}
        previous_version = run.version

        advance_run(session, run)

        assert run.status == "awaiting_input"
        assert run.version == previous_version + 1
        assert run.configuration["proposed_brief"] == run.input_snapshot
        assert aware(run.input_expires_at) == now + timedelta(hours=24)
        event = session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == "brief.confirmation_required"))
        assert event is not None
        assert event.payload == {
            "questions": ["Confirm the horizon"],
            "expires_at": run.input_expires_at.isoformat(),
            "active_seconds": run.active_seconds,
        }


@pytest.mark.parametrize("output,expected_status", [
    ({"proposed_changes": []}, "running"),
    ({"questions": ["Clarify"]}, "awaiting_input"),
])
def test_intake_accepts_legacy_missing_optional_fields(factory, output, expected_status):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.status = "succeeded"
        step.result_json = {"output": output}

        advance_run(session, run)

        assert run.status == expected_status
        if expected_status == "awaiting_input":
            assert run.configuration["proposed_changes"] == []
        else:
            assert run.stage == "proposals"


def test_legacy_intake_questions_are_capped_before_confirmation(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.status = "succeeded"
        step.result_json = {"output": {"questions": ["One?", "Two?", "Three?", "Four?"]}}

        advance_run(session, run)

        assert run.status == "awaiting_input"
        assert run.configuration["questions"] == ["One?", "Two?", "Three?"]


def test_negative_audit_without_defect_text_is_not_completed(factory):
    class NegativeAudit(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            if request.stage == "audit":
                output = result.output.model_copy(update={"audit_passed": False, "critical_defects": []})
                return replace(result, output=output)
            return result

    run_id = seed(factory)
    registry = {provider: NegativeAudit(provider) for provider in ("openai", "anthropic", "google")}
    drain(factory, registry=registry)
    with factory() as session:
        run = session.get(Run, run_id)
        audit = session.scalar(select(Step).where(Step.run_id == run_id, Step.stage == "audit"))
        assert audit.status == "succeeded"
        assert audit.result_json["output"]["audit_passed"] is False
        assert audit.result_json["output"]["critical_defects"] == []
        assert run.status == "completed_with_limitations"
        assert run.stop_reason == "independent_audit_incomplete_or_negative"
