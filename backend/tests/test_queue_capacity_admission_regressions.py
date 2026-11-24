import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Owner, Run
from round_robin.orchestration.admission import ensure_queue_capacity
from round_robin.orchestration.budget import BudgetDenied


def add_runs(factory, count):
    return [seed(factory, suffix=f'queue_{index}') for index in range(count)]


def test_owner_boundary_self_exclusion_and_nonqueued_state(factory):
    ids = add_runs(factory, 4)
    with factory.begin() as session:
        session.get(Run, ids[3]).status = 'running'
        with pytest.raises(BudgetDenied, match='^owner_queue_limit$'):
            ensure_queue_capacity(session, 'owner_test')
        ensure_queue_capacity(session, 'owner_test', run_id=ids[0])
        with pytest.raises(BudgetDenied, match='^owner_queue_limit$'):
            ensure_queue_capacity(session, 'owner_test', run_id=ids[3])
        session.get(Run, ids[2]).status = 'failed'
        ensure_queue_capacity(session, 'owner_test')
        with pytest.raises(BudgetDenied, match='^owner_queue_limit$'):
            ensure_queue_capacity(session, 'owner_test', owner_limit=2)


def test_global_boundary_counts_all_owners_and_excludes_only_requested_run(factory):
    ids = add_runs(factory, 20)
    with factory.begin() as session:
        for index, identity in enumerate(ids):
            owner = f'owner_{index}'
            session.add(Owner(id=owner))
            session.get(Run, identity).owner_id = owner
        session.flush()
        with pytest.raises(BudgetDenied, match='^deployment_queue_limit$'):
            ensure_queue_capacity(session, 'owner_absent')
        ensure_queue_capacity(session, 'owner_absent', run_id=ids[0])
        session.get(Run, ids[-1]).status = 'cancelled'
        ensure_queue_capacity(session, 'owner_absent')
        assert len(list(session.scalars(select(Run).where(Run.status == 'queued')))) == 19
