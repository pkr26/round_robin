"""Only validated search candidates are durably registered before fetching."""
import json

import pytest
from round_robin.database.models import BudgetOperation, Run
from round_robin.research import service
from round_robin.research.fetcher import FetchResponse
from round_robin.research.search import SearchHit
from sqlalchemy import select

pytest_plugins = ("test_research_service",)


def test_validated_candidates_are_saved_before_fetch_and_survive_interruption(research_run):
    with research_run.begin() as session:
        assert service.schedule_research(session, session.get(Run, "run_local"))
    urls = ["https://example.com/source1", "https://example.com/source2"]
    rejected = "https://example.com/private?token=SYNTHETIC_SECRET"
    class Search:
        def search(self, query, **kwargs):
            return [SearchHit("Rejected", rejected), *(SearchHit("Synthetic", url) for url in urls)]
    class Transport:
        def __init__(self):
            self.snapshots = []
        def get(self, target, **kwargs):
            with research_run() as session:
                run = session.get(Run, "run_local")
                self.snapshots.append((target.url, run.configuration))
            if len(self.snapshots) == 2:
                raise RuntimeError("synthetic fetch interruption")
            return FetchResponse(503, {}, b"")
    transport = Transport()
    with pytest.raises(RuntimeError, match="synthetic fetch interruption"):
        service.execute_research("run_local", session_factory=research_run, search=Search(),
                                 transport=transport, resolver=lambda _: ["93.184.216.34"])
    assert [url for url, _ in transport.snapshots] == urls
    for index, (url, configuration) in enumerate(transport.snapshots):
        records = list(configuration["research_candidates"].values())
        assert records == [{"url": candidate, "origin": "server_recorded_search_result"}
                           for candidate in urls[:index + 1]]
        assert configuration["research_summary"] == {"status": "running", "queries": 1, "pages": index + 1}
        assert url == records[-1]["url"]
        assert "SYNTHETIC_SECRET" not in json.dumps(configuration)
    with research_run() as session:
        run = session.get(Run, "run_local")
        assert run.configuration["research_candidates"] == transport.snapshots[-1][1]["research_candidates"]
        operations = list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run.id,
                                                                         BudgetOperation.kind == "public_search")))
        assert len(operations) == 1 and operations[0].status == "unknown"
