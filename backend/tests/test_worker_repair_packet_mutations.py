"""Provider repair requests retain only the current step's diagnostics."""

import socket
from types import SimpleNamespace

import pytest
from round_robin.database.models import Run
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


def request(session, run_id, *, calls, error, identity="step_current"):
    step = SimpleNamespace(
        id=identity,
        stage="proposal",
        stage_iteration=0,
        role_slot_id="proposal",
        calls=calls,
        error_code=error,
    )
    return build_request(session, session.get(Run, run_id), step).packet()


@pytest.mark.parametrize(
    ("calls", "error", "repair"),
    [
        (0, None, False),
        (1, "timeout", False),
        (1, "rate_limit", False),
        (1, "schema_invalid", True),
        (1, "malformed_response", True),
        (1, "invalid_reference", True),
    ],
)
def test_repair_applies_to_response_validation_retries(factory, calls, error, repair):
    run_id = seed(factory, suffix="repair")
    with factory() as session:
        packet = request(session, run_id, calls=calls, error=error)
    assert packet["repair"] is repair
    assert packet["repair_diagnostics"] == []


def test_diagnostics_are_scoped_to_current_step_and_missing_entries_are_empty(factory):
    run_id = seed(factory, suffix="diagnostics")
    current = [{"code": "invalid_reference", "path": "$.references"}]
    other = [{"code": "schema_invalid", "path": "$.other_step"}]
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {
            **run.configuration,
            "step_dispatch": {
                "step_current": {"repair_diagnostics": current},
                "step_other": {"repair_diagnostics": other},
                "step_empty": {},
            },
        }
    with factory() as session:
        assert (
            request(session, run_id, calls=1, error="invalid_reference")[
                "repair_diagnostics"
            ]
            == current
        )
        assert (
            request(session, run_id, calls=1, error="timeout", identity="step_empty")[
                "repair_diagnostics"
            ]
            == []
        )
        assert (
            request(session, run_id, calls=0, error=None, identity="step_missing")[
                "repair_diagnostics"
            ]
            == []
        )
