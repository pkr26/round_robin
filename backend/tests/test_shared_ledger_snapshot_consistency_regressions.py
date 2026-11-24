"""The run and report in one snapshot share an accounting-integrity decision."""
import pytest
from round_robin.api import events

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import create, start
from backend.tests.test_orchestration import drain


@pytest.mark.parametrize('initial_invalid', [False, True])
def test_snapshot_uses_one_ledger_state_when_subsequent_reads_change(client, application, monkeypatch, initial_invalid):
    meeting = create(client)
    started = start(client, meeting)
    assert started.status_code == 202, started.text
    run_id = started.json()['id']
    drain(application.state.session_factory)
    baseline = client.get(f'/api/runs/{run_id}/snapshot')
    assert baseline.status_code == 200 and baseline.json()['report'] is not None
    real = events.ledger_state
    calls = []

    def changing(session, run):
        calls.append(True)
        if len(calls) == 1:
            return (True, None) if initial_invalid else real(session, run)
        return real(session, run) if initial_invalid else (True, None)

    monkeypatch.setattr(events, 'ledger_state', changing)
    response = client.get(f'/api/runs/{run_id}/snapshot')
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(calls) == 1
    if initial_invalid:
        assert body['report'] is None and body['run']['readiness'] is None
        assert body['run']['remaining_limits']['requests'] is None
        assert body['run']['remaining_limits']['requests_unavailable_reason'] == 'ledger_integrity_failed'
    else:
        assert body['report'] is not None
        assert body['run']['remaining_limits']['requests'] is not None
        assert body['run']['remaining_limits']['requests_unavailable_reason'] is None
