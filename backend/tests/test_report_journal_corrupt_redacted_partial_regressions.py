from copy import deepcopy
from dataclasses import replace

from round_robin.database.models import BudgetOperation, Event, ReportVersion, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.schemas.contributions import DiscussionObjection
from sqlalchemy import select

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_redacted_integrity_partial_remains_readable_after_saved_step_corruption(factory):
    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            if request.stage == 'proposal' and self.provider == 'openai':
                result = replace(result, output=result.output.model_copy(update={'objections': [DiscussionObjection(alias='new_objection_1',concern='Independent review needed.',severity='material',reference_ids=[])]}))
            return result
    run_id = seed(factory)
    drain(factory, {p:Provider(p) for p in ('openai','anthropic','google')})
    with factory.begin() as session:
        run = lock_work_run(session,run_id)
        issues = deepcopy(run.configuration['discussion_issues'])
        run.configuration = {**run.configuration, 'discussion_issues': issues + [issues[0]]}
        run.status = 'running'
        # Model a resumable simulated run with a live, unused finish hold.
        hold = session.get(BudgetOperation, f'finish_{run_id}')
        hold.status, hold.reserved_usd = 'held', '0'
        advance_run(session,run)
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id==run_id).order_by(ReportVersion.version.desc()).limit(1))
        committed = session.scalar(select(Event).where(
            Event.run_id == run_id, Event.event_type == 'content.committed'
        ))
        assert committed is not None
        step = session.get(Step, committed.payload['step_id'])
        assert step is not None
        # Model damage discovered after saving the fail-closed partial report.
        step.status, step.result_json = 'pending', None
        session.flush()
        report = read_runtime_report(session, row)
        assert report['execution_status'] == 'failed'
        assert report['readiness'] is None and report['issues'] == []
        assert report['assessment']['unresolved_critical_defect'] is True
