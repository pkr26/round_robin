"""Research keeps enough active time for discussion and finalization."""
from types import SimpleNamespace

import pytest
from round_robin.research import service


@pytest.mark.parametrize(
    ("configuration", "remaining", "expected"),
    [
        ({}, 260, 45),
        ({"discussion_policy": {"version": "current"}, "finish_policy": {"reserved_active_seconds": 50}}, 260, 45),
        ({"discussion_policy": {"version": "current"}, "finish_policy": {"reserved_active_seconds": 220}}, 260, 15),
        ({"finish_policy": {"reserved_active_seconds": 50}}, 100, 25),
        ({}, 214, 0),
        ({}, 215, 0),
        ({}, 216, 1),
        ({}, 285, 70),
        ({}, 286, 70),
    ],
)
def test_research_time_does_not_spend_finish_reserve(monkeypatch, configuration, remaining, expected):
    run = SimpleNamespace(configuration=configuration)
    monkeypatch.setattr(service, "remaining_active", lambda candidate: remaining if candidate is run else 0)
    assert service.research_seconds_available(run) == expected
