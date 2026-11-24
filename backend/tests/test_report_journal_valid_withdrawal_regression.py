"""An issue's originator may withdraw it with scoped evidence."""
from dataclasses import replace
from sqlalchemy import select
from round_robin.database.models import ReportVersion
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.schemas.contributions import DiscussionObjection, ObjectionResponse
from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_originator_withdrawal_is_readable(factory):
    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            ref = min(request.allowed_reference_ids)
            output = result.output
            if request.stage == 'proposal':
                output = output.model_copy(update={'objections': [DiscussionObjection(
                    alias='new_objection_1', concern=f'{self.provider} needs review.',
                    severity='material', reference_ids=[ref])]})
            if request.stage in {'synthesis', 'audit'}:
                output = output.model_copy(update={'objection_responses': [ObjectionResponse(
                    objection_id=issue['id'], disposition='withdrawn',
                    response='The concern is withdrawn.', reference_ids=[ref])
                    for issue in request.open_objections if issue['source_provider'] == self.provider]})
            return replace(result, output=output)
    run_id = seed(factory)
    drain(factory, {p: Provider(p) for p in ('openai', 'anthropic', 'google')})
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        withdrawals = [issue for issue in row.content['issues'] if issue['status'] == 'withdrawn']
        assert withdrawals
        for issue in withdrawals:
            assert issue['events'][-1]['provider'] == issue['source_provider']
            assert issue['events'][-1]['reference_ids']
        assert read_runtime_report(session, row)['issues'] == row.content['issues']
