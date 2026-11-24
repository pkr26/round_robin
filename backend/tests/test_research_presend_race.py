"""A post-authorization race must preserve honest search accounting."""

from round_robin.database.models import BudgetOperation, ReportVersion, Run
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.research import service
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend.tests.test_research_service import PageFixture, SearchFixture

pytest_plugins = ("backend.tests.test_research_service",)


def test_input_change_at_presend_commit_never_falsely_refunds_a_search(research_run, monkeypatch):
    with research_run.begin() as session:
        assert service.schedule_research(session, session.get(Run, "run_local"))
    original_append = service.append_event
    phase = {"authorized": False, "injected": False}

    def note_authorization(session, run, event_type, payload):
        result = original_append(session, run, event_type, payload)
        if event_type == "research.query_authorized":
            session.info["observe_authorization_commit"] = True
        return result

    def change_after_commit(session):
        if session.info.pop("observe_authorization_commit", False):
            phase["authorized"] = True
        elif phase["authorized"] and not phase["injected"]:
            phase["injected"] = True
            with research_run.begin() as other:
                run = other.get(Run, "run_local")
                run.input_snapshot = {**run.input_snapshot, "horizon_months": "synthetic-invalid-secret"}

    monkeypatch.setattr(service, "append_event", note_authorization)
    event.listen(Session, "after_commit", change_after_commit)
    search, pages = SearchFixture(), PageFixture()
    try:
        assert not service.execute_research("run_local", session_factory=research_run, search=search,
            transport=pages, resolver=lambda _: ["93.184.216.34"])
    finally:
        event.remove(Session, "after_commit", change_after_commit)
    assert phase["injected"] and len(search.queries) <= 1 and pages.calls == 0
    sent_count = len(search.queries)
    assert not service.execute_research("run_local", session_factory=research_run, search=search,
        transport=pages, resolver=lambda _: ["93.184.216.34"])
    assert len(search.queries) == sent_count and pages.calls == 0
    with research_run() as session:
        run = session.get(Run, "run_local")
        operation = session.scalar(select(BudgetOperation).where(BudgetOperation.kind == "public_search"))
        report = read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run.id)))
        assert run.status == "failed" and report["comparison"] == []
        assert "synthetic-invalid-secret" not in str(report)
        if search.queries:
            assert operation.status == "unknown"
            assert operation.id in report["usage"]["unknown_operation_ids"]
            assert report["usage"]["conservative_exposure_usd"] == "0.01"
        elif operation is not None:
            assert operation.status == "settled" and operation.settled_usd == "0"
