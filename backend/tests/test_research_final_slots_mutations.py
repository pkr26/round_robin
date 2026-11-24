"""Public research preserves the final passage slot without overfilling it."""
import pytest
from round_robin.database.models import EvidenceRecord, Run
from round_robin.research import service
from round_robin.research.fetcher import FetchResponse
from round_robin.research.search import SearchHit
from sqlalchemy import select

from backend.tests.test_research_origin_scope_mutations import evidence

pytest_plugins = ("test_research_service",)


@pytest.mark.parametrize("existing_count", [22, 23])
def test_final_passage_slots_are_not_overfilled_by_long_sources(research_run, existing_count):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
        session.add_all([evidence(run, f"evidence_existing_{i}", f"origin_existing_{i}", f"Existing source {i}")
                         for i in range(existing_count)])
    class Search:
        def __init__(self):
            self.count = 0
        def search(self, query, **kwargs):
            self.count += 1
            return [SearchHit("Synthetic", f"https://example.com/source{self.count}")]
    class Transport:
        def get(self, target, **kwargs):
            return FetchResponse(200, {"content-type": "text/plain"}, ("A" * 2000 + "B" * 2000).encode())
    search = Search()
    assert service.execute_research("run_local", session_factory=research_run, search=search,
                                    transport=Transport(), resolver=lambda _: ["93.184.216.34"])
    assert search.count == 24 - existing_count
    with research_run() as session:
        run = session.get(Run, "run_local")
        rows = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id)))
        assert len(rows) == 24
        assert {row.id for row in rows if row.content["origin"] == "user_supplied"} == {
            f"evidence_existing_{i}" for i in range(existing_count)
        }
        retrieved = [row for row in rows if row.content["origin"] == "retrieved"]
        assert len(retrieved) == 24 - existing_count
        assert all(row.content["verification_status"] == "not_checked" for row in retrieved)
        assert run.configuration["research_summary"]["pages"] == 24 - existing_count


def test_slot_filled_during_search_does_not_overfill_on_settlement(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
        session.add_all([evidence(run, f"evidence_existing_{i}", f"origin_existing_{i}", f"Existing source {i}")
                         for i in range(23)])
    class Search:
        def search(self, query, **kwargs):
            with research_run.begin() as session:
                run = session.get(Run, "run_local")
                session.add(evidence(run, "evidence_concurrent", "origin_concurrent", "Concurrent supplied source"))
            return [SearchHit("Synthetic", "https://example.com/source")]
    class Transport:
        def get(self, target, **kwargs):
            return FetchResponse(200, {"content-type": "text/plain"}, b"A synthetic fetched passage")
    assert service.execute_research("run_local", session_factory=research_run, search=Search(),
                                    transport=Transport(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        run = session.get(Run, "run_local")
        rows = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id)))
        assert len(rows) == 24
        assert all(row.content["origin"] == "user_supplied" for row in rows)
        assert "passage_packet_limit" in run.configuration["research_summary"]["limitations"]


@pytest.mark.parametrize(("existing_count", "expected"), [(0, [2, 2, 2]), (19, [1, 2, 2])])
def test_long_sources_share_remaining_passage_slots(research_run, existing_count, expected):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
        session.add_all([evidence(run, f"evidence_existing_{i}", f"origin_existing_{i}", f"Existing source {i}")
                         for i in range(existing_count)])
    class Search:
        def __init__(self):
            self.count = 0
        def search(self, query, **kwargs):
            self.count += 1
            return [SearchHit("Synthetic", f"https://example.com/source{self.count}")]
    class Transport:
        def get(self, target, **kwargs):
            return FetchResponse(200, {"content-type": "text/plain"}, ("A" * 2000 + "B" * 2000 + "C" * 2000).encode())
    search = Search()
    assert service.execute_research("run_local", session_factory=research_run, search=search,
                                    transport=Transport(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        run = session.get(Run, "run_local")
        rows = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id)))
        counts = {}
        for row in rows:
            if row.content["origin"] != "retrieved":
                continue
            url = row.content["url"]
            counts[url] = counts.get(url, 0) + 1
        assert search.count == 3
        assert len(counts) == 3
        assert [counts[f"https://example.com/source{i}"] for i in range(1, 4)] == expected
