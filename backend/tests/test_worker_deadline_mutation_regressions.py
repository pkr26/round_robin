"""Provider requests preserve the finishing reserve and bounded deadlines."""

import socket
from types import SimpleNamespace

import pytest
from round_robin.database.models import Attempt, Run, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker import tasks
from sqlalchemy import select

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


@pytest.mark.parametrize(
    ("stage", "remaining", "expected"),
    [
        ("proposal", 300, 35),
        ("synthesis", 200, 55),
        ("audit", 75, 50),
        ("proposal", 600, 90),
        ("proposal", 265.5, 0.5),
    ],
)
def test_request_deadline_preserves_stage_finish_reserve(
    factory, monkeypatch, stage, remaining, expected
):
    run_id = seed(factory, suffix="deadline")
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {
            **run.configuration,
            "finish_policy": {
                **run.configuration.get("finish_policy", {}),
                "reserved_active_seconds": 240,
            },
        }
    step = SimpleNamespace(
        id="deadline_step",
        stage=stage,
        stage_iteration=0,
        role_slot_id=stage,
        calls=0,
        error_code=None,
    )
    monkeypatch.setattr(tasks, "remaining_active", lambda run: remaining)
    with factory() as session:
        request = tasks.build_request(session, session.get(Run, run_id), step)
    assert request.timeout_seconds == expected


def test_no_provider_authorization_at_exhausted_finish_reserve(factory, monkeypatch):
    run_id = seed(factory, suffix="no_deadline")
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
    monkeypatch.setattr(tasks, "remaining_active", lambda run: 215)
    adapter = SimulatedProvider("openai")

    def forbid_bound(*args, **kwargs):
        pytest.fail("must not authorize a provider request without time")

    monkeypatch.setattr(adapter, "conservative_bound", forbid_bound)
    tasks.execute_step(
        step_id, session_factory=factory, adapter_registry={"openai": adapter}
    )
    assert adapter.calls == 0
    with factory() as session:
        assert session.scalar(select(Attempt).where(Attempt.run_id == run_id)) is None


def test_deadline_uses_saved_run_active_time(factory):
    run_id = seed(factory, suffix="active_clock")
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {
            **run.configuration,
            "finish_policy": {
                **run.configuration.get("finish_policy", {}),
                "reserved_active_seconds": 240,
            },
            "limits": {**run.configuration.get("limits", {}), "active_seconds": 600},
        }
        run.active_seconds = 300
        run.active_started_at = None
    step = SimpleNamespace(
        id="deadline_step",
        stage="proposal",
        stage_iteration=0,
        role_slot_id="proposal",
        calls=0,
        error_code=None,
    )
    with factory() as session:
        request = tasks.build_request(session, session.get(Run, run_id), step)
    assert request.timeout_seconds == 35
