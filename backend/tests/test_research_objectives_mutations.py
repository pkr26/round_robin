"""Research records unfulfilled objectives and fairly distributes its page budget."""
from types import SimpleNamespace

from round_robin.database.models import BudgetOperation, Run
from round_robin.research import service
from round_robin.research.fetcher import FetchResponse
from round_robin.research.search import SearchHit
from sqlalchemy import func, select

from backend.tests.test_research_origin_scope_mutations import evidence

pytest_plugins = ("test_research_service",)


class SyntheticPages:
    def get(self, target, **kwargs):
        return FetchResponse(200, {"content-type": "text/plain"}, ("Synthetic source " + target.url).encode())


class SyntheticSearch:
    def __init__(self):
        self.count = 0
    def search(self, query, **kwargs):
        self.count += 1
        return [SearchHit("Synthetic", f"https://example.com/query{self.count}/source{i}") for i in range(8)]


def test_page_budget_is_shared_across_all_three_approved_objectives(research_run):
    with research_run.begin() as session:
        assert service.schedule_research(session, session.get(Run, "run_local"))
    search = SyntheticSearch()
    assert service.execute_research("run_local", session_factory=research_run, search=search,
                                    transport=SyntheticPages(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        run = session.get(Run, "run_local")
        summary = run.configuration["research_summary"]
        plan = run.configuration["research_plan"]
        assert search.count == summary["queries"] == 3
        assert summary["pages"] == 8
        assert summary["source_count"] == 8
        assert summary["status"] == "retrieved_unverified"
        assert "research_objective_without_new_source" not in summary["limitations"]
        assert summary["objectives"] == [
            {"template_id": item["template_id"], "term_ids": item["term_ids"], "queried": True,
             "pages_attempted": pages, "sources_retrieved": pages}
            for item, pages in zip(plan, [2, 3, 3], strict=True)
        ]


def test_exhausted_page_capacity_preserves_unqueried_objectives_without_spend(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
        session.add_all([evidence(run, f"evidence_existing_{i}", f"origin_existing_{i}", f"Existing source {i}")
                         for i in range(23)])
    search = SyntheticSearch()
    assert service.execute_research("run_local", session_factory=research_run, search=search,
                                    transport=SyntheticPages(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        run = session.get(Run, "run_local")
        summary, plan = run.configuration["research_summary"], run.configuration["research_plan"]
        assert search.count == summary["queries"] == 1
        assert session.scalar(select(func.count()).select_from(BudgetOperation).where(BudgetOperation.kind == "public_search")) == 1
        assert "unfulfilled_research_objective" in summary["limitations"]
        assert summary["objectives"] == [
            {"template_id": item["template_id"], "term_ids": item["term_ids"], "queried": index == 0,
             "pages_attempted": 1 if index == 0 else 0, "sources_retrieved": 1 if index == 0 else 0}
            for index, item in enumerate(plan)
        ]


def test_exact_deadline_skips_remaining_objectives_without_spend(research_run, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(service, "research_seconds_available", lambda run: 70)
    with research_run.begin() as session:
        assert service.schedule_research(session, session.get(Run, "run_local"))
    class DeadlineSearch:
        def __init__(self):
            self.count = 0
        def search(self, query, **kwargs):
            self.count += 1
            clock.now = 1070.0
            return []
    search = DeadlineSearch()
    assert service.execute_research("run_local", session_factory=research_run, search=search)
    with research_run() as session:
        run = session.get(Run, "run_local")
        summary, plan = run.configuration["research_summary"], run.configuration["research_plan"]
        assert search.count == summary["queries"] == 1
        assert session.scalar(select(func.count()).select_from(BudgetOperation).where(BudgetOperation.kind == "public_search")) == 1
        assert "research_deadline" in summary["limitations"]
        assert "research_objective_without_new_source" in summary["limitations"]
        assert summary["objectives"] == [
            {"template_id": item["template_id"], "term_ids": item["term_ids"], "queried": index == 0,
             "pages_attempted": 0, "sources_retrieved": 0}
            for index, item in enumerate(plan)
        ]


def test_five_page_budget_reserves_later_objectives(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
        session.add_all([evidence(run, f"evidence_existing_{i}", f"origin_existing_{i}", f"Existing source {i}")
                         for i in range(19)])
    search = SyntheticSearch()
    assert service.execute_research("run_local", session_factory=research_run, search=search,
                                    transport=SyntheticPages(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        run = session.get(Run, "run_local")
        summary = run.configuration["research_summary"]
        assert search.count == summary["queries"] == 3
        assert summary["pages"] == 5
        assert [item["pages_attempted"] for item in summary["objectives"]] == [1, 2, 2]
        assert [item["sources_retrieved"] for item in summary["objectives"]] == [1, 2, 2]
