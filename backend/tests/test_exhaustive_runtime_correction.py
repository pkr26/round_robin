"""Fresh adversarial controller review: bounded correction and no-send settlement."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory, seed, drain
from round_robin.database.models import Attempt, BudgetOperation, Outbox, Run, Step, ReportVersion, utcnow
from round_robin.dispatcher.service import reconcile_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run, _reserve_correction
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


class CorrectingProvider(SimulatedProvider):
    def __init__(self, provider, *, always_negative=False):
        super().__init__(provider)
        self.requests = []
        self.always_negative = always_negative
        self.audits = 0

    def generate(self, request):
        self.requests.append(request)
        result = super().generate(request)
        if request.open_objections and request.stage in {'synthesis', 'audit'} and not self.always_negative:
            from round_robin.schemas.contributions import ObjectionResponse
            responses = [ObjectionResponse(objection_id=issue['id'], disposition='resolved' if request.stage == 'audit' else 'answered',
                response='Checked the supplied applicable schedule; preserve the qualification.', reference_ids=[request.evidence[0]['id']]) for issue in request.open_objections]
            result = replace(result, output=result.output.model_copy(update={'objection_responses': responses}))
        if request.stage == 'audit':
            self.audits += 1
            if self.audits == 1 or self.always_negative:
                return replace(result, output=result.output.model_copy(update={'audit_passed': False, 'critical_defects': ['The supplier schedule lacks supporting evidence.']}))
        return result


def test_negative_audit_gets_exactly_one_correction_and_independent_reaudit(factory):
    run_id = seed(factory)
    registry = {name: CorrectingProvider(name) for name in ('openai', 'anthropic', 'google')}
    drain(factory, registry)
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == 'completed' and run.configuration['stage_iteration'] == 1
        steps = list(session.scalars(select(Step).where(Step.run_id == run_id)))
        assert len(steps) == 8
        assert sum(step.stage == 'proposal' for step in steps) == 3
        assert sum(step.stage == 'intake' for step in steps) == 1
        for stage in ('synthesis', 'audit'):
            family = [step for step in steps if step.stage == stage]
            assert {step.stage_iteration for step in family} == {0, 1}
            assert sum(step.calls for step in family) == 2
        synthesis = next(step for step in steps if step.stage == 'synthesis' and step.stage_iteration == 1)
        audit = next(step for step in steps if step.stage == 'audit' and step.stage_iteration == 1)
        old_audit = next(step for step in steps if step.stage == 'audit' and step.stage_iteration == 0)
        assert synthesis.provider != audit.provider
        requests = [request for provider in registry.values() for request in provider.requests]
        correction = next(request for request in requests if request.step_id == synthesis.id)
        assert old_audit.id in correction.allowed_reference_ids
        assert next(peer for peer in correction.peer_outputs if peer['stage'] == 'audit')['output']['audit_passed'] is False
        reviewed = next(request for request in requests if request.step_id == audit.id)
        assert [peer['id'] for peer in reviewed.peer_outputs if peer['stage'] == 'synthesis'] == [synthesis.id]
        assert len(list(session.scalars(select(Attempt)))) == 8
        assert session.get(BudgetOperation, f'finish_{run_id}').status == 'released'
        report = session.scalar(select(ReportVersion)).content
        assert report['assessment']['audit_succeeded'] is True
        assert len(report['contribution_ids']) == 8


def test_repeated_negative_audit_stops_after_one_correction_without_resetting_limits(factory):
    run_id = seed(factory)
    registry = {name: CorrectingProvider(name, always_negative=True) for name in ('openai', 'anthropic', 'google')}
    drain(factory, registry)
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == 'completed_with_limitations'
        assert run.stop_reason == 'independent_audit_incomplete_or_negative'
        assert run.configuration['stage_iteration'] == 1
        assert len(list(session.scalars(select(Attempt)))) == 8
        assert session.scalar(select(ReportVersion)).content['assessment']['unresolved_critical_defect']


def correction_candidate(factory):
    run_id = seed(factory)
    drain(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.status = 'running'
        run.stage = 'audit'
        hold = session.get(BudgetOperation, f'finish_{run_id}')
        hold.status = 'held'
    return run_id


@pytest.mark.parametrize('boundary', ['iteration', 'calls', 'claims', 'requests', 'time', 'missing_hold', 'released_hold', 'remaining_roles', 'spending_policy'])
def test_correction_is_denied_when_any_original_resource_cannot_fund_both_stages(factory, monkeypatch, boundary):
    run_id = correction_candidate(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        config = dict(run.configuration)
        hold = session.get(BudgetOperation, f'finish_{run_id}')
        if boundary == 'iteration': config['stage_iteration'] = 1
        if boundary == 'calls': config['limits'] = {**config['limits'], 'max_calls_per_step': 1}
        if boundary == 'claims':
            session.scalar(select(Step).where(Step.stage == 'synthesis')).infrastructure_claims = 3
        if boundary == 'requests': config['limits'] = {**config['limits'], 'max_requests': 7}
        if boundary == 'time': run.active_seconds = config['limits']['active_seconds'] - 215
        if boundary == 'missing_hold': session.delete(hold); session.flush()
        if boundary == 'released_hold': hold.status = 'released'
        if boundary == 'remaining_roles': hold.metadata_json = {**hold.metadata_json, 'roles_remaining': ['audit']}
        if boundary == 'spending_policy':
            from round_robin.orchestration.budget import BudgetDenied
            def deny(*args, **kwargs): raise BudgetDenied('owner_budget_exhausted')
            monkeypatch.setattr('round_robin.orchestration.controller._policy_checks', deny)
        run.configuration = config
        assert _reserve_correction(session, run) is False
        assert run.stage == 'audit'


def test_correction_hold_adds_exposure_without_resetting_existing_held_money(factory, monkeypatch):
    run_id = correction_candidate(factory)
    seen = []
    monkeypatch.setattr('round_robin.orchestration.controller._policy_checks', lambda session, run, extra: seen.append(extra))
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.configuration = {**run.configuration, 'mode': 'live', 'finish_policy': {**run.configuration['finish_policy'], 'reserved_usd': '0.40'}}
        hold = session.get(BudgetOperation, f'finish_{run_id}')
        hold.reserved_usd = '0.07'
        # A funded run's original finish allocation remains recorded even
        # after both cheaper reviewed calls leave surplus.
        hold.metadata_json = {**hold.metadata_json, 'initial_reserved_usd': '0.40'}
        assert _reserve_correction(session, run)
        assert seen == [Decimal('0.40')]
        assert Decimal(hold.reserved_usd) == Decimal('0.47')
        assert hold.metadata_json['roles_remaining'] == ['synthesis', 'audit']


def test_heartbeat_thread_exhaustion_settles_proven_not_sent_without_provider_call(factory, monkeypatch):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id))
    provider = SimulatedProvider('openai')
    def unavailable(self): raise RuntimeError('cannot start new thread')
    monkeypatch.setattr('round_robin.worker.tasks.threading.Thread.start', unavailable)
    result = execute_step(step_id, session_factory=factory, adapter_registry={'openai': provider})
    assert result['status'] == 'persisted' and provider.calls == 0
    with factory() as session:
        attempt = session.scalar(select(Attempt))
        assert attempt.status == 'failed' and attempt.usage_certainty == 'proven_not_sent'
        operation = session.get(BudgetOperation, attempt.id)
        assert operation.status == 'settled' and Decimal(operation.settled_usd) == 0
        assert session.get(Step, step_id).lease_owner is None


def test_reconciler_does_not_consume_other_services_published_outbox_jobs(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        advance_run(session, run)
        for kind in ('image_submit', 'export', 'custom_future_family'):
            session.add(Outbox(id=f'foreign_{kind}', run_id=run_id, kind=kind, status='published', published_at=utcnow() - timedelta(minutes=1)))
    reconcile_once(session_factory=factory)
    with factory() as session:
        assert all(session.get(Outbox, f'foreign_{kind}').status == 'published' for kind in ('image_submit', 'export', 'custom_future_family'))
