"""A saved issue's terminal status must agree with its final journal event."""
from copy import deepcopy

import pytest
from sqlalchemy import select
from round_robin.database.models import ReportVersion
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.schemas.runtime import RuntimeDecisionReport
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import _saved_with_journal


def test_saved_terminal_status_matches_final_event(factory):
    run_id = _saved_with_journal(factory)
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert read_runtime_report(session, row)['issues'] == row.content['issues']
        content = deepcopy(row.content)
        issue = content['issues'][0]
        assert issue['status'] == issue['events'][-1]['disposition'] == 'resolved'
        issue['status'] = 'withdrawn'
        RuntimeDecisionReport.model_validate(content)
        row.content = content
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, row)
