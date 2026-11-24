from datetime import timedelta
from uuid import uuid4

import pytest
from round_robin.api import visual as routes
from round_robin.database.models import BudgetOperation, utcnow
from round_robin.database.visual_models import DesignOperation, DesignSession
from sqlalchemy import select

from backend.tests.test_deep_visual_routes import (
    funded_visual_routes as funded_visual_routes,  # noqa: PLC0414
)
from backend.tests.test_deep_visual_routes import improve_body, prepare
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_visual import visual_app as visual_app  # noqa: PLC0414


@pytest.mark.parametrize('change', ['missing', 'id', 'revision', 'action', 'expiry', 'consumed', 'inputs', 'brief'])
def test_changed_visual_preflight_cannot_queue_work(funded_visual_routes, monkeypatch, change):
    app, client, design, requests, _ = funded_visual_routes
    quote = prepare(client, design, 'generate')
    body = improve_body(quote)
    now = utcnow()
    monkeypatch.setattr(routes, 'utcnow', lambda: now)
    with app.state.session_factory.begin() as session:
        saved = session.get(DesignSession, design['id'])
        preflight = dict(saved.preflight)
        if change == 'missing':
            saved.preflight = None
        elif change == 'id':
            body['preflight_id'] = 'preflight_foreign'
        elif change == 'revision':
            body['expected_revision'] += 1
        else:
            if change == 'action':
                preflight['action'] = 'review'
            elif change == 'expiry':
                preflight['expires_at'] = now.isoformat()
            elif change == 'consumed':
                preflight['consumed_by_command'] = str(uuid4())
            elif change == 'inputs':
                preflight['input_hashes'] = ['a' * 64]
            elif change == 'brief':
                preflight['brief_hash'] = 'a' * 64
            saved.preflight = preflight
    response = client.post(f"/api/design-sessions/{design['id']}/improvements", json=body)
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'preflight_stale'
    assert requests == []
    with app.state.session_factory() as session:
        assert session.scalar(select(DesignOperation).where(DesignOperation.session_id == design['id'])) is None
        assert session.scalar(select(BudgetOperation).where(BudgetOperation.run_id == design['id'])) is None


def test_visual_funding_blocker_remains_actionable_and_spends_nothing(funded_visual_routes):
    app, client, design, requests, _ = funded_visual_routes
    quote = prepare(client, design, 'generate')
    with app.state.session_factory.begin() as session:
        saved = session.get(DesignSession, design['id'])
        saved.preflight = {**saved.preflight, 'blockers': ['Restore the configured funding limit.']}
    response = client.post(f"/api/design-sessions/{design['id']}/improvements", json=improve_body(quote))
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'visual_funding_locked'
    assert response.json()['error']['message'] == 'Restore the configured funding limit.'
    assert requests == []
    with app.state.session_factory() as session:
        assert session.scalar(select(DesignOperation).where(DesignOperation.session_id == design['id'])) is None
        assert session.scalar(select(BudgetOperation).where(BudgetOperation.run_id == design['id'])) is None


def test_consumed_preflight_records_its_command_and_exact_admission_window(funded_visual_routes, monkeypatch):
    app, client, design, requests, _ = funded_visual_routes
    quote = prepare(client, design, 'generate')
    body = improve_body(quote)
    now = utcnow()
    monkeypatch.setattr(routes, 'utcnow', lambda: now)
    path = f"/api/design-sessions/{design['id']}/improvements"
    accepted = client.post(path, json=body)
    assert accepted.status_code == 202, accepted.text
    with app.state.session_factory() as session:
        saved = session.get(DesignSession, design['id'])
        assert saved.preflight['consumed_by_command'] == body['command_id']
        assert saved.preflight['queue_accepted_at'] == now.isoformat()
        assert saved.preflight['initial_admission_expires_at'] == (now + timedelta(minutes=30)).isoformat()
    assert client.post(path, json=body).json() == accepted.json()
    assert client.post(path, json={**body, 'command_id': str(uuid4())}).status_code == 409
    assert requests == []


def test_disabled_application_paid_setting_overrides_enabled_environment(funded_visual_routes):
    from dataclasses import replace

    app, client, design, requests, _ = funded_visual_routes
    quote = prepare(client, design, 'generate')
    app.state.settings = replace(app.state.settings, paid_access_enabled=False)
    response = client.post(f"/api/design-sessions/{design['id']}/improvements", json=improve_body(quote))
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'visual_configuration_changed'
    assert requests == []
    with app.state.session_factory() as session:
        assert session.scalar(select(DesignOperation).where(DesignOperation.session_id == design['id'])) is None
        assert session.scalar(select(BudgetOperation).where(BudgetOperation.run_id == design['id'])) is None


def test_missing_operator_config_and_missing_frozen_hash_cannot_authorize(funded_visual_routes, monkeypatch):
    app, client, design, requests, _ = funded_visual_routes
    quote = prepare(client, design, 'generate')
    with app.state.session_factory.begin() as session:
        saved = session.get(DesignSession, design['id'])
        config = dict(saved.configuration)
        config.pop('operator_configuration_hash')
        saved.configuration = config
    monkeypatch.delenv('ROUND_ROBIN_IMAGE_CONFIG_FILE')
    response = client.post(f"/api/design-sessions/{design['id']}/improvements", json=improve_body(quote))
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'visual_configuration_changed'
    assert requests == []
    with app.state.session_factory() as session:
        saved = session.get(DesignSession, design['id'])
        assert saved.preflight['consumed_by_command'] is None
        assert session.scalar(select(DesignOperation).where(DesignOperation.session_id == design['id'])) is None
        assert session.scalar(select(BudgetOperation).where(BudgetOperation.run_id == design['id'])) is None
