from concurrent.futures import ThreadPoolExecutor
import threading
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from backend.tests.integration.test_postgres_api import postgres_app
from round_robin.database.models import Command, Meeting
from round_robin.fixtures import sample_brief


@pytest.mark.parametrize('iteration', range(3))
def test_original_command_and_abandon_have_exactly_one_durable_winner(postgres_app, iteration):
    identity = str(uuid4())
    payload = {'command_id': identity, 'brief': sample_brief().model_dump(mode='json')}
    barrier = threading.Barrier(2)
    def original():
        with TestClient(postgres_app) as client:
            barrier.wait(timeout=5)
            return client.post('/api/meetings', json=payload)
    def abandon():
        with TestClient(postgres_app) as client:
            barrier.wait(timeout=5)
            return client.post(f'/api/commands/{identity}/abandon')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(original), pool.submit(abandon)
        sent, retired = first.result(timeout=10), second.result(timeout=10)
    assert retired.status_code == 200
    with postgres_app.state.session_factory() as session:
        count = session.scalar(select(func.count()).select_from(Meeting))
        receipt = session.get(Command, ('owner_local', identity))
        if retired.json()['status'] == 'abandoned':
            assert sent.status_code == 410 and count == 0 and receipt.command_type == 'abandoned'
        else:
            assert retired.json()['status'] == 'committed' and sent.status_code == 201 and count == 1
            assert retired.json()['result']['id'] == sent.json()['id'] == receipt.resource_id
    with TestClient(postgres_app) as client:
        repeated = client.post('/api/meetings', json=payload)
        assert repeated.status_code == sent.status_code
