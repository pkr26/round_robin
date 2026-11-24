"""The persisted journal event must retain its actual committed step attribution."""
from copy import deepcopy

import pytest
from sqlalchemy import select
from round_robin.database.models import Event, ReportVersion
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.schemas.runtime import RuntimeDecisionReport
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import _saved_with_journal


@pytest.mark.parametrize('field', ['provider', 'stage'])
def test_coordinated_saved_event_attribution_disagrees_with_step(factory, field):
    run_id = _saved_with_journal(factory)
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert read_runtime_report(session, row)['issues'] == row.content['issues']
        content = deepcopy(row.content)
        issue = content['issues'][0]
        event = issue['events'][0]
        journal = next(e for e in session.scalars(select(Event).where(
            Event.run_id == run_id, Event.event_type == 'discussion.obligation_recorded'))
            if e.payload['event']['id'] == event['id'])
        assert journal.payload['event'] == event
        alternatives = {'provider': 'anthropic', 'stage': 'audit'}
        assert event[field] != alternatives[field]
        event[field] = alternatives[field]
        payload = deepcopy(journal.payload)
        payload['event'][field] = alternatives[field]
        journal.payload = payload
        RuntimeDecisionReport.model_validate(content)
        row.content = content
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, row)
