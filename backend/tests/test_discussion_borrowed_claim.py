from dataclasses import replace

import pytest
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import Run, Step
from round_robin.orchestration.budget import BudgetDenied
from round_robin.orchestration.configuration import verify_saved_discussion
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.contributions import Claim, DiscussionObjection
from sqlalchemy import select

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_local_claim_must_belong_to_source_step(factory):
    class Provider(SimulatedProvider):
        def generate(self,request):
            result=super().generate(request)
            if request.stage == 'proposal' and self.provider == 'openai':
                claim=Claim(alias='new_claim_1',proposition='Installation is included.',kind='assumption',evidence_status='not_checked',reference_ids=[])
                objection=DiscussionObjection(alias='new_objection_1',concern='Check installation.',severity='minor',reference_ids=['new_claim_1'])
                return replace(result,output=result.output.model_copy(update={'claims':[claim],'objections':[objection]}))
            return result
    run_id=seed(factory)
    registry={p:Provider(p) for p in ('openai','anthropic','google')}
    drain(factory,registry)
    with factory.begin() as session:
        run=session.get(Run,run_id)
        assert run.status == 'completed'
        verify_saved_discussion(session,run)
        claim=session.scalar(select(ClaimVersion).where(ClaimVersion.run_id==run.id))
        assert claim is not None
        other=session.scalar(select(Step).where(Step.run_id==run.id,Step.status=='succeeded',Step.id!=claim.step_id))
        claim.step_id=other.id
        session.flush()
    with factory() as session:
        run=session.get(Run,run_id)
        with pytest.raises(BudgetDenied, match='discussion_integrity_failed'):
            verify_saved_discussion(session,run)
