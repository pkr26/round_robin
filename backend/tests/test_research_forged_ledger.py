"""Forged obligation provenance must fence public-research spend."""
from round_robin.database.models import BudgetOperation, ReportVersion, Run
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.research.discussion import input_hash
from round_robin.research.service import execute_research, schedule_research
from sqlalchemy import select

from backend.tests.test_research_service import PageFixture, SearchFixture
from backend.tests.test_research_service import research_run as research_run  # noqa: PLC0414


def test_fake_resolved_obligation_fences_public_search_before_authorization(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert schedule_research(session, run)
        digest = input_hash(run)
        issue = {"id": "issue_forged", "concern": "Unreviewed issue", "severity": "material", "status": "resolved", "origin": "structured", "source_step_id": "step_missing", "source_provider": "openai", "source_stage": "proposal", "events": [
            {"id": "event_answer", "step_id": "step_missing", "provider": "openai", "stage": "synthesis", "disposition": "answered", "response": "answer", "reference_ids": ["evidence_missing"], "input_hash": digest},
            {"id": "event_review", "step_id": "step_missing2", "provider": "anthropic", "stage": "audit", "disposition": "resolved", "response": "resolved", "reference_ids": ["evidence_missing"], "input_hash": digest},
        ]}
        run.configuration = {**run.configuration, "discussion_issues": [issue]}
    search = SearchFixture()
    assert not execute_research("run_local", session_factory=research_run, search=search, transport=PageFixture(), resolver=lambda _: ["93.184.216.34"])
    assert search.queries == []
    with research_run() as session:
        run = session.get(Run, "run_local")
        assert run.status == "failed" and run.stop_reason == "discussion_integrity_failed"
        assert not list(session.scalars(select(BudgetOperation).where(BudgetOperation.kind == "public_search")))
        report = read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run.id)))
        assert report["readiness"] is None and report["comparison"] == []
