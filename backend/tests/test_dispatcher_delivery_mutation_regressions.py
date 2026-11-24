"""Dispatch respects durable quarantine, scheduling and generation boundaries."""

from datetime import timedelta

import pytest
from round_robin.database.models import Outbox, PolicyBucket, Run, Step, utcnow
from round_robin.dispatcher import service
from sqlalchemy import delete

from backend.tests.test_orchestration import seed

pytest_plugins = ("backend.tests.test_orchestration",)


@pytest.fixture
def queue(factory, tmp_path, monkeypatch):
    factory.configure(info={"artifact_directory": tmp_path / "artifacts", "durability_control_directory": tmp_path / "control"})
    run_id = seed(factory)
    now = utcnow()
    monkeypatch.setattr(service, "utcnow", lambda: now)
    with factory.begin() as session:
        session.execute(delete(Outbox))
    return factory, run_id, now


def _item(run_id, identity, kind, due_at, **values):
    return Outbox(id=identity, run_id=run_id, kind=kind, status="pending", due_at=due_at, **values)


@pytest.mark.parametrize("gate", ["restore_quarantine", "dispatch_locked"])
def test_durable_deployment_gate_blocks_all_publication(queue, gate):
    factory, run_id, now = queue
    with factory.begin() as session:
        session.add(PolicyBucket(id="deployment_gate", key="deployment", limit_usd="0", spending_locked=True, metadata_json={gate: True}))
        session.add(_item(run_id, "work_ready", "research", now))
    sent = []
    assert service.dispatch_once(session_factory=factory, sender=lambda *args: sent.append(args)) == 0
    assert sent == []
    with factory() as session:
        work = session.get(Outbox, "work_ready")
        assert work.status == "pending" and work.publication_attempts == 0


def test_batch_publishes_due_jobs_in_order_with_limit_and_not_future(queue):
    factory, run_id, now = queue
    with factory.begin() as session:
        session.add_all([
            _item(run_id, "z_earlier", "finalize", now - timedelta(seconds=2)),
            _item(run_id, "a_later", "research", now - timedelta(seconds=1)),
            _item(run_id, "b_equal", "advance", now),
            _item(run_id, "c_future", "advance", now + timedelta(seconds=1)),
        ])
    sent = []
    assert service.dispatch_once(session_factory=factory, sender=lambda *args: sent.append(args), limit=2) == 2
    assert sent == [("finalize", run_id), ("research", run_id)]
    with factory() as session:
        assert session.get(Outbox, "z_earlier").publication_attempts == 1
    assert service.dispatch_once(session_factory=factory, sender=lambda *args: sent.append(args), limit=2) == 1
    assert sent[-1] == ("advance", run_id)
    with factory() as session:
        assert session.get(Outbox, "c_future").status == "pending"


def test_legacy_zero_generation_is_forwarded_and_stale_work_is_retired(queue):
    factory, run_id, now = queue
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {}
        session.add_all([
            Step(id="step_current", run_id=run_id, stage="proposal", role_slot_id="current", provider="openai", status="pending"),
            Step(id="step_stale", run_id=run_id, stage="proposal", role_slot_id="stale", provider="openai", status="pending"),
        ])
        session.add_all([
            _item(run_id, "a_current", "execute", now, step_id="step_current", payload={}),
            _item(run_id, "b_stale", "execute", now, step_id="step_stale", payload={"dispatch_generation": 1}),
        ])
    sent = []
    assert service.dispatch_once(session_factory=factory, sender=lambda *args: sent.append(args)) == 1
    assert sent == [("execute", {"step_id": "step_current", "dispatch_generation": 0})]
    with factory() as session:
        assert session.get(Outbox, "a_current").status == "published"
        assert session.get(Outbox, "b_stale").status == "done"


def test_failed_send_is_delayed_without_blocking_other_due_work(queue):
    factory, run_id, now = queue
    with factory.begin() as session:
        session.add_all([
            _item(run_id, "a_failing", "research", now, publication_attempts=3),
            _item(run_id, "b_following", "finalize", now, publication_attempts=2),
        ])
    sent = []
    def sender(kind, argument):
        sent.append((kind, argument))
        if kind == "research":
            raise ConnectionError("synthetic broker failure")
    assert service.dispatch_once(session_factory=factory, sender=sender) == 1
    assert sent == [("research", run_id), ("finalize", run_id)]
    with factory() as session:
        failed = session.get(Outbox, "a_failing")
        succeeded = session.get(Outbox, "b_following")
        assert failed.status == "pending" and failed.publication_attempts == 4
        assert failed.due_at.replace(tzinfo=now.tzinfo) > now
        assert succeeded.status == "published" and succeeded.publication_attempts == 3
    sent.clear()
    assert service.dispatch_once(session_factory=factory, sender=sender) == 0
    assert sent == []
