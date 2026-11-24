from datetime import UTC, datetime, timedelta

import pytest
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.database.models import Meeting, Outbox, Owner, PreflightRecord, Run
from round_robin.database.services import enqueue
from round_robin.fixtures import sample_brief
from round_robin.orchestration.budget import aware
from sqlalchemy import select

pytest_plugins = ("backend.tests.test_orchestration",)


@pytest.fixture
def seeded_runs(factory):
    now = datetime(2026, 11, 17, 12, tzinfo=UTC)
    with factory.begin() as session:
        session.add(Owner(id="owner_synthetic")); session.flush()
        for suffix in ("one", "two"):
            session.add(Meeting(id=f"meeting_{suffix}", owner_id="owner_synthetic", title="Synthetic")); session.flush()
            session.add(PreflightRecord(id=f"preflight_{suffix}", meeting_id=f"meeting_{suffix}", owner_id="owner_synthetic", draft_revision=1, draft_hash="a" * 64, configuration_hash="b" * 64, payload={}, expires_at=now + timedelta(days=1))); session.flush()
            session.add(Run(id=f"run_{suffix}", meeting_id=f"meeting_{suffix}", owner_id="owner_synthetic", preflight_id=f"preflight_{suffix}", input_snapshot=sample_brief().model_dump(mode="json"), configuration=run_configuration(Settings(), "simulated"), initial_admission_expires_at=now + timedelta(days=1)))
    return now


@pytest.mark.parametrize("status", ["pending", "published"])
def test_advance_reuses_same_run_outbox_without_rewriting_delivery(factory, seeded_runs, status):
    now = seeded_runs
    with factory.begin() as session:
        run = session.get(Run, "run_one")
        first = Outbox(id="work_a", run_id=run.id, kind="advance", status=status, payload={"run_id": run.id, "sentinel": "preserve"}, due_at=now)
        second = Outbox(id="work_b", run_id=run.id, kind="advance", status="pending", payload={"run_id": run.id}, due_at=now)
        # Reverse insertion to distinguish deterministic ID ordering from row order.
        session.add_all([second, first]); session.flush()
        picked = enqueue(session, run, due_at=now + timedelta(seconds=99), payload={"changed": True})
        assert picked.id == "work_a" and picked.status == status
        assert picked.payload == {"run_id": run.id, "sentinel": "preserve"} and aware(picked.due_at) == now
        session.flush()
        assert len(list(session.scalars(select(Outbox).where(Outbox.run_id == run.id)))) == 2


def test_advance_never_reuses_other_run_or_terminal_delivery(factory, seeded_runs):
    now = seeded_runs
    with factory.begin() as session:
        run = session.get(Run, "run_one")
        session.add_all([
            Outbox(id="old_done", run_id=run.id, kind="advance", status="done", payload={"run_id": run.id}, due_at=now),
            Outbox(id="other_pending", run_id="run_two", kind="advance", status="pending", payload={"run_id": "run_two"}, due_at=now),
            Outbox(id="different_kind", run_id=run.id, kind="execute", status="pending", payload={"run_id": run.id}, due_at=now),
        ]); session.flush()
        row = enqueue(session, run, due_at=now)
        assert row.id not in {"old_done", "other_pending", "different_kind"}
        assert row.run_id == run.id and row.kind == "advance" and row.status == "pending"
        assert row.payload == {"run_id": run.id}


@pytest.mark.parametrize("delta", [-10, 10])
def test_execute_generation_and_latest_due_time_are_preserved(factory, seeded_runs, delta):
    now = seeded_runs
    not_before = now + timedelta(seconds=30)
    requested = not_before + timedelta(seconds=delta)
    with factory.begin() as session:
        run = session.get(Run, "run_one")
        run.configuration = {**run.configuration, "step_dispatch": {"step_synthetic": {"generation": 7, "not_before": not_before.isoformat()}}}
        row = enqueue(session, run, "execute", step_id="step_synthetic", payload={"private_test_marker": "kept", "dispatch_generation": 999}, due_at=requested)
        assert row.run_id == run.id and row.step_id == "step_synthetic" and row.kind == "execute" and row.status == "pending"
        assert row.payload == {"private_test_marker": "kept", "dispatch_generation": 7}
        assert aware(row.due_at) == max(requested, not_before)


def test_missing_generation_defaults_zero_and_other_job_payload_is_not_rewritten(factory, seeded_runs, monkeypatch):
    from round_robin.database import services
    now = seeded_runs
    monkeypatch.setattr(services, "utcnow", lambda: now)
    with factory.begin() as session:
        run = session.get(Run, "run_one")
        step = enqueue(session, run, "execute", step_id="step_missing")
        other = enqueue(session, run, "research", payload={"scope": "synthetic"})
        assert step.payload == {"run_id": run.id, "dispatch_generation": 0} and aware(step.due_at) == now
        assert other.payload == {"scope": "synthetic"} and aware(other.due_at) == now
