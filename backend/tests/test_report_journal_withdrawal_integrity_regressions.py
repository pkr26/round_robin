"""Saved issue reports must retain response and unresolved-status integrity."""
from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import drain, factory, seed
from backend.tests.test_discussion_integrity import prepare_resolved_issue
from round_robin.database.models import Attempt, ReportVersion, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.schemas.contributions import DiscussionObjection, ObjectionResponse
from round_robin.schemas.runtime import RuntimeDecisionReport


def test_withdrawal_requires_committed_withdrawal(factory):
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
    with factory.begin() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert read_runtime_report(session, report)['issues'] == report.content['issues']
        issue = next(issue for issue in report.content['issues'] if issue['status'] == 'withdrawn')
        final = issue['events'][-1]
        step = session.get(Step, final['step_id'])
        result = deepcopy(step.result_json)
        responses = [response for response in result['output']['objection_responses']
                     if response['objection_id'] == issue['id'] and response['response'] == final['response']
                     and response['reference_ids'] == final['reference_ids'] and response['disposition'] == 'withdrawn']
        assert len(responses) == 1
        responses[0]['disposition'] = 'answered'
        step.result_json = result
        attempt = session.get(Attempt, result['attempt_id'])
        assert attempt is not None
        attempt.response_json = deepcopy(result)
    with factory() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, report)


def test_pending_material_issue_cannot_claim_no_unresolved_issue(factory, monkeypatch):
    run_id = prepare_resolved_issue(factory, monkeypatch, target_status='answered_pending_review')
    monkeypatch.setattr('round_robin.orchestration.controller._reserve_correction', lambda session, run: False)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert read_runtime_report(session, report)['assessment']['unresolved_critical_defect'] is True
        content = deepcopy(report.content)
        assert any(issue['severity'] in {'critical', 'material'} and issue['status'] == 'answered_pending_review'
                   for issue in content['issues'])
        content['assessment']['unresolved_critical_defect'] = False
        RuntimeDecisionReport.model_validate(content)
        report.content = content
    with factory() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        with pytest.raises(UnreadableReport, match='could not be safely read'):
            read_runtime_report(session, report)
