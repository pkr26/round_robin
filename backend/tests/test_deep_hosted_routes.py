"""Mock actual OIDC client boundaries rather than accepting only happy tokens."""
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select

from backend.tests.test_hosted import hosted
from round_robin.database.models import Owner


@pytest.mark.parametrize("path", ["/auth/login", "/auth/callback"])
@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_oidc_transport_failures_are_sanitized_retryable_service_errors(hosted, path, failure, caplog):
    async def unavailable(*args, **kwargs):
        raise failure("PRIVATE_IDENTITY_PROVIDER_DIAGNOSTIC")
    hosted.state.oidc = SimpleNamespace(authorize_redirect=unavailable, authorize_access_token=unavailable)
    with TestClient(hosted, base_url="https://app.example", raise_server_exceptions=False) as client:
        response = client.get(path)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "oidc_unavailable"
    assert "PRIVATE_IDENTITY_PROVIDER_DIAGNOSTIC" not in response.text + caplog.text
    with hosted.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Owner)) == 0


@pytest.mark.parametrize("payload", [None, [], "unexpected", {"id_token": "opaque", "userinfo": [1]}, {"id_token": True, "userinfo": {"iss": "https://identity.example", "sub": "alice"}}])
def test_malformed_oidc_token_shapes_cannot_create_sessions(hosted, payload):
    async def token(*args, **kwargs):
        return payload
    hosted.state.oidc = SimpleNamespace(authorize_access_token=token)
    with TestClient(hosted, base_url="https://app.example", raise_server_exceptions=False) as client:
        response = client.get("/auth/callback")
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "oidc_identity_invalid"
    assert not any(value.startswith("__Host-round_robin=") for value in response.headers.get_list("set-cookie"))
    with hosted.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Owner)) == 0
