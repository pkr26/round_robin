"""Saved issue scope fields must agree with their committed opening."""
from copy import deepcopy

import pytest
from round_robin.database.models import ReportVersion
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.schemas.runtime import RuntimeDecisionReport
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import _saved_with_journal


@pytest.mark.parametrize('field', ['option_id', 'requirement_id', 'reference_ids'])
def test_saved_issue_scope_matches_committed_opening(factory, field):
    run_id = _saved_with_journal(factory)
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        content = deepcopy(row.content)
        issue = content['issues'][0]
        assert issue['option_id'] is None
        assert issue['requirement_id'] is None
        assert issue['reference_ids']
        replacements = {'option_id': content['comparison'][0]['option_id'], 'requirement_id': content['hard_requirement_ids'][0], 'reference_ids': []}
        issue[field] = replacements[field]
        RuntimeDecisionReport.model_validate(content)
        row.content = content
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, row)
