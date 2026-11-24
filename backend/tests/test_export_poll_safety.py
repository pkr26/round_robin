"""Polling must not offer downloads for unavailable artifacts."""
from datetime import UTC, timedelta
from uuid import uuid4

import pytest
from round_robin.database.models import ExportRecord, utcnow

pytest_plugins = ('backend.tests.test_artifacts_api',)


@pytest.mark.parametrize('status', ['pending','failed','purged'])
def test_poll_only_exposes_download_for_ready_artifacts(artifacts, status):
    app, client, _, run_id, report = artifacts
    created = client.post(f'/api/runs/{run_id}/exports', json={'command_id':str(uuid4()), 'report_id':report['id'], 'format':'json'})
    assert created.status_code == 200
    ready = created.json()
    assert ready['status'] == 'ready' and ready['download_url']
    with app.state.session_factory.begin() as session:
        row = session.get(ExportRecord,ready['id'])
        row.status = status
        row.error_code = 'export_render_failed' if status == 'failed' else None
    polled = client.get(f'/api/exports/{ready["id"]}')
    assert polled.status_code == 200, polled.text
    assert polled.json()['status'] == status and polled.json()['download_url'] is None
    assert client.get(ready['download_url']).status_code == 409


def test_poll_does_not_offer_expired_ready_artifact(artifacts):
    app, client, _, run_id, report = artifacts
    created = client.post(f'/api/runs/{run_id}/exports', json={'command_id':str(uuid4()), 'report_id':report['id'], 'format':'json'})
    ready=created.json()
    with app.state.session_factory.begin() as session:
        session.get(ExportRecord,ready['id']).created_at = utcnow() - timedelta(days=8)
    assert client.get(ready['download_url']).status_code == 409
    polled=client.get(f'/api/exports/{ready["id"]}')
    assert polled.status_code == 200
    assert polled.json()['download_url'] is None


@pytest.mark.parametrize('offset_microseconds,valid', [(1, True), (0, True), (-1, False)])
def test_poll_and_download_agree_at_exact_seven_day_boundary(artifacts, monkeypatch, offset_microseconds, valid):
    from datetime import datetime

    from round_robin.api import artifacts as routes

    app, client, _, run_id, report = artifacts
    created = client.post(f'/api/runs/{run_id}/exports', json={'command_id':str(uuid4()), 'report_id':report['id'], 'format':'json'}).json()
    fixed = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
    with app.state.session_factory.begin() as session:
        session.get(ExportRecord, created['id']).created_at = fixed - timedelta(days=7) + timedelta(microseconds=offset_microseconds)
    monkeypatch.setattr(routes, 'utcnow', lambda: fixed)
    polled = client.get(f'/api/exports/{created["id"]}')
    assert polled.status_code == 200
    assert polled.json()['status'] == 'ready'
    assert polled.json()['error'] is None
    assert (polled.json()['download_url'] is not None) is valid
    assert client.get(created['download_url']).status_code == (200 if valid else 409)


@pytest.mark.parametrize('missing_field', ['path', 'sha256'])
def test_poll_omits_link_when_ready_metadata_is_incomplete(artifacts, missing_field):
    app, client, _, run_id, report = artifacts
    created = client.post(f'/api/runs/{run_id}/exports', json={'command_id':str(uuid4()), 'report_id':report['id'], 'format':'json'}).json()
    with app.state.session_factory.begin() as session:
        setattr(session.get(ExportRecord, created['id']), missing_field, None)
    polled = client.get(f'/api/exports/{created["id"]}')
    assert polled.status_code == 200
    assert polled.json()['status'] == 'ready' and polled.json()['download_url'] is None
    assert client.get(created['download_url']).status_code == 409


def test_idempotent_replay_retains_original_receipt_after_expiry(artifacts):
    app, client, _, run_id, report = artifacts
    command = {'command_id':str(uuid4()), 'report_id':report['id'], 'format':'json'}
    endpoint=f'/api/runs/{run_id}/exports'
    created=client.post(endpoint,json=command).json()
    assert created['status'] == 'ready' and created['download_url']
    assert client.get(created['download_url']).status_code == 200
    with app.state.session_factory.begin() as session:
        session.get(ExportRecord,created['id']).created_at = utcnow() - timedelta(days=8)
    replay = client.post(endpoint,json=command)
    assert replay.status_code == 200 and replay.json() == created
    assert client.get(created['download_url']).status_code == 409
    fresh_poll = client.get(f'/api/exports/{created["id"]}').json()
    assert fresh_poll['status'] == 'ready' and fresh_poll['download_url'] is None
