"""Worker review context must be independent, current, and scoped to its run."""
import socket
from datetime import timedelta
from types import SimpleNamespace

import pytest
from round_robin.database.models import Run, Step, utcnow
from round_robin.worker.tasks import build_request

from backend.tests.test_orchestration import seed

pytest_plugins = ("backend.tests.test_orchestration",)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network calls forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def seed_steps(factory):
    own = seed(factory, suffix="own")
    foreign = seed(factory, suffix="foreign")
    now = utcnow()
    with factory.begin() as session:
        for identity, run_id, stage, status, iteration, created in [
            ("own_proposal_a", own, "proposal", "succeeded", 0, now),
            ("own_proposal_b", own, "proposal", "succeeded", 0, now),
            ("own_failed", own, "proposal", "failed", 0, now),
            ("foreign_proposal", foreign, "proposal", "succeeded", 0, now),
            ("own_synthesis_old", own, "synthesis", "succeeded", 0, now + timedelta(seconds=1)),
            ("own_synthesis_new", own, "synthesis", "succeeded", 1, now),
            ("own_audit_old", own, "audit", "succeeded", 0, now),
            ("foreign_synthesis", foreign, "synthesis", "succeeded", 2, now),
        ]:
            session.add(Step(id=identity, run_id=run_id, stage=stage, role_slot_id=identity, provider="openai",
                status=status, stage_iteration=iteration, created_at=created, result_json={"output": {"marker": identity}}))
    return own


def request(factory, run_id, stage, iteration=0):
    step = SimpleNamespace(id="step_request", stage=stage, stage_iteration=iteration, role_slot_id=stage, calls=0, error_code=None)
    with factory() as session:
        return build_request(session, session.get(Run, run_id), step)


def test_independent_proposal_has_no_peer_outputs(factory):
    run_id = seed_steps(factory)
    packet = request(factory, run_id, "proposal")
    assert packet.peer_outputs == []
    assert packet.packet()["peer_outputs"] == []


def test_initial_synthesis_only_sees_its_own_successful_proposals(factory):
    run_id = seed_steps(factory)
    packet = request(factory, run_id, "synthesis")
    assert {peer["id"] for peer in packet.peer_outputs} == {"own_proposal_a", "own_proposal_b"}
    assert all(peer["stage"] == "proposal" and peer["provider"] == "openai" and peer["output"]["marker"] == peer["id"] for peer in packet.peer_outputs)


def test_audit_sees_latest_synthesis_but_not_another_audit_or_foreign_run(factory):
    run_id = seed_steps(factory)
    packet = request(factory, run_id, "audit", iteration=1)
    assert {peer["id"] for peer in packet.peer_outputs} == {"own_proposal_a", "own_proposal_b", "own_synthesis_new"}
    assert "own_synthesis_new" in packet.allowed_reference_ids
    assert "foreign_synthesis" not in packet.allowed_reference_ids


def test_correction_synthesis_receives_latest_prior_synthesis_and_audit(factory):
    run_id = seed_steps(factory)
    packet = request(factory, run_id, "synthesis", iteration=1)
    assert {peer["id"] for peer in packet.peer_outputs} == {"own_proposal_a", "own_proposal_b", "own_synthesis_new", "own_audit_old"}


def test_equal_time_proposals_have_stable_context_across_storage_orders(tmp_path):
    from round_robin.database.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    timestamp = utcnow()
    orders = []
    for index, insertion in enumerate((("proposal_b", "proposal_a"), ("proposal_a", "proposal_b"))):
        engine = create_engine(f"sqlite:///{tmp_path / f'order_{index}.sqlite'}")
        Base.metadata.create_all(engine)
        local_factory = sessionmaker(engine, expire_on_commit=False)
        try:
            run_id = seed(local_factory, suffix="order")
            with local_factory.begin() as session:
                for identity in insertion:
                    session.add(Step(id=identity, run_id=run_id, stage="proposal", role_slot_id=identity,
                        provider="openai", status="succeeded", stage_iteration=0, created_at=timestamp,
                        result_json={"output": {"marker": identity}}))
            packet = request(local_factory, run_id, "synthesis")
            orders.append([peer["id"] for peer in packet.peer_outputs])
        finally:
            engine.dispose()
    assert set(orders[0]) == {"proposal_a", "proposal_b"}
    assert orders[0] == orders[1]
