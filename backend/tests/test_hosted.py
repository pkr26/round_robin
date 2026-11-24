from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
import pytest

from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import utcnow
from round_robin.fixtures import sample_brief
from round_robin.security.hosted import SESSION_COOKIE, create_session, revoke_session


def hosted_settings(tmp_path):
    return replace(Settings(), development=False, hosted_enabled=True, public_origin="https://app.example", oidc_issuer="https://identity.example", oidc_client_id="synthetic-client", oidc_client_secret="synthetic-secret", command_hash_key="synthetic-persistent-hash-key-32-characters", invite_allowlist=(("https://identity.example", "alice"), ("https://identity.example", "bob")), hosted_quotas_configured=True, private_storage_configured=True, retention_configured=True, artifact_directory=tmp_path / "artifacts")


@pytest.fixture
def hosted(tmp_path):
    return create_app(f"sqlite:///{tmp_path/'hosted.db'}", settings=hosted_settings(tmp_path), initialize=True)


def session_client(app, subject="alice", **kwargs):
    token, csrf, owner = create_session(app.state.session_factory, app.state.settings, "https://identity.example", subject, **kwargs)
    client = TestClient(app, base_url="https://app.example")
    client.cookies.set(SESSION_COOKIE, token, domain="app.example", path="/")
    return client, token, csrf, owner


def test_hosted_startup_requires_explicit_security_configuration(tmp_path):
    with pytest.raises(ValueError, match="Hosted startup"):
        create_app(f"sqlite:///{tmp_path/'denied.db'}", settings=replace(Settings(), development=False), initialize=True)


def test_two_invited_identities_cannot_read_each_others_records(hosted):
    alice, _, csrf, alice_owner = session_client(hosted)
    bob, _, bob_csrf, bob_owner = session_client(hosted, "bob")
    assert alice_owner != bob_owner
    payload = {"command_id": str(uuid4()), "brief": sample_brief().model_dump(mode="json")}
    response = alice.post("/api/meetings", json=payload, headers={"x-csrf-token": csrf, "origin": "https://app.example"})
    assert response.status_code == 201, response.text
    meeting = response.json()
    assert bob.get(f"/api/meetings/{meeting['id']}").status_code == 404
    assert bob.get("/api/meetings").json()["items"] == []
    # Reusing the same UUID in another authenticated namespace cannot read Alice's result.
    result = bob.post("/api/meetings", json=payload, headers={"x-csrf-token": bob_csrf})
    assert result.status_code == 201
    assert result.json()["id"] != meeting["id"]


def test_csrf_and_origin_are_enforced_on_all_cookie_commands(hosted):
    client, _, csrf, _ = session_client(hosted)
    payload = {"command_id": str(uuid4()), "brief": sample_brief().model_dump(mode="json")}
    assert client.post("/api/meetings", json=payload).status_code == 403
    assert client.post("/api/meetings", json=payload, headers={"x-csrf-token": csrf, "origin": "https://outside.example"}).status_code == 403
    response = client.get("/auth/session")
    assert response.status_code == 200
    assert response.json()["csrf_token"] == csrf
    assert "token" not in response.json()


@pytest.mark.parametrize("age", [timedelta(minutes=31), timedelta(hours=13)])
def test_idle_and_absolute_expiry_fail_closed(hosted, age):
    client, _, _, _ = session_client(hosted, now=utcnow() - age)
    assert client.get("/api/meetings").status_code == 401


def test_revocation_and_uninvited_subject_fail_closed(hosted):
    client, token, _, _ = session_client(hosted)
    revoke_session(hosted.state.session_factory, token)
    assert client.get("/api/meetings").status_code == 401
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "uninvited")
    assert error.value.status_code == 403


def test_authlib_uses_pkce_openid_and_callback_sets_secure_opaque_cookie(hosted):
    assert hosted.state.oidc.client_kwargs["code_challenge_method"] == "S256"
    assert "openid" in hosted.state.oidc.client_kwargs["scope"]
    class SyntheticVerifiedOIDC:
        async def authorize_access_token(self, request):
            return {"id_token": "synthetic-verified-by-test-double", "userinfo": {"iss": "https://identity.example", "sub": "alice"}}
    hosted.state.oidc = SyntheticVerifiedOIDC()
    with TestClient(hosted, base_url="https://app.example", follow_redirects=False) as client:
        response = client.get("/auth/callback?state=synthetic&code=synthetic")
    assert response.status_code == 303
    cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith(SESSION_COOKIE + "="))
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "alice" not in cookie and "synthetic-verified" not in cookie


def test_callback_without_verified_id_token_cannot_create_a_session(hosted):
    class MissingVerifiedToken:
        async def authorize_access_token(self, request):
            return {"userinfo": {"iss": "https://identity.example", "sub": "alice"}}
    hosted.state.oidc = MissingVerifiedToken()
    with TestClient(hosted, base_url="https://app.example") as client:
        assert client.get("/auth/callback").status_code == 400
