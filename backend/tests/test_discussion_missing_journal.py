from dataclasses import replace

import pytest
from round_robin.database.models import Run
from round_robin.orchestration.budget import BudgetDenied
from round_robin.orchestration.configuration import verify_saved_discussion
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.contributions import DiscussionObjection, ObjectionResponse

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_removing_marker_does_not_hide_a_journaled_response(factory):
    class Provider(SimulatedProvider):
        def generate(self,request):
            result=super().generate(request)
            ref=min(request.allowed_reference_ids)
            output=result.output
            if request.stage=='proposal' and self.provider=='openai':
                output=output.model_copy(update={'objections':[DiscussionObjection(alias='new_objection_1',concern='Installation needs review',severity='material',reference_ids=[ref])]})
            if request.stage in {'synthesis','audit'} and request.open_objections:
                output=output.model_copy(update={'objection_responses':[ObjectionResponse(objection_id=x['id'],disposition='resolved',response='It is addressed.',reference_ids=[ref]) for x in request.open_objections if x['severity']=='material']})
            return replace(result,output=output)
    run_id=seed(factory); drain(factory,{p:Provider(p) for p in ('openai','anthropic','google')})
    with factory.begin() as session:
        run=session.get(Run,run_id); verify_saved_discussion(session,run)
        entries=run.configuration['discussion_issues']; issue=next(i for i in entries if i['concern']=='Installation needs review')
        assert len(issue['events'])==3
        corrupted=[{**i,'events':i['events'][:-1]} if i['id']==issue['id'] else i for i in entries]
        run.configuration={**{k:v for k,v in run.configuration.items() if k!='discussion_policy_version'},'discussion_issues':corrupted}
    with factory() as session, pytest.raises(BudgetDenied,match='discussion_integrity_failed'):
        verify_saved_discussion(session,session.get(Run,run_id))
