"""Real PostgreSQL transaction evidence; never run against a non-test database."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from uuid import uuid4

from fastapi.testclient import TestClient
from fastapi import Request
import pytest
from sqlalchemy import create_engine, func, select, text

from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, Base, Event, PolicyBucket, Run
from round_robin.api.auth import require_owner
from round_robin.database.services import append_event, locked_run
from round_robin.database.safety import require_disposable_postgres
from round_robin.fixtures import sample_brief


@pytest.fixture
def postgres_app(tmp_path):
    value = os.environ.get("ROUND_ROBIN_TEST_DATABASE_URL")
    if not value:
        pytest.skip("Real PostgreSQL checks require ROUND_ROBIN_TEST_DATABASE_URL and explicit loopback permission")
    try:
        url = require_disposable_postgres(value)
    except ValueError:
        pytest.fail("Refusing a non-loopback or non-test PostgreSQL database")
    schema = "rr_test_" + uuid4().hex
    administration = create_engine(url)
    with administration.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    test_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    app = create_app(test_url.render_as_string(hide_password=False), settings=replace(Settings(), artifact_directory=tmp_path / "artifacts"))
    Base.metadata.create_all(app.state.engine)
    try:
        yield app
    finally:
        app.state.engine.dispose()
        with administration.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        administration.dispose()


def command(**values):
    return {"command_id": str(uuid4()), **values}


def create(client):
    response = client.post("/api/meetings", json=command(brief=sample_brief().model_dump(mode="json")))
    assert response.status_code == 201, response.text
    return response.json()


def test_postgresql_competing_starts_consume_preflight_once(postgres_app):
    with TestClient(postgres_app) as client:
        meeting = create(client)
        quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1)).json()
    def send(_):
        with TestClient(postgres_app) as client:
            return client.post(f"/api/meetings/{meeting['id']}/runs", json=command(preflight_id=quote["id"], expected_draft_revision=1))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(send, range(2)))
    assert sorted(result.status_code for result in results) == [202, 409]
    with postgres_app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 1


def test_postgresql_duplicate_command_reconciles_same_result(postgres_app):
    payload = command(brief=sample_brief().model_dump(mode="json"))
    def send(_):
        with TestClient(postgres_app) as client:
            return client.post("/api/meetings", json=payload)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, range(2)))
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()["id"] == responses[1].json()["id"]


def test_postgresql_revision_race_preserves_one_save(postgres_app):
    with TestClient(postgres_app) as client:
        meeting = create(client)
    def send(index):
        with TestClient(postgres_app) as client:
            brief = {**meeting["draft"], "title": f"Competing edit {index}"}
            return client.patch(f"/api/meetings/{meeting['id']}/draft", json=command(expected_revision=1, brief=brief))
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_postgresql_ordered_events_from_competing_transactions(postgres_app):
    with TestClient(postgres_app) as client:
        meeting = create(client)
        quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1)).json()
        run = client.post(f"/api/meetings/{meeting['id']}/runs", json=command(preflight_id=quote["id"], expected_draft_revision=1)).json()
    def commit(index):
        with postgres_app.state.session_factory.begin() as session:
            row = locked_run(session, run["id"])
            append_event(session, row, "test.committed", {"index": index})
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(commit, range(12)))
    with postgres_app.state.session_factory() as session:
        sequences = list(session.scalars(select(Event.sequence).where(Event.run_id == run["id"]).order_by(Event.sequence)))
    assert sequences == list(range(1, 14))


def test_postgresql_lock_refreshes_identity_loaded_before_another_commit(postgres_app):
    with TestClient(postgres_app) as client:
        meeting = create(client)
        quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1)).json()
        run = client.post(f"/api/meetings/{meeting['id']}/runs", json=command(preflight_id=quote["id"], expected_draft_revision=1)).json()
    with postgres_app.state.session_factory() as stale:
        original = stale.get(Run, run["id"])
        assert original.event_sequence == 1
        with postgres_app.state.session_factory.begin() as other:
            append_event(other, locked_run(other, run["id"]), "other.committed", {})
        refreshed = locked_run(stale, run["id"])
        assert refreshed is original
        assert refreshed.event_sequence == 2
        append_event(stale, refreshed, "stale.refreshed", {})
        stale.commit()
    with postgres_app.state.session_factory() as session:
        assert list(session.scalars(select(Event.sequence).where(Event.run_id == run["id"]).order_by(Event.sequence))) == [1, 2, 3]


def test_postgresql_two_owners_can_initialize_their_first_policies_concurrently(postgres_app):
    def test_owner(request: Request):
        return request.headers["x-test-owner"]
    postgres_app.dependency_overrides[require_owner] = test_owner
    prepared = []
    for owner in ("owner_alice", "owner_bob"):
        with TestClient(postgres_app) as client:
            client.headers["x-test-owner"] = owner
            meeting = create(client)
            quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1)).json()
            prepared.append((owner, meeting, quote))
    with postgres_app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PolicyBucket)) == 0
    def send(values):
        owner, meeting, quote = values
        with TestClient(postgres_app) as client:
            return client.post(f"/api/meetings/{meeting['id']}/runs", headers={"x-test-owner": owner}, json=command(preflight_id=quote["id"], expected_draft_revision=1))
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, prepared))
    assert [response.status_code for response in responses] == [202, 202]
    assert len({response.json()["id"] for response in responses}) == 2
    with postgres_app.state.session_factory() as session:
        assert {row.key for row in session.scalars(select(PolicyBucket))} == {"deployment", "owner:owner_alice", "owner:owner_bob"}
        assert session.scalar(select(func.count()).select_from(Run)) == 2
        assert session.scalar(select(func.count()).select_from(Attempt)) == 0
