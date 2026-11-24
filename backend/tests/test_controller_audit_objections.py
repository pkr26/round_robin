"""Exercise the audit decision with real, committed objection history."""
import pytest
from round_robin.database.models import ReportVersion
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.reports.read_adapters import read_runtime_report
from sqlalchemy import select

from backend.tests.test_discussion_integrity import prepare_resolved_issue
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


@pytest.mark.parametrize("severity,status,expected", [
    ("material", "open", "completed_with_limitations"),
    ("critical", "open", "completed_with_limitations"),
    ("material", "answered_pending_review", "completed_with_limitations"),
    ("critical", "answered_pending_review", "completed_with_limitations"),
    ("minor", "open", "completed"),
    ("material", "resolved", "completed"),
])
def test_audit_objection_gate(factory, monkeypatch, severity, status, expected):
    run_id = prepare_resolved_issue(factory, monkeypatch, severity=severity, target_status=status)
    monkeypatch.setattr("round_robin.orchestration.controller._reserve_correction", lambda session, run: False)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        issue = next(item for item in run.configuration["discussion_issues"] if item["concern"] == "Delivery exclusions need a response.")
        assert issue["status"] == status
        advance_run(session, run)
        assert run.status == expected
        assert run.stop_reason == ("protocol_complete" if expected == "completed" else "independent_audit_incomplete_or_negative")
    with factory() as session:
        saved = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        issue = next(item for item in read_runtime_report(session, saved)["issues"] if item["concern"] == "Delivery exclusions need a response.")
        assert issue["status"] == status
