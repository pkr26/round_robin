"""Public-research settlement never implies that retrieved sources were verified."""
import pytest
from round_robin.database.models import Event, EvidenceRecord, Run
from round_robin.research import service
from round_robin.research.fetcher import FetchResponse
from round_robin.research.search import SearchHit
from sqlalchemy import select

pytest_plugins = ("test_research_service",)


@pytest.mark.parametrize("source_status", [200, 503])
def test_settlement_event_matches_saved_results_and_stays_unverified(research_run, source_status):
    with research_run.begin() as session:
        assert service.schedule_research(session, session.get(Run, "run_local"))
    class Search:
        def __init__(self):
            self.count = 0
        def search(self, query, **kwargs):
            self.count += 1
            return [SearchHit("Synthetic", f"https://example.com/source{self.count}")]
    class Transport:
        def get(self, target, **kwargs):
            return FetchResponse(source_status, {"content-type": "text/plain"}, b"Synthetic retrieved text")
    search = Search()
    assert service.execute_research("run_local", session_factory=research_run, search=search,
                                    transport=Transport(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        run = session.get(Run, "run_local")
        summary = run.configuration["research_summary"]
        assert run.configuration["research_state"] == "done"
        assert run.configuration["research_lease_id"] is None
        assert summary["queries"] == summary["pages"] == search.count == 3
        assert summary["source_count"] == (3 if source_status == 200 else 0)
        assert summary["status"] == ("retrieved_unverified" if source_status == 200 else "limited")
        events = list(session.scalars(select(Event).where(Event.run_id == run.id, Event.event_type == "research.settled")))
        assert len(events) == 1
        assert events[0].payload == {
            "queries": summary["queries"], "pages": summary["pages"], "source_count": summary["source_count"],
            "semantic_verification": False, "limitations": summary["limitations"],
        }
        evidence = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id)))
        assert len(evidence) == (3 if source_status == 200 else 0)
        assert all(row.content["verification_status"] == "not_checked" and row.content["time_sensitive"] is True
                   for row in evidence)
        if source_status == 503:
            assert "source_unavailable_or_rejected" in summary["limitations"]
