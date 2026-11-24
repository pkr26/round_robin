"""The journal reader binds a withdrawal to the issue's original provider."""
from copy import deepcopy

import pytest
from sqlalchemy import select
from round_robin.database.models import Attempt, Event, ReportVersion, Step
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.schemas.runtime import RuntimeDecisionReport
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import _saved_with_journal


def test_foreign_provider_cannot_withdraw_saved_issue(factory):
    run_id = _saved_with_journal(factory)
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert read_runtime_report(session, row)['issues'] == row.content['issues']
        content = deepcopy(row.content)
        issue = content['issues'][0]
        final = issue['events'][-1]
        assert issue['status'] == final['disposition'] == 'resolved'
        assert final['provider'] != issue['source_provider']
        assert final['reference_ids']
        issue['status'] = final['disposition'] = 'withdrawn'
        journal = next(e for e in session.scalars(select(Event).where(
            Event.run_id == run_id, Event.event_type == 'discussion.obligation_recorded'))
            if e.payload['event']['id'] == final['id'])
        payload = deepcopy(journal.payload)
        payload['event']['disposition'] = 'withdrawn'
        journal.payload = payload
        step = session.get(Step, final['step_id'])
        result = deepcopy(step.result_json)
        responses = [r for r in result['output']['objection_responses'] if r['objection_id'] == issue['id'] and r['response'] == final['response'] and r['reference_ids'] == final['reference_ids']]
        assert len(responses) == 1 and responses[0]['disposition'] == 'resolved'
        responses[0]['disposition'] = 'withdrawn'
        step.result_json = result
        if result.get('attempt_id'):
            attempt = session.get(Attempt, result['attempt_id'])
            attempt.response_json = deepcopy(result)
        RuntimeDecisionReport.model_validate(content)
        row.content = content
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, row)
