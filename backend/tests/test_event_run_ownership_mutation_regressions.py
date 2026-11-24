"""Independent API ownership probes; corrupt-owner fixture is synthetic."""
from uuid import uuid4

from round_robin.api.auth import require_owner
from round_robin.database.models import Run

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import create, start


def test_unknown_run_is_consistently_hidden(client):
    for suffix in ('', '/snapshot', '/events'):
        response = client.get(f'/api/runs/run_not_real{suffix}')
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'not_found'
        assert isinstance(response.json()['error']['message'], str) and response.json()['error']['message']


def test_foreign_owner_cannot_read_or_stop_run(client, application):
    meeting = create(client)
    run = start(client, meeting).json()
    application.dependency_overrides[require_owner] = lambda: 'owner_other'
    for suffix in ('', '/snapshot', '/events'):
        response = client.get(f"/api/runs/{run['id']}{suffix}")
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'not_found'
        assert isinstance(response.json()['error']['message'], str) and response.json()['error']['message']
    response = client.post(f"/api/runs/{run['id']}/cancel", json={'command_id': str(uuid4()), 'expected_run_version': run['version']})
    assert response.status_code == 404
    assert response.json()['error']['code'] == 'not_found'
    assert isinstance(response.json()['error']['message'], str) and response.json()['error']['message']


def test_run_with_mismatched_owner_is_not_laundered_through_meeting(client, application):
    meeting = create(client)
    run = start(client, meeting).json()
    with application.state.session_factory.begin() as session:
        session.get(Run, run['id']).owner_id = 'owner_other'
    for suffix in ('', '/snapshot', '/events'):
        response = client.get(f"/api/runs/{run['id']}{suffix}")
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'not_found'
