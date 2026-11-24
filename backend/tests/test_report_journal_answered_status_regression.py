"""A saved issue's answered status must agree with its final journal event."""
from copy import deepcopy
from dataclasses import replace
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.contributions import DiscussionObjection, ObjectionResponse
from backend.tests.test_orchestration import drain, seed

import pytest
from sqlalchemy import select
from round_robin.database.models import ReportVersion
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.schemas.runtime import RuntimeDecisionReport
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import _saved_with_journal


def test_saved_answered_status_matches_final_event(factory):
    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            ref = min(request.allowed_reference_ids)
            output = result.output
            if request.stage == 'proposal' and self.provider == 'openai':
                output = output.model_copy(update={'objections': [DiscussionObjection(alias='new_objection_1', concern='Independent review is needed.', severity='minor', reference_ids=[ref])]})
            if request.stage in {'synthesis', 'audit'} and request.open_objections:
                output = output.model_copy(update={'objection_responses': [ObjectionResponse(objection_id=issue['id'], disposition='resolved', response='The passage addresses the question.', reference_ids=[ref]) for issue in request.open_objections if issue['severity'] == 'minor']})
            return replace(result, output=output)
    run_id = seed(factory)
    drain(factory, {p: Provider(p) for p in ('openai', 'anthropic', 'google')})
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert read_runtime_report(session, row)['issues'] == row.content['issues']
        content = deepcopy(row.content)
        issue = content['issues'][0]
        assert issue['status'] == 'open' and issue['events'][-1]['disposition'] == 'opened'
        issue['status'] = 'answered_pending_review'
        RuntimeDecisionReport.model_validate(content)
        row.content = content
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, row)
