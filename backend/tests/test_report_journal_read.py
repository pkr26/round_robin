from copy import deepcopy
from dataclasses import replace

import pytest
from round_robin.database.models import Event, ReportVersion, Step
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.schemas.contributions import DiscussionObjection, ObjectionResponse
from sqlalchemy import select

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


@pytest.mark.parametrize("tamper", ["attribution", "omitted", "reordered", "source", "status", "event_text", "journal_text", "unresolved", "coordinated", "coordinated_step", "legacy_no_attempt", "after_cutoff", "missing_issue_and_journal"])
def test_saved_report_rejects_forged_audit_attribution(factory, tamper):
    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            ref = min(request.allowed_reference_ids)
            output = result.output
            if request.stage == 'proposal' and self.provider == 'openai':
                objections = [DiscussionObjection(alias='new_objection_1', concern='Installation inclusion needs independent review.', severity='material', reference_ids=[ref])]
                if tamper == 'missing_issue_and_journal':
                    objections.append(DiscussionObjection(alias='new_objection_2', concern='A separate minor observation.', severity='minor', reference_ids=[ref]))
                output = output.model_copy(update={'objections': objections})
            if request.stage in {'synthesis', 'audit'} and request.open_objections:
                output = output.model_copy(update={'objection_responses': [ObjectionResponse(objection_id=i['id'], disposition='answered' if tamper in {'coordinated', 'coordinated_step'} and request.stage == 'audit' else 'resolved', response='The supplied passage addresses the requirement.', reference_ids=[ref]) for i in request.open_objections if i['severity'] == 'material']})
            return replace(result, output=output)
    run_id = seed(factory)
    drain(factory, {p:Provider(p) for p in ('openai', 'anthropic', 'google')})
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        content = deepcopy(row.content)
        issue = next(i for i in content['issues'] if i['concern'] == 'Installation inclusion needs independent review.')
        assert issue['events'][0]['disposition'] == 'opened'
        assert issue['events'][-1]['disposition'] == ('answered' if tamper in {'coordinated', 'coordinated_step'} else 'resolved')
        if tamper == 'missing_issue_and_journal':
            omitted = next(i for i in content['issues'] if i['concern'] == 'A separate minor observation.')
            content['issues'].remove(omitted)
            removed = [event for event in session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type == 'discussion.obligation_recorded')) if event.payload['objection_id'] == omitted['id']]
            assert removed
            for event in removed:
                session.delete(event)
        elif tamper == 'attribution':
            issue['events'][-1]['step_id'] = issue['events'][-2]['step_id']
        elif tamper == 'omitted':
            issue['events'].pop(1)
        elif tamper == 'reordered':
            issue['events'][0], issue['events'][1] = issue['events'][1], issue['events'][0]
        elif tamper == 'source':
            issue['concern'] = 'PRIVATE_TAMPER_SENTINEL'
        elif tamper == 'status':
            issue['events'][-1]['disposition'] = 'answered'
        elif tamper == 'event_text':
            issue['events'][-1]['response'] = 'PRIVATE_TAMPER_SENTINEL'
        elif tamper == 'journal_text':
            journal_event = next(e for e in session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type == 'discussion.obligation_recorded')) if e.payload['event']['id'] == issue['events'][-1]['id'])
            payload = deepcopy(journal_event.payload)
            payload['event']['response'] = 'PRIVATE_TAMPER_SENTINEL'
            journal_event.payload = payload
        elif tamper == 'unresolved':
            issue['status'] = 'open'
        elif tamper in {'coordinated', 'coordinated_step'}:
            issue['status'] = 'resolved'
            issue['events'][-1]['disposition'] = 'resolved'
            journal_event = next(e for e in session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type == 'discussion.obligation_recorded')) if e.payload['event']['id'] == issue['events'][-1]['id'])
            payload = deepcopy(journal_event.payload)
            payload['event']['disposition'] = 'resolved'
            journal_event.payload = payload
            if tamper == 'coordinated_step':
                step = session.get(Step, issue['events'][-1]['step_id'])
                result = deepcopy(step.result_json)
                response = next(item for item in result['output']['objection_responses'] if item['objection_id'] == issue['id'])
                assert response['disposition'] == 'answered'
                response['disposition'] = 'resolved'
                step.result_json = result
        elif tamper == 'legacy_no_attempt':
            for step in session.scalars(select(Step).where(Step.run_id == run_id, Step.status == 'succeeded')):
                if step.result_json:
                    result = deepcopy(step.result_json)
                    result.pop('attempt_id', None)
                    step.result_json = result
        else:
            session.add(Event(id='later_event', run_id=run_id, sequence=row.event_cutoff + 100, event_type='discussion.obligation_recorded', payload={'objection_id':'later', 'event':{'id':'later'}}))
        row.content = content
        session.flush()
        if tamper in {'after_cutoff', 'legacy_no_attempt'}:
            assert read_runtime_report(session, row)['issues'] == content['issues']
        else:
            with pytest.raises(UnreadableReport, match='could not be safely read'):
                read_runtime_report(session, row)
