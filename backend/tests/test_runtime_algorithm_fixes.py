"""Cross-transition regressions from the discussion-algorithm audit."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.tests.test_orchestration import elapse_retry_wait, factory, seed
from round_robin.api.events import clarify
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, Outbox, Run, Step, Upload, utcnow
from round_robin.database.services import enqueue
from round_robin.dispatcher.service import dispatch_once
from round_robin.fixtures import sample_brief
from round_robin.orchestration.admission import ensure_queue_capacity, lock_work_run
from round_robin.orchestration.budget import BudgetDenied
from round_robin.orchestration.controller import advance_run, intake_evidence_view
from round_robin.orchestration.protocol import correction_required, next_stage, policy_descriptor, recovery_fits, verify_policy
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.application import Clarification
from round_robin.worker.tasks import execute_step


def proposals(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.stage = 'proposals'
        advance_run(session, run)
        steps = {step.provider: step.id for step in session.scalars(select(Step))}
    return run_id, steps


class RateLimited(SimulatedProvider):
    def __init__(self, provider, delay):
        super().__init__(provider)
        self.delay = delay
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        raise ProviderFailure('rate_limit', retryable=True, retry_after=self.delay, may_have_sent=False, cost_usd=0)


def test_duplicate_delivery_cannot_bypass_due_time_or_retry_generation(factory):
    run_id, steps = proposals(factory)
    adapter = RateLimited('openai', 120)
    registry = {'openai': adapter}
    execute_step(steps['openai'], session_factory=factory, adapter_registry=registry, dispatch_generation=0)
    assert execute_step(steps['openai'], session_factory=factory, adapter_registry=registry, dispatch_generation=0)['status'] == 'stale_dispatch_generation'
    assert execute_step(steps['openai'], session_factory=factory, adapter_registry=registry, dispatch_generation=1)['status'] == 'retry_wait'
    assert execute_step(steps['openai'], session_factory=factory, adapter_registry=registry)['status'] == 'retry_wait'
    with factory.begin() as session:
        run = session.get(Run, run_id)
        controls = dict(run.configuration['step_dispatch'])
        controls[steps['openai']] = {**controls[steps['openai']], 'not_before': (utcnow() - timedelta(seconds=1)).isoformat()}
        run.configuration = {**run.configuration, 'step_dispatch': controls}
        assert session.get(Step, steps['openai']).calls == 1
        assert len(list(session.scalars(select(Attempt)))) == 1
    execute_step(steps['openai'], session_factory=factory, adapter_registry=registry, dispatch_generation=1)
    assert adapter.calls == 2


def test_long_optional_retry_is_declined_before_it_spends_finishing_time(factory):
    run_id, steps = proposals(factory)
    registry = {p: SimulatedProvider(p) for p in steps}
    registry['openai'] = RateLimited('openai', 500)
    for provider in steps:
        execute_step(steps[provider], session_factory=factory, adapter_registry=registry)
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.stage == 'synthesis'
        assert session.get(Step, steps['openai']).status == 'failed'
        synthesis = session.scalar(select(Step).where(Step.stage == 'synthesis'))
        synthesis_id = synthesis.id
    registry['openai'] = SimulatedProvider('openai')
    execute_step(synthesis_id, session_factory=factory, adapter_registry=registry)
    with factory() as session:
        assert session.get(Step, synthesis_id).status == 'succeeded'
        assert session.get(Run, run_id).stage == 'audit'


def test_cutoff_retires_a_pending_optional_proposal_and_keeps_two_valid_inputs(factory):
    run_id, steps = proposals(factory)
    registry = {p: SimulatedProvider(p) for p in steps}
    for provider in ('openai', 'anthropic'):
        execute_step(steps[provider], session_factory=factory, adapter_registry=registry)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.active_seconds = 390
        run.active_started_at = None
        advance_run(session, run)
        assert session.get(Step, steps['google']).error_code == 'mandatory_finish_cutoff'
        assert run.stage == 'synthesis'
        assert len(list(session.scalars(select(Attempt)))) == 2


def test_obsolete_outbox_delivery_is_retired_before_publication(factory):
    run_id, steps = proposals(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, 'step_dispatch': {steps['openai']: {'generation': 2}}}
        enqueue(session, run, 'execute', step_id=steps['openai'])
    sent = []
    dispatch_once(session_factory=factory, sender=lambda kind, payload: sent.append((kind, payload)))
    openai = [payload for kind, payload in sent if kind == 'execute' and payload['step_id'] == steps['openai']]
    assert openai == [{'step_id': steps['openai'], 'dispatch_generation': 2}]


def test_clarification_cannot_exceed_existing_owner_queue(factory):
    run_id = seed(factory, suffix='waiting')
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.status = 'awaiting_input'
        run.first_admitted_at = utcnow()
        run.input_expires_at = utcnow() + timedelta(hours=24)
    for index in range(3):
        seed(factory, suffix=f'queued_{index}')
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=Settings())), state=SimpleNamespace())
    with factory() as session, pytest.raises(HTTPException) as error:
        clarify(run_id, Clarification(command_id=uuid4(), expected_run_version=1, brief=sample_brief()), request, owner='owner_test', session=session)
    assert error.value.status_code == 429
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == 'awaiting_input' and run.resumption_expires_at is None


def test_queue_guard_enforces_global_bound_and_excludes_same_run(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        ensure_queue_capacity(session, 'owner_test', owner_limit=1, run_id=run_id)
        with pytest.raises(BudgetDenied, match='owner_queue_limit'):
            ensure_queue_capacity(session, 'owner_test', owner_limit=1)
    for index in range(19):
        seed(factory, suffix=f'global_{index}')
    with factory() as session, pytest.raises(BudgetDenied, match='deployment_queue_limit'):
        ensure_queue_capacity(session, 'different_owner')


def test_intake_receives_selected_text_without_freezing_the_brief(factory):
    import hashlib
    text = 'Supplier quoted delivery is 42 days; ask whether the deadline is flexible.'
    brief = sample_brief().model_copy(update={'selected_attachment_ids': ['upload_intake']})
    run_id = seed(factory, brief=brief)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        digest = hashlib.sha256(text.encode()).hexdigest()
        session.add(Upload(id='upload_intake', meeting_id=run.meeting_id, owner_id=run.owner_id,
            filename='supplier.txt', media_type='text/plain', byte_size=len(text.encode()), status='ready', canonical_text=text,
            sha256=digest, recipients=['openai', 'anthropic', 'google']))
        run.configuration = {**run.configuration, 'input_manifest': [{'id': 'upload_intake', 'sha256': digest}]}
        session.flush()
        _, evidence = intake_evidence_view(session, run)
        assert any(item['text'] == text for item in evidence)
        assert not run.configuration.get('evidence_prepared')
        assert 'resolved_brief' not in run.configuration


def test_shared_policy_has_finite_targeted_correction_and_capacity_aware_envelope():
    assert correction_required(audit_passed=False, critical_defects=['Missing applicable schedule'], iteration=0)
    assert not correction_required(audit_passed=False, critical_defects=['Same blocker'], iteration=1)
    assert not correction_required(audit_passed=False, critical_defects=[], iteration=0)
    assert correction_required(audit_passed=True, open_objections=[{'severity': 'critical', 'status': 'open'}])
    assert not recovery_fits(stage='proposal', remaining_seconds=600, wait_seconds=500)
    assert recovery_fits(stage='proposal', remaining_seconds=600, wait_seconds=120)
    assert not recovery_fits(stage='proposal', remaining_seconds=600, wait_seconds=120, capacity_delay_seconds=180)
    assert verify_policy({'discussion_policy': policy_descriptor()})
    assert verify_policy({})
    assert not verify_policy({'discussion_policy': {'version': 'unknown', 'sha256': '0'*64}})


def test_shared_stage_policy_keeps_profiles_distinct_and_counts_provider_identity():
    assert next_stage('proposal', ['openai', 'openai']) == 'publication'
    assert next_stage('proposal', ['openai', 'google']) == 'synthesis'
    assert next_stage('proposal', ['openai'], profile='single_provider') == 'synthesis'
    assert next_stage('proposal', ['openai', 'google'], profile='bounded_debate') == 'critique'
    assert next_stage('critique', profile='bounded_debate') == 'evidence_review'
    assert next_stage('evidence_review', profile='bounded_debate', material_findings=True) == 'revision'
    assert next_stage('synthesis', ['openai']) == 'audit'
    assert next_stage('audit', correction_admitted=True) == 'synthesis'
    assert next_stage('audit') == 'publication'
    assert len({policy_descriptor(profile)['sha256'] for profile in ('single_provider', 'independent_review', 'bounded_debate')}) == 3


def test_schema_repair_receives_the_saved_validation_diagnostic(factory):
    run_id, steps = proposals(factory)
    class Repairing(SimulatedProvider):
        requests = []
        def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise ProviderFailure('invalid_reference', retryable=True, cost_usd=0,
                    diagnostics=({'code': 'invalid_reference', 'path': '$.constraints[0].basis_refs'},))
            return super().generate(request)
    adapter = Repairing('openai')
    execute_step(steps['openai'], session_factory=factory, adapter_registry={'openai': adapter})
    elapse_retry_wait(factory, steps['openai'])
    execute_step(steps['openai'], session_factory=factory, adapter_registry={'openai': adapter})
    assert adapter.requests[1].repair
    assert adapter.requests[1].repair_diagnostics == ({'code': 'invalid_reference', 'path': '$.constraints[0].basis_refs'},)


def test_intake_change_requires_confirmation_and_does_not_mutate_the_brief(factory):
    from round_robin.providers.base import IntakeChange
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id))
    class Proposing(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            return replace(result, output=result.output.model_copy(update={'proposed_changes': [IntakeChange(
                field='horizon_months', proposed_value_json='36', reason='Compare a longer operating horizon.', reference_ids=[])]}))
    execute_step(step_id, session_factory=factory, adapter_registry={'openai': Proposing('openai')})
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == 'awaiting_input'
        assert run.input_snapshot['horizon_months'] == sample_brief().horizon_months
        assert run.configuration['proposed_changes'][0]['proposed_value_json'] == '36'
        assert run.configuration['questions']


def test_rejected_discussion_output_does_not_leave_claims_from_failed_contribution(factory):
    from round_robin.database.claim_models import ClaimVersion
    from round_robin.schemas.contributions import Claim, DiscussionObjection
    run_id, steps = proposals(factory)
    class Inconsistent(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            return replace(result, output=result.output.model_copy(update={
                'claims': [Claim(alias='new_claim_1', proposition='Quoted supplied amount.', kind='fact', evidence_status='supported', reference_ids=[request.evidence[0]['id']])],
                'objections': [DiscussionObjection(alias='new_objection_1', concern='Foreign scope.', severity='critical', target_reference_id='foreign_reference', reference_ids=[])]}))
    execute_step(steps['openai'], session_factory=factory, adapter_registry={'openai': Inconsistent('openai')})
    with factory() as session:
        assert session.get(Step, steps['openai']).error_code == 'claim_integrity_failed'
        assert list(session.scalars(select(ClaimVersion))) == []
