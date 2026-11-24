"""A saved report must reject a journal event with a negative sequence."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import _saved_with_journal
from round_robin.database.models import Event, ReportVersion
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report


def test_saved_report_rejects_negative_journal_sequence(factory):
    run_id = _saved_with_journal(factory)
    with factory.begin() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        expected_issues = deepcopy(report.content['issues'])
        assert expected_issues and expected_issues[0]['events']
        assert read_runtime_report(session, report)['issues'] == expected_issues
        first_id = expected_issues[0]['events'][0]['id']
        journal = list(session.scalars(select(Event).where(
            Event.run_id == run_id, Event.event_type == 'discussion.obligation_recorded')))
        matches = [event for event in journal if event.payload['event']['id'] == first_id]
        assert len(matches) == 1 and matches[0].sequence > 0
        assert session.scalar(select(Event).where(Event.run_id == run_id, Event.sequence == -1)) is None
        # Synthetic persisted corruption: the normal writer starts sequences at 1.
        matches[0].sequence = -1
    with factory() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert session.scalar(select(Event).where(Event.run_id == run_id, Event.sequence == -1)) is not None
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, report)
