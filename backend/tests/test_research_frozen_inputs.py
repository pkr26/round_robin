import pytest
from round_robin.database.models import BudgetOperation, ReportVersion, Run
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.research import service
from round_robin.research.search import SearchHit
from round_robin.research.service import execute_research, schedule_research
from sqlalchemy import select

from backend.tests.test_research_service import PageFixture, SearchFixture
from backend.tests.test_research_service import research_run as research_run  # noqa: PLC0414


@pytest.mark.parametrize("corrupt", ["input", "resolved"])
def test_corrupt_original_brief_does_not_authorize_public_search(research_run, corrupt):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        run.configuration = {**run.configuration, "resolved_brief": dict(run.input_snapshot)}
        assert schedule_research(session, run)
        if corrupt == "input":
            run.input_snapshot = {**run.input_snapshot, "horizon_months": "private-invalid-value"}
        else:
            run.configuration = {**run.configuration, "resolved_brief": {**run.configuration["resolved_brief"], "horizon_months": "private-invalid-value"}}
    search = SearchFixture()
    execute_research("run_local", session_factory=research_run, search=search, transport=PageFixture(), resolver=lambda _: ["93.184.216.34"])
    assert search.queries == []
    with research_run() as session:
        assert not list(session.scalars(select(BudgetOperation).where(BudgetOperation.kind == "public_search")))
        run = session.get(Run, "run_local")
        assert run.status == "failed" and run.stop_reason == "invalid_frozen_inputs"
        assert read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run.id)))["comparison"] == []


def test_corruption_during_search_prevents_subsequent_queries(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        run.configuration = {**run.configuration, "resolved_brief": dict(run.input_snapshot)}
        assert schedule_research(session, run)

    class CorruptAfterFirstSearch(SearchFixture):
        def search(self, query, **kwargs):
            self.queries.append(query)
            if len(self.queries) == 1:
                with research_run.begin() as session:
                    run = session.get(Run, "run_local")
                    run.input_snapshot = {**run.input_snapshot, "horizon_months": "private-invalid-value"}
            return [SearchHit("Public quote", "https://example.com/quote")]

    search = CorruptAfterFirstSearch()
    pages = PageFixture()
    execute_research("run_local", session_factory=research_run, search=search, transport=pages, resolver=lambda _: ["93.184.216.34"])
    assert len(search.queries) == 1
    assert pages.calls == 0
    with research_run() as session:
        run = session.get(Run, "run_local")
        operations = list(session.scalars(select(BudgetOperation).where(BudgetOperation.kind == "public_search")))
        assert run.status == "failed" and run.stop_reason == "invalid_frozen_inputs"
        assert len(operations) == 1 and operations[0].status == "unknown"
        report = read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run.id)))
        assert report["usage"]["unknown_operation_ids"] == [operations[0].id]
        assert report["usage"]["conservative_exposure_usd"] == "0.01"


def test_corruption_after_authorization_before_send_releases_proven_unsent_operation(research_run, monkeypatch):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert schedule_research(session, run)
    original_append = service.append_event

    def corrupt_after_authorization(session, run, event_type, payload):
        result = original_append(session, run, event_type, payload)
        if event_type == "research.query_authorized":
            run.input_snapshot = {**run.input_snapshot, "horizon_months": "private-invalid-value"}
        return result

    monkeypatch.setattr(service, "append_event", corrupt_after_authorization)
    search = SearchFixture()
    assert not execute_research("run_local", session_factory=research_run, search=search, transport=PageFixture(), resolver=lambda _: ["93.184.216.34"])
    assert search.queries == []
    with research_run() as session:
        run = session.get(Run, "run_local")
        operation = session.scalar(select(BudgetOperation).where(BudgetOperation.kind == "public_search"))
        assert run.status == "failed" and run.stop_reason == "invalid_frozen_inputs"
        assert operation.status == "settled" and operation.settled_usd == "0"
        report = read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run.id)))
        assert report["usage"]["status"] == "reported" and report["usage"]["conservative_exposure_usd"] == "0"


def test_corruption_during_name_resolution_stops_page_fetch_and_keeps_search_exposure(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert schedule_research(session, run)
    search, pages = SearchFixture(), PageFixture()
    def corrupt_during_resolution(_):
        with research_run.begin() as session:
            run = session.get(Run, "run_local")
            run.input_snapshot = {**run.input_snapshot, "horizon_months": "private-invalid-value"}
        return ["93.184.216.34"]

    assert not execute_research("run_local", session_factory=research_run, search=search, transport=pages, resolver=corrupt_during_resolution)
    assert len(search.queries) == 1 and pages.calls == 0
    with research_run() as session:
        run = session.get(Run, "run_local")
        operation = session.scalar(select(BudgetOperation).where(BudgetOperation.kind == "public_search"))
        assert run.status == "failed" and run.stop_reason == "invalid_frozen_inputs"
        assert operation.status == "unknown"
