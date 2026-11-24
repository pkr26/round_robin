"""A public-research deadline follows synthetic search, validation and fetch."""
from types import SimpleNamespace

import pytest
from round_robin.database.models import Run
from round_robin.research import service
from round_robin.research.fetcher import FetchUnavailable
from round_robin.research.search import SearchHit

pytest_plugins = ("test_research_service",)


def test_search_target_and_fetch_receive_remaining_deadline(research_run, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(service, "research_seconds_available", lambda run: 70)
    with research_run.begin() as session:
        assert service.schedule_research(session, session.get(Run, "run_local"))

    search_timeouts, target_timeouts, fetch_timeouts = [], [], []

    class SyntheticSearch:
        def search(self, query, **kwargs):
            search_timeouts.append(kwargs.get("timeout"))
            return [SearchHit("Synthetic", f"https://example.invalid/{len(search_timeouts)}")]

    def target(url, resolver, **kwargs):
        target_timeouts.append(kwargs.get("resolution_timeout"))
        return SimpleNamespace(url=url)

    def fetch(candidate_id, allowance, **kwargs):
        fetch_timeouts.append(kwargs.get("timeout"))
        clock.now = 1069.8
        raise FetchUnavailable("synthetic unavailable source")

    monkeypatch.setattr(service, "validate_target", target)
    monkeypatch.setattr(service, "collect_source", fetch)
    assert service.execute_research("run_local", session_factory=research_run, search=SyntheticSearch())
    assert search_timeouts == pytest.approx([5, 0.2, 0.2])
    assert target_timeouts == pytest.approx([5, 0.2, 0.2])
    assert fetch_timeouts == pytest.approx([5, 0.2, 0.2])
