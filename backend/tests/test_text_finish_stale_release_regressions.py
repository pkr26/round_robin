import pytest

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import BudgetOperation, Run
from round_robin.orchestration.budget import release_unused_finish, reserve_finish


@pytest.mark.parametrize('damage', ['owner', 'run', 'kind', 'status', 'metadata'])
def test_release_refreshes_cached_hold_and_preserves_later_ambiguity(factory, damage):
    run_id = seed(factory)
    with factory.begin() as session:
        reserve_finish(session, session.get(Run, run_id))
    with factory() as reader:
        run = reader.get(Run, run_id)
        cached = reader.get(BudgetOperation, f'finish_{run_id}')
        with factory.begin() as writer:
            fresh = writer.get(BudgetOperation, cached.id)
            fresh.reserved_usd = '0.40'
            if damage == 'owner': fresh.owner_id = 'foreign'
            elif damage == 'run': fresh.run_id = 'foreign'
            elif damage == 'kind': fresh.kind = 'model'
            elif damage == 'status': fresh.status = 'unknown'
            elif damage == 'metadata': fresh.metadata_json = {**fresh.metadata_json, 'model_call': True}
        assert release_unused_finish(reader, run) is False
        reader.commit()
    with factory() as session:
        hold = session.get(BudgetOperation, f'finish_{run_id}')
        assert hold.reserved_usd == '0.40'
        assert hold.status == ('unknown' if damage == 'status' else 'held')
