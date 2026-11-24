from datetime import timedelta

import pytest

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Attempt, BudgetOperation, Run, Step, utcnow
from round_robin.dispatcher.service import _expire_step, reconcile_once
from round_robin.orchestration.budget import BudgetDenied


def test_expiring_owned_step_does_not_mutate_foreign_run_attempt_or_ledger(factory):
    own, other = seed(factory, suffix='own'), seed(factory, suffix='other')
    with factory.begin() as session:
        step = Step(id='step_owned', run_id=own, stage='proposal', role_slot_id='proposal_1', provider='openai',
                    status='authorized', lease_expires_at=utcnow()-timedelta(seconds=5))
        session.add(step)
        session.flush()
        session.add(Attempt(id='attempt_foreign', run_id=other, step_id=step.id, provider='openai', status='authorized',
                            authorized_at=utcnow(), reserved_usd='0.40', configuration={}))
        session.add(BudgetOperation(id='attempt_foreign', run_id=other, owner_id='owner_test', kind='model', status='reserved',
                                    reserved_usd='0.40', metadata_json={'model_call': True}))
    try:
        with factory.begin() as session:
            _expire_step(session, session.get(Run, own), session.get(Step, 'step_owned'))
    except BudgetDenied:
        pass
    with factory() as session:
        attempt = session.get(Attempt, 'attempt_foreign')
        ledger = session.get(BudgetOperation, 'attempt_foreign')
        assert attempt.status == 'authorized' and attempt.completed_at is None
        assert ledger.status == 'reserved' and ledger.reserved_usd == '0.40'


@pytest.mark.parametrize('damage', ['foreign_run', 'foreign_owner', 'wrong_kind', 'wrong_bound', 'missing'])
def test_terminal_attempt_with_ambiguous_ledger_is_not_reconciled(factory, damage):
    own, other = seed(factory, suffix='own'), seed(factory, suffix='other')
    with factory.begin() as session:
        run = session.get(Run, own)
        run.status, run.stage = 'cancelled', 'finished'
        session.get(Run, other).status = 'completed'
        session.add(Step(id='step_owned', run_id=own, stage='proposal', role_slot_id='proposal_1', provider='openai',
                         status='skipped', lease_expires_at=None))
        session.flush()
        session.add(Attempt(id='attempt_owned', run_id=own, step_id='step_owned', provider='openai', status='authorized',
                            authorized_at=utcnow()-timedelta(seconds=151), reserved_usd='0.40', configuration={}))
        if damage != 'missing':
            session.add(BudgetOperation(id='attempt_owned', run_id=other if damage == 'foreign_run' else own,
                owner_id='foreign_owner' if damage == 'foreign_owner' else 'owner_test',
                kind='public_search' if damage == 'wrong_kind' else 'model', status='reserved',
                reserved_usd='0.10' if damage == 'wrong_bound' else '0.40', metadata_json={'model_call': True}))
    reconcile_once(session_factory=factory)
    with factory() as session:
        assert session.get(Attempt, 'attempt_owned').status == 'authorized'
        ledger = session.get(BudgetOperation, 'attempt_owned')
        if damage == 'missing':
            assert ledger is None
        else:
            assert ledger.status == 'reserved'


def test_authorized_step_without_attempt_is_not_requeued(factory):
    from sqlalchemy import select
    from round_robin.database.models import Outbox
    own = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, own)
        run.status, run.stage = 'running', 'proposals'
        session.add(Step(id='step_missing_attempt', run_id=own, stage='proposal', role_slot_id='proposal_1', provider='openai',
                         status='authorized', infrastructure_claims=1, lease_owner='lost', lease_expires_at=utcnow()-timedelta(seconds=1)))
    reconcile_once(session_factory=factory)
    with factory() as session:
        step = session.get(Step, 'step_missing_attempt')
        assert step.status == 'authorized' and step.lease_owner == 'lost'
        assert not list(session.scalars(select(Outbox).where(Outbox.step_id == step.id, Outbox.kind == 'execute')))


def test_one_foreign_attempt_rolls_back_whole_step_batch(factory):
    own, other = seed(factory, suffix='own'), seed(factory, suffix='other')
    with factory.begin() as session:
        session.add(Step(id='step_batch', run_id=own, stage='proposal', role_slot_id='proposal_1', provider='openai',
                         status='authorized', lease_owner='lost', lease_expires_at=utcnow()-timedelta(seconds=1)))
        session.flush()
        for identity, run_id in [('attempt_valid', own), ('attempt_foreign', other)]:
            session.add(Attempt(id=identity, run_id=run_id, step_id='step_batch', provider='openai', status='authorized',
                                authorized_at=utcnow()-timedelta(seconds=151), reserved_usd='0.40', configuration={}))
            session.add(BudgetOperation(id=identity, run_id=run_id, owner_id='owner_test', kind='model', status='reserved',
                                        reserved_usd='0.40', metadata_json={'model_call': True}))
    with pytest.raises(BudgetDenied, match='attempt_identity_unavailable'):
        with factory.begin() as session:
            _expire_step(session, session.get(Run, own), session.get(Step, 'step_batch'))
    with factory() as session:
        assert session.get(Step, 'step_batch').status == 'authorized'
        for identity in ('attempt_valid', 'attempt_foreign'):
            assert session.get(Attempt, identity).status == 'authorized'
            assert session.get(BudgetOperation, identity).status == 'reserved'


def test_invalid_kind_does_not_block_other_terminal_reconciliation(factory):
    damaged, healthy = seed(factory, suffix='damaged'), seed(factory, suffix='healthy')
    with factory.begin() as session:
        for identity, run_id, kind in [('attempt_bad', damaged, 'public_search'), ('attempt_good', healthy, 'model')]:
            run = session.get(Run, run_id)
            run.status, run.stage = 'cancelled', 'finished'
            step_id = f'step_{identity}'
            session.add(Step(id=step_id, run_id=run_id, stage='proposal', role_slot_id=step_id,
                             provider='openai', status='skipped', lease_expires_at=None))
            session.flush()
            session.add(Attempt(id=identity, run_id=run_id, step_id=step_id, provider='openai', status='authorized',
                                authorized_at=utcnow()-timedelta(seconds=151), reserved_usd='0.40', configuration={}))
            session.add(BudgetOperation(id=identity, run_id=run_id, owner_id='owner_test', kind=kind, status='reserved',
                                        reserved_usd='0.40', metadata_json={'model_call': True}))
    reconcile_once(session_factory=factory)
    with factory() as session:
        assert session.get(Attempt, 'attempt_bad').status == 'authorized'
        assert session.get(BudgetOperation, 'attempt_bad').status == 'reserved'
        assert session.get(Attempt, 'attempt_good').status == 'unknown'
        assert session.get(BudgetOperation, 'attempt_good').status == 'unknown'
