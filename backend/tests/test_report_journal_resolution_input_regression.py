"""A resolved issue must be backed by an answer to the same input."""
from copy import deepcopy

import pytest
from sqlalchemy import select
from round_robin.database.models import Event, ReportVersion
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.schemas.runtime import RuntimeDecisionReport
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import _saved_with_journal


def test_resolution_requires_answer_for_same_input(factory):
    run_id = _saved_with_journal(factory)
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert read_runtime_report(session, row)['issues'] == row.content['issues']
        content = deepcopy(row.content)
        issue = content['issues'][0]
        final = issue['events'][-1]
        assert issue['status'] == final['disposition'] == 'resolved'
        assert final['stage'] == 'audit'
        answers = [e for e in issue['events'][:-1] if e['disposition'] == 'answered'
                   and e['provider'] != final['provider'] and e['input_hash'] == final['input_hash']]
        assert len(answers) == 1
        answer = answers[0]
        answer['input_hash'] = '0' * 64 if answer['input_hash'] != '0' * 64 else '1' * 64
        journal = next(e for e in session.scalars(select(Event).where(
            Event.run_id == run_id, Event.event_type == 'discussion.obligation_recorded'))
            if e.payload['event']['id'] == answer['id'])
        payload = deepcopy(journal.payload)
        payload['event']['input_hash'] = answer['input_hash']
        journal.payload = payload
        RuntimeDecisionReport.model_validate(content)
        row.content = content
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, row)
