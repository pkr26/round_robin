"""Private resolved requirements guide local excerpts, never public search queries."""
from copy import deepcopy

import pytest
from round_robin.database.models import Run
from round_robin.research import service
from round_robin.research.fetcher import FetchResponse
from round_robin.research.search import SearchHit

pytest_plugins = ("test_research_service",)


def test_resolved_requirements_stay_local_while_approved_search_scope_is_used(research_run, monkeypatch):
    private_marker = "SYNTHETIC_PRIVATE_RELEVANCE"
    resolved_marker = "SYNTHETIC_RESOLVED_TERM"
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        resolved = deepcopy(run.input_snapshot)
        resolved["hard_requirements"][0]["description"] = private_marker
        resolved["public_research_terms"][0]["text"] = resolved_marker
        run.configuration = {**run.configuration, "resolved_brief": resolved}
        assert service.schedule_research(session, run)
    expected_terms = [item["description"] for item in resolved["hard_requirements"]] + [resolved_marker]
    original = service.exact_passages
    local_calls = []
    def observe(source, **kwargs):
        local_calls.append((kwargs.get("maximum"), list(kwargs.get("terms", ()))))
        return original(source, **kwargs)
    monkeypatch.setattr(service, "exact_passages", observe)
    class Search:
        def __init__(self):
            self.queries = []
        def search(self, query, **kwargs):
            self.queries.append(query)
            return [SearchHit("Synthetic", "https://example.com/source")]
    class Transport:
        def get(self, target, **kwargs):
            return FetchResponse(200, {"content-type": "text/plain"}, b"Synthetic public source text")
    search = Search()
    assert service.execute_research("run_local", session_factory=research_run, search=search,
                                    transport=Transport(), resolver=lambda _: ["93.184.216.34"])
    assert len(search.queries) == 3
    assert all("workshop label systems" in query for query in search.queries)
    assert all(private_marker not in query and resolved_marker not in query for query in search.queries)
    assert local_calls
    assert all(maximum == 2 and terms == expected_terms for maximum, terms in local_calls)


@pytest.mark.parametrize("missing", ["hard_requirements", "public_research_terms"])
def test_schema_valid_omitted_relevance_collections_default_empty(research_run, monkeypatch, missing):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        resolved = deepcopy(run.input_snapshot)
        resolved.pop(missing)
        run.configuration = {**run.configuration, "resolved_brief": resolved}
        assert service.schedule_research(session, run)
    expected = [item["description"] for item in resolved.get("hard_requirements", [])] + [
        item["text"] for item in resolved.get("public_research_terms", [])
    ]
    original = service.exact_passages
    calls = []
    def observe(source, **kwargs):
        calls.append(list(kwargs.get("terms", ())))
        return original(source, **kwargs)
    monkeypatch.setattr(service, "exact_passages", observe)
    class Search:
        def search(self, query, **kwargs):
            return [SearchHit("Synthetic", "https://example.com/source")]
    class Transport:
        def get(self, target, **kwargs):
            return FetchResponse(200, {"content-type": "text/plain"}, b"Synthetic public source text")
    assert service.execute_research("run_local", session_factory=research_run, search=Search(),
                                    transport=Transport(), resolver=lambda _: ["93.184.216.34"])
    assert calls == [expected]
