"""An actually generated pending answer remains readable in the saved report."""
from sqlalchemy import select
from round_robin.database.models import ReportVersion
from round_robin.orchestration.controller import advance_run
from round_robin.orchestration.admission import lock_work_run
from round_robin.reports.read_adapters import read_runtime_report
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_discussion_integrity import prepare_resolved_issue


def test_saved_answered_issue_is_readable(factory, monkeypatch):
    run_id = prepare_resolved_issue(factory, monkeypatch, target_status='answered_pending_review')
    monkeypatch.setattr('round_robin.orchestration.controller._reserve_correction', lambda session, run: False)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        issue = next(item for item in run.configuration['discussion_issues'] if item['concern'] == 'Delivery exclusions need a response.')
        assert issue['status'] == 'answered_pending_review'
        assert issue['events'][-1]['disposition'] == 'answered'
        advance_run(session, run)
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert row is not None
        report = read_runtime_report(session, row)
        saved = next(item for item in report['issues'] if item['id'] == issue['id'])
        assert saved['status'] == 'answered_pending_review'
        assert saved['events'][-1]['disposition'] == 'answered'
