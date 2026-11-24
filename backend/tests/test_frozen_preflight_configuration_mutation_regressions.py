"""Preflight commits the exact execution and funding contract that Start uses."""
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from round_robin.api import preflight as preflight_module
from round_robin.database.models import Attempt, PreflightRecord, Run, utcnow
from round_robin.database.services import lock_policies
from round_robin.orchestration.protocol import policy_descriptor
from round_robin.providers.http import request_contract_hash
from sqlalchemy import func, select

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import command, create, preflight, start
from backend.tests.test_provider_configuration import synthetic_configuration


def test_simulated_preflight_and_start_freeze_advertised_execution_contract(client, application):
    meeting = create(client)
    quote = preflight(client, meeting)
    assert quote['mode'] == 'simulated' and quote['paid_access_enabled'] is False
    assert quote['limits'] == {'max_requests': 24, 'max_calls_per_step': 2, 'active_seconds': 600,
                               'per_run_usd': '2', 'owner_daily_usd': '10', 'owner_monthly_usd': '50'}
    assert quote['finish_policy'] == {'reserved_usd': '0.40', 'reserved_requests': 2,
                                      'reserved_active_seconds': 190, 'version': 'runtime-finish-v1'}
    frozen = quote['configuration']
    assert frozen['mode'] == 'simulated' and frozen['funded'] is False
    assert all(frozen['capabilities'][provider]['structured_text'] is True for provider in ('openai', 'anthropic', 'google'))
    assert frozen['request_contract_hash'] == request_contract_hash()
    assert frozen['discussion_policy'] == policy_descriptor()
    result = start(client, meeting, quote)
    assert result.status_code == 202, result.text
    with application.state.session_factory() as session:
        stored = session.get(Run, result.json()['id']).configuration
        assert all(stored[key] == frozen[key] for key in ('mode', 'funded', 'limits', 'finish_policy', 'capabilities', 'request_contract_hash', 'discussion_policy'))
        assert session.scalar(select(func.count()).select_from(Attempt)) == 0


def test_live_preflight_freezes_mode_and_start_preserves_it_without_sending(client, application, tmp_path):
    path = tmp_path / 'operator.json'
    path.write_text(json.dumps(synthetic_configuration()))
    application.state.settings = replace(application.state.settings, provider_config_file=path, paid_access_enabled=True)
    meeting = create(client)
    with application.state.session_factory.begin() as session:
        for policy in lock_policies(session, 'owner_local'):
            policy.spending_locked = False
    response = client.post(f'/api/meetings/{meeting["id"]}/preflight', json=command(expected_draft_revision=1, mode='live'))
    assert response.status_code == 200, response.text
    quote = response.json()
    assert quote['blockers'] == [] and quote['paid_access_enabled'] is True
    assert quote['configuration']['mode'] == 'live'
    started = start(client, meeting, quote)
    assert started.status_code == 202, started.text
    with application.state.session_factory() as session:
        run = session.get(Run, started.json()['id'])
        assert run.configuration['mode'] == 'live'
        assert session.scalar(select(func.count()).select_from(Attempt)) == 0


@pytest.mark.parametrize('offset,expected', [(timedelta(0), 422), (timedelta(microseconds=1), 200)])
def test_live_configuration_expiry_is_exclusive_at_exact_timestamp(client, application, tmp_path, monkeypatch, offset, expected):
    now = utcnow()
    config = synthetic_configuration()
    config['expires_at'] = (now + offset).isoformat()
    path = tmp_path / 'operator.json'
    path.write_text(json.dumps(config))
    application.state.settings = replace(application.state.settings, provider_config_file=path, paid_access_enabled=True)
    monkeypatch.setattr(preflight_module, 'utcnow', lambda: now)
    meeting = create(client)
    response = client.post(f'/api/meetings/{meeting["id"]}/preflight', json=command(expected_draft_revision=1, mode='live'))
    assert response.status_code == expected, response.text
    if expected == 422:
        assert response.json()['error']['code'] == 'provider_configuration_invalid'
        with application.state.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(PreflightRecord)) == 0
            assert session.scalar(select(func.count()).select_from(Run)) == 0
            assert session.scalar(select(func.count()).select_from(Attempt)) == 0
