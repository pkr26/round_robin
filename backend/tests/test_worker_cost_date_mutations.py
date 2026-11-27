"""Provider calculations use the dispatch local date for quote freshness."""

import socket
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from round_robin.database.models import Run
from round_robin.research import calculator
from round_robin.worker.tasks import build_request

from backend.tests.test_evidence_regressions import complete_brief
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
    ("dispatch_utc", "expected_date", "expected_output"),
    [
        (datetime(2026, 11, 17, 1, tzinfo=UTC), "2026-11-16", "1600.00"),
        (datetime(2026, 11, 18, 1, tzinfo=UTC), "2026-11-17", None),
    ],
)
def test_provider_cost_packet_uses_dispatch_local_date(
    factory, monkeypatch, dispatch_utc, expected_date, expected_output
):
    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2030, 1, 1, tzinfo=UTC).astimezone(tz) if tz else cls(2030, 1, 1)

    monkeypatch.setattr(calculator, "datetime", LaterClock)
    monkeypatch.setattr("round_robin.worker.tasks.utcnow", lambda: dispatch_utc)
    run_id = seed(
        factory,
        brief=complete_brief(valid_until=date(2026, 11, 16)),
        suffix="cost_date",
    )
    with factory.begin() as session:
        session.get(Run, run_id).created_at = datetime(2026, 11, 15, 1, tzinfo=UTC)
    stage = SimpleNamespace(
        id="request_step",
        stage="proposal",
        stage_iteration=0,
        role_slot_id="proposal",
        calls=0,
        error_code=None,
    )
    with factory() as session:
        request = build_request(session, session.get(Run, run_id), stage)
        packet = request.packet()
    assert packet["cost_as_of"] == expected_date
    assert packet["calculations"][0]["output"] == expected_output
    if expected_output is None:
        assert any(
            "quote expired" in reason
            for reason in packet["calculations"][0]["missing_components"]
        )
    else:
        assert not any(
            "quote expired" in reason
            for reason in packet["calculations"][0]["missing_components"]
        )
