"""The terminal event owns the reference requirement for a saved issue."""
from dataclasses import replace

from sqlalchemy import select

from round_robin.database.models import ReportVersion
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.schemas.contributions import DiscussionObjection, ObjectionResponse
from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_terminal_resolution_does_not_require_prior_answer_references(factory):
    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            ref = min(request.allowed_reference_ids)
            output = result.output
            if request.stage == 'proposal' and self.provider == 'openai':
                output = output.model_copy(update={'objections': [DiscussionObjection(
                    alias='new_objection_1', concern='Independent review is needed.',
                    severity='material', reference_ids=[ref])]})
            if request.stage in {'synthesis', 'audit'} and request.open_objections:
                refs = [] if request.stage == 'synthesis' else [ref]
                output = output.model_copy(update={'objection_responses': [ObjectionResponse(
                    objection_id=issue['id'], disposition='resolved',
                    response='The passage addresses the question.', reference_ids=refs)
                    for issue in request.open_objections if issue['severity'] == 'material']})
            return replace(result, output=output)
    run_id = seed(factory)
    drain(factory, {p: Provider(p) for p in ('openai', 'anthropic', 'google')})
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        issue = row.content['issues'][0]
        assert issue['status'] == 'resolved'
        assert len(issue['events']) > 2
        assert issue['events'][1]['disposition'] == 'answered'
        assert issue['events'][1]['reference_ids'] == []
        assert issue['events'][-1]['reference_ids']
        assert read_runtime_report(session, row)['issues'] == row.content['issues']
