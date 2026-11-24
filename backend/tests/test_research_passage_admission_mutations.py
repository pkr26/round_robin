"""Each retrieved passage must fit the downstream proposal packet."""
from round_robin.database.models import EvidenceRecord, Run
from round_robin.providers import base
from round_robin.research import service
from round_robin.research.fetcher import FetchResponse
from round_robin.research.search import SearchHit
from sqlalchemy import select

pytest_plugins = ("test_research_service",)


def test_exact_fit_is_kept_and_packet_sizing_failure_is_excluded(research_run, monkeypatch):
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
            body = {"1": b"EXACT synthetic source", "2": b"OVER synthetic source", "3": b"SAFE synthetic source"}[target.url[-1]]
            return FetchResponse(200, {"content-type": "text/plain"}, body)
    def packet_size(session, run, passages):
        if not passages:
            return 1000
        text = passages[-1]["text"]
        if text.startswith("OVER"):
            raise base.ProviderFailure("synthetic_packet_too_large")
        return base.MAX_INITIAL_PACKET_BYTES if text.startswith("EXACT") else base.MAX_INITIAL_PACKET_BYTES - 1
    monkeypatch.setattr(service, "proposal_packet_bytes", packet_size)
    assert service.execute_research("run_local", session_factory=research_run, search=Search(),
                                    transport=Transport(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        run = session.get(Run, "run_local")
        rows = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id)))
        assert {row.content["text"] for row in rows} == {"EXACT synthetic source", "SAFE synthetic source"}
        assert "downstream_context_reserve" in run.configuration["research_summary"]["limitations"]


def test_later_fitting_passage_survives_earlier_sizing_failure(research_run, monkeypatch):
    with research_run.begin() as session:
        assert service.schedule_research(session, session.get(Run, "run_local"))
    class Search:
        def search(self, query, **kwargs):
            return [SearchHit("Synthetic", "https://example.com/source")]
    class Transport:
        def get(self, target, **kwargs):
            return FetchResponse(200, {"content-type": "text/plain"}, ("A" * 2000 + "B" * 2000).encode())
    def packet_size(session, run, passages):
        if not passages:
            return 1000
        if passages[-1]["text"].startswith("A"):
            raise base.ProviderFailure("synthetic_packet_too_large")
        return base.MAX_INITIAL_PACKET_BYTES - 1
    monkeypatch.setattr(service, "proposal_packet_bytes", packet_size)
    assert service.execute_research("run_local", session_factory=research_run, search=Search(),
                                    transport=Transport(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        run = session.get(Run, "run_local")
        rows = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id)))
        assert [row.content["text"] for row in rows] == ["B" * 2000]
        assert "downstream_context_reserve" in run.configuration["research_summary"]["limitations"]
