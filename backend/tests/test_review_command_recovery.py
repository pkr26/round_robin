from uuid import uuid4

from sqlalchemy import func, select

from backend.tests.test_api import application, client, command
from backend.tests.test_hosted import hosted, session_client
from round_robin.database.models import Command, Meeting
from round_robin.fixtures import sample_brief


def test_abandon_unseen_command_durably_fences_a_late_original(client, application):
    identity = str(uuid4())
    endpoint = f'/api/commands/{identity}/abandon'
    assert client.get(f'/api/commands/{identity}').status_code == 404
    assert client.post(endpoint).json()['status'] == 'abandoned'
    assert client.post(endpoint).json()['status'] == 'abandoned'
    assert client.get(f'/api/commands/{identity}').json()['outcome'] == 'abandoned'
    late = client.post('/api/meetings', json={'command_id': identity, 'brief': sample_brief().model_dump(mode='json')})
    assert late.status_code == 410 and late.json()['error']['code'] == 'command_abandoned'
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Meeting)) == 0
        saved = session.get(Command, ('owner_local', identity))
        assert saved.result == {} and saved.meeting_id is None and saved.resource_id is None


def test_abandon_returns_the_committed_or_deleted_original_without_cancelling_it(client):
    payload = command(brief=sample_brief().model_dump(mode='json'))
    created = client.post('/api/meetings', json=payload).json()
    receipt = client.get(f'/api/commands/{payload["command_id"]}').json()
    response = client.post(f'/api/commands/{payload["command_id"]}/abandon').json()
    assert response['status'] == 'committed' and response['result'] == receipt['result']
    assert response['result']['id'] == created['id']
    assert client.post('/api/meetings', json=payload).json() == created
    assert client.request('DELETE', f'/api/meetings/{created["id"]}', json=command()).status_code == 200
    gone = client.post(f'/api/commands/{payload["command_id"]}/abandon').json()
    assert gone['status'] == 'gone' and gone['result'] == {}


def test_abandon_requires_csrf_and_is_scoped_to_the_authenticated_owner(hosted):
    alice, _, csrf_a, _ = session_client(hosted)
    bob, _, csrf_b, _ = session_client(hosted, 'bob')
    identity = str(uuid4())
    assert alice.post(f'/api/commands/{identity}/abandon').status_code == 403
    assert alice.post(f'/api/commands/{identity}/abandon', headers={'x-csrf-token': csrf_a}).json()['status'] == 'abandoned'
    created = bob.post('/api/meetings', json={'command_id': identity, 'brief': sample_brief().model_dump(mode='json')}, headers={'x-csrf-token': csrf_b})
    assert created.status_code == 201
    assert alice.get(f'/api/commands/{identity}').json()['outcome'] == 'abandoned'
    assert bob.get(f'/api/commands/{identity}').json()['outcome'] == 'committed'
