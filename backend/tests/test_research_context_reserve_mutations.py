"""Public research cannot spend when proposal context lacks its reserve."""
import pytest
from round_robin.database.models import BudgetOperation, Event, Outbox, Run
from round_robin.providers import base
from round_robin.research import service
from sqlalchemy import func, select

pytest_plugins = ("test_research_service",)


@pytest.mark.parametrize("packet_size", [1000, 1001, "unavailable"])
def test_no_context_room_finishes_with_unverified_gap_and_no_spend(research_run, monkeypatch, packet_size):
    monkeypatch.setattr(base, "MAX_INITIAL_PACKET_BYTES", 1512)
    def packet(*args):
        if packet_size == "unavailable":
            raise base.ProviderFailure("synthetic_packet_failure")
        return packet_size
    monkeypatch.setattr(service, "proposal_packet_bytes", packet)
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
        run.configuration = {**run.configuration, "research_lease_id": "stale-synthetic-lease"}
    class NoSearch:
        def search(self, *args, **kwargs):
            pytest.fail("No usable proposal context remains for public research")
    assert service.execute_research("run_local", session_factory=research_run, search=NoSearch())
    with research_run() as session:
        run = session.get(Run, "run_local")
        assert run.configuration["research_state"] == "done"
        assert run.configuration["research_lease_id"] is None
        assert run.configuration["research_summary"] == {
            "status": "limited", "reason": "downstream_context_reserve", "queries": 0,
            "pages": 0, "limitations": ["downstream_context_reserve"],
        }
        events = list(session.scalars(select(Event).where(Event.run_id == run.id, Event.event_type == "research.unavailable")))
        assert len(events) == 1
        assert events[0].payload == {"reason": "downstream_context_reserve", "verified": False}
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.run_id == run.id, Outbox.kind == "advance")) == 1
        assert session.scalar(select(func.count()).select_from(BudgetOperation)) == 0


def test_one_byte_of_context_room_allows_synthetic_search(research_run, monkeypatch):
    monkeypatch.setattr(base, "MAX_INITIAL_PACKET_BYTES", 1512)
    monkeypatch.setattr(service, "proposal_packet_bytes", lambda *args: 999)
    with research_run.begin() as session:
        assert service.schedule_research(session, session.get(Run, "run_local"))
    class EmptySearch:
        def __init__(self):
            self.queries = []
        def search(self, query, **kwargs):
            self.queries.append(query)
            return []
    search = EmptySearch()
    assert service.execute_research("run_local", session_factory=research_run, search=search)
    assert len(search.queries) == 3
    with research_run() as session:
        assert session.scalar(select(func.count()).select_from(BudgetOperation).where(BudgetOperation.kind == "public_search")) == 3
