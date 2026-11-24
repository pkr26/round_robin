"""A saved open issue cannot be marked as resolved by readiness dimensions."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from backend.tests.test_discussion_integrity import prepare_resolved_issue
from backend.tests.test_orchestration import factory
from round_robin.database.models import ReportVersion
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.schemas.runtime import RuntimeDecisionReport


def test_unresolved_issue_cannot_have_resolved_readiness(factory, monkeypatch):
    run_id = prepare_resolved_issue(factory, monkeypatch, severity='material', target_status='open')
    monkeypatch.setattr('round_robin.orchestration.controller._reserve_correction', lambda session, run: False)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        original = read_runtime_report(session, report)
        assert original['assessment']['unresolved_critical_defect'] is True
        assert any(i['severity'] == 'material' and i['status'] == 'open' for i in original['issues'])
        content = deepcopy(report.content)
        if content['readiness_dimensions'] is None:
            content['readiness_dimensions'] = {
                'complete_comparison': False, 'checked_arithmetic': False,
                'scoped_cost_support': False, 'requirements_assessed': False,
                'distinct_review': False, 'material_objections_resolved': False,
                'basis_scope': 'Synthetic owner assessment',
            }
        content['readiness_dimensions']['material_objections_resolved'] = True
        RuntimeDecisionReport.model_validate(content)
        report.content = content
    with factory() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, report)
