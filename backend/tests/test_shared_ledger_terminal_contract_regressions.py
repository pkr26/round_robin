from uuid import uuid4

import pytest

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import create, start
from round_robin.database.models import BudgetOperation, Run
from round_robin.orchestration.controller import finalize_run


@pytest.mark.parametrize('case', ['released_nan', 'too_many_calls', 'tiny_fraction', 'too_large'])
def test_bad_runtime_usage_fences_terminal_without_releasing_hold(application, client, case):
    run = start(client, create(client)).json()
    with application.state.session_factory.begin() as session:
        session.add(BudgetOperation(id=f"finish_{run['id']}", run_id=run['id'], owner_id='owner_local',
            kind='finish_hold', status='held', reserved_usd='0.20', metadata_json={'roles_remaining': ['audit']}))
        for index in range(25 if case == 'too_many_calls' else 1):
            session.add(BudgetOperation(id=f'bad_{index}', run_id=run['id'], owner_id='owner_local', kind='model',
                status='released' if case == 'released_nan' else 'settled', reserved_usd='0',
                settled_usd={'released_nan':'NaN', 'too_many_calls':'0', 'tiny_fraction':'0.0000000000000000000000001', 'too_large':'1000000000000000000000000000001'}[case],
                metadata_json={'model_call': True}))
    result = client.post(f"/api/runs/{run['id']}/cancel", json={'command_id': str(uuid4()), 'expected_run_version': run['version']})
    assert result.status_code == 200
    with application.state.session_factory.begin() as session:
        finalize_run(session, session.get(Run, run['id']))
    with application.state.session_factory() as session:
        stored = session.get(Run, run['id'])
        assert stored.status == 'cancelled' and stored.configuration['ledger_integrity_failed']
        assert session.get(BudgetOperation, f"finish_{run['id']}").status == 'held'
        assert stored.report_version == 0
