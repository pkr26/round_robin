"""Development mode must never dispatch hosted sign-in, even if its flag is set."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from round_robin.app import create_app
from round_robin.configuration import Settings


@pytest.mark.parametrize('path', ['/auth/login', '/auth/callback'])
@pytest.mark.parametrize('hosted_enabled', [False, True])
def test_development_never_calls_oidc(tmp_path, path, hosted_enabled):
    calls=[]
    async def redirect(*args, **kwargs):
        calls.append('redirect')
        return RedirectResponse('/unexpected')
    async def token(*args, **kwargs):
        calls.append('token')
        return {}
    settings=replace(Settings(),development=True,hosted_enabled=hosted_enabled,artifact_directory=tmp_path/'artifacts')
    settings.validate()
    app=create_app(f'sqlite:///{tmp_path / "hosted_gate.sqlite"}',settings=settings,initialize=True)
    app.state.oidc=SimpleNamespace(authorize_redirect=redirect,authorize_access_token=token)
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.get(path,follow_redirects=False)
    assert response.status_code==404
    assert response.json()['error']['code']=='hosted_access_disabled'
    assert 'disabled' in (response.json()['error']['message'] or '').lower()
    assert calls==[]
