"""Hosted authentication errors retain actionable response fields."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.tests.test_hosted import hosted, session_client
from round_robin.security.hosted import forbidden


def test_forbidden_default_and_non_authentication_scope():
    with pytest.raises(HTTPException) as unauthenticated:
        forbidden('session_required', 'Sign in to continue')
    assert unauthenticated.value.status_code == 401
    assert unauthenticated.value.detail == {'code': 'session_required', 'message': 'Sign in to continue', 'retry_scope': 'login'}
    for status in (400, 403):
        with pytest.raises(HTTPException) as denied:
            forbidden('access_denied', 'Request cannot proceed', status)
        assert denied.value.status_code == status
        assert denied.value.detail == {'code': 'access_denied', 'message': 'Request cannot proceed', 'retry_scope': 'none'}


def test_authentication_and_origin_errors_reach_http_clients(hosted):
    anonymous = TestClient(hosted, base_url='https://app.example')
    response = anonymous.get('/api/meetings')
    assert response.status_code == 401
    assert response.json()['error'] == {'code': 'session_required', 'message': 'Sign in to continue', 'retry_scope': 'login'}
    client, _, csrf, _ = session_client(hosted)
    response = client.post('/api/meetings', json={}, headers={'x-csrf-token': csrf, 'origin': 'https://outside.example'})
    assert response.status_code == 403
    assert response.json()['error'] == {'code': 'origin_invalid', 'message': 'Command origin is not allowed', 'retry_scope': 'none'}
