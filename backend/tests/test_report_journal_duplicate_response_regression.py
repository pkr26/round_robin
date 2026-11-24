"""A saved report cannot silently duplicate a recorded issue response."""
from copy import deepcopy

import pytest
from sqlalchemy import func, select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import _saved_with_journal
from round_robin.database.models import Event, ReportVersion
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.schemas.runtime import RuntimeDecisionReport


def test_duplicate_journal_response_is_rejected(factory):
    run_id = _saved_with_journal(factory)
    with factory.begin() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert read_runtime_report(session, report)['issues'] == report.content['issues']
        content = deepcopy(report.content)
        issue = next(issue for issue in content['issues'] if issue['status'] == 'resolved')
        final = issue['events'][-1]
        assert final['disposition'] == 'resolved'
        recorded = next(e for e in session.scalars(select(Event).where(
            Event.run_id == run_id, Event.event_type == 'discussion.obligation_recorded'))
            if e.payload['event']['id'] == final['id'])
        duplicate = deepcopy(final)
        duplicate['id'] = 'synthetic_duplicate_response'
        issue['events'].append(duplicate)
        payload = deepcopy(recorded.payload)
        payload['event'] = duplicate
        next_sequence = session.scalar(select(func.max(Event.sequence)).where(Event.run_id == run_id)) + 1
        session.add(Event(id='synthetic_duplicate_record', run_id=run_id,
                          sequence=next_sequence, event_type=recorded.event_type, payload=payload))
        report.event_cutoff = next_sequence
        content["event_cutoff"] = next_sequence
        RuntimeDecisionReport.model_validate(content)
        report.content = content
    with factory() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, report)
