"""Open or pending material and critical issues require an unresolved assessment."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory
from backend.tests.test_discussion_integrity import prepare_resolved_issue
from round_robin.database.models import ReportVersion
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.schemas.runtime import RuntimeDecisionReport


@pytest.mark.parametrize('severity', ['critical', 'material'])
@pytest.mark.parametrize('status', ['open', 'answered_pending_review'])
def test_unresolved_assessment_cannot_hide_saved_issue(factory, monkeypatch, severity, status):
    run_id = prepare_resolved_issue(factory, monkeypatch, severity=severity, target_status=status)
    monkeypatch.setattr('round_robin.orchestration.controller._reserve_correction', lambda session, run: False)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert report is not None
        original = read_runtime_report(session, report)
        assert any(i['severity'] == severity and i['status'] == status for i in original['issues'])
        assert original['assessment']['unresolved_critical_defect'] is True
        content = deepcopy(report.content)
        content['assessment']['unresolved_critical_defect'] = False
        RuntimeDecisionReport.model_validate(content)
        report.content = content
    with factory() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, report)
