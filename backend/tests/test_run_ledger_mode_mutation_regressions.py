"""Saved accounting state controls what the run API may claim."""
import pytest
from round_robin.database.models import BudgetOperation, Run

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


@pytest.mark.parametrize('mode,expected_requests', [('live', 23), ('simulated', None), ('missing', None)])
def test_saved_paid_operation_respects_run_mode_without_calling_provider(artifacts, mode, expected_requests):
    app, client, _, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        configuration = dict(run.configuration)
        if mode == 'missing':
            configuration.pop('mode', None)
        else:
            configuration['mode'] = mode
        run.configuration = configuration
        session.add(BudgetOperation(id='op_paid_fixture', run_id=run_id, owner_id=run.owner_id,
                                    kind='model', status='settled', reserved_usd='0.25',
                                    settled_usd='0.25', metadata_json={'model_call': True}))
    response = client.get(f'/api/runs/{run_id}')
    assert response.status_code == 200
    assert response.json()['execution_mode'] == ('simulated' if mode == 'missing' else mode)
    limits = response.json()['remaining_limits']
    assert limits['requests'] == expected_requests
    assert limits['requests_unavailable_reason'] == ('ledger_integrity_failed' if expected_requests is None else None)
