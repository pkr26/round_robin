"""Current report visibility must follow the saved run's accounting state."""
from copy import deepcopy

from round_robin.api import events
from round_robin.database.models import BudgetOperation, ReportVersion, Run

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


def test_quarantined_current_report_is_hidden_but_history_remains_readable(artifacts):
    app, client, _, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, 'ledger_integrity_failed': True}
    snapshot = client.get(f'/api/runs/{run_id}/snapshot')
    assert snapshot.status_code == 200
    assert snapshot.json()['report'] is None
    assert snapshot.json()['run']['remaining_limits']['requests'] is None
    current = client.get(f'/api/runs/{run_id}/report')
    assert current.status_code == 503
    assert current.json()['error']['code'] == 'ledger_integrity_failed'
    history = client.get(f'/api/runs/{run_id}/reports/{report["version"]}')
    assert history.status_code == 200
    assert history.json()['id'] == report['id']


def test_valid_nonzero_request_count_does_not_hide_current_report(artifacts):
    app, client, _, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        session.add(BudgetOperation(id='op_test_count', run_id=run_id, owner_id=run.owner_id,
                                    kind='model', status='settled', reserved_usd='0',
                                    settled_usd='0', metadata_json={'model_call': True}))
    snapshot = client.get(f'/api/runs/{run_id}/snapshot')
    assert snapshot.status_code == 200
    assert snapshot.json()['run']['remaining_limits']['requests'] == 23
    assert snapshot.json()['report'] is not None
    assert snapshot.json()['report']['id'] == report['id']


def test_snapshot_uses_one_consistent_accounting_observation(artifacts, monkeypatch):
    _, client, _, run_id, report = artifacts
    observations = iter([(False, 0), (True, None), (True, None)])
    monkeypatch.setattr(events, 'ledger_state', lambda *_args: next(observations, (True, None)))
    snapshot = client.get(f'/api/runs/{run_id}/snapshot')
    assert snapshot.status_code == 200
    assert snapshot.json()['run']['remaining_limits']['requests'] == 24
    assert snapshot.json()['report'] is not None
    assert snapshot.json()['report']['id'] == report['id']


def test_snapshot_selects_newest_saved_report_for_this_run(artifacts):
    app, client, _, run_id, report = artifacts
    newer = deepcopy(report)
    newer['id'] = 'report_newest_fixture'
    newer['version'] = 2
    with app.state.session_factory.begin() as session:
        session.get(Run, run_id).report_version = 2
        session.add(ReportVersion(id=newer['id'], run_id=run_id, version=2,
                                  event_cutoff=newer['event_cutoff'], content=newer))
    snapshot = client.get(f'/api/runs/{run_id}/snapshot')
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()['report'] is not None
    assert snapshot.json()['report']['id'] == newer['id']
    assert snapshot.json()['report']['version'] == 2
