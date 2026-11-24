"""Distinct evidence keeps its provenance and duplicate passages do not stop scanning."""
import hashlib

from round_robin.database.models import EvidenceRecord, Run
from round_robin.research import service
from round_robin.research.fetcher import FetchResponse
from round_robin.research.search import SearchHit
from sqlalchemy import select

from backend.tests.test_research_origin_scope_mutations import evidence

pytest_plugins = ("test_research_service",)


def test_duplicate_first_passage_does_not_hide_later_unique_passage(research_run):
    url = "https://example.com/source"
    first_identity = "evidence_" + hashlib.sha256(f"run_local:{url}:extracted_text[0:2000]".encode()).hexdigest()[:40]
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
        session.add(evidence(run, first_identity, "origin_previous", "A" * 2000))
    class Search:
        def search(self, query, **kwargs):
            return [SearchHit("Synthetic", url)]
    class Transport:
        def get(self, target, **kwargs):
            return FetchResponse(200, {"content-type": "text/plain"}, ("A" * 2000 + "B" * 2000).encode())
    assert service.execute_research("run_local", session_factory=research_run, search=Search(),
                                    transport=Transport(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        rows = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == "run_local")))
        assert {row.content["text"] for row in rows} == {"A" * 2000, "B" * 2000}
        assert session.get(EvidenceRecord, first_identity).content["origin_group"] == "origin_previous"


def test_distinct_texts_from_distinct_hosts_keep_distinct_origin_groups(research_run):
    with research_run.begin() as session:
        assert service.schedule_research(session, session.get(Run, "run_local"))
    class Search:
        def __init__(self):
            self.count = 0
        def search(self, query, **kwargs):
            self.count += 1
            return [SearchHit("Synthetic", f"https://source{self.count}.example.com/page")]
    class Transport:
        def get(self, target, **kwargs):
            return FetchResponse(200, {"content-type": "text/plain"}, ("Synthetic text " + target.hostname).encode())
    assert service.execute_research("run_local", session_factory=research_run, search=Search(),
                                    transport=Transport(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        rows = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == "run_local")))
        assert len(rows) == 3
        assert len({row.sha256 for row in rows}) == 3
        assert len({row.content["origin_group"] for row in rows}) == 3
        assert all(row.content["verification_status"] == "not_checked" for row in rows)
