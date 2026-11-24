"""Security boundaries for opaque sessions and login state."""

import json
from dataclasses import replace
from datetime import UTC, timedelta

import pytest
from fastapi import HTTPException, Request
from round_robin.database.models import PolicyBucket, utcnow
from round_robin.security.hosted import (
    SESSION_COOKIE,
    HostedIdentity,
    _bounded_login_state,
    _valid_login,
    create_session,
    digest,
)
from sqlalchemy import select

pytest_plugins = ("test_hosted",)


def _request(token):
    return Request({"type": "http", "method": "GET", "headers": [(b"cookie", f"{SESSION_COOKIE}={token}".encode())]})


def test_unknown_session_tokens_cannot_select_another_users_session(hosted, monkeypatch):
    values = iter(("known-token", "known-csrf"))
    monkeypatch.setattr("round_robin.security.hosted.secrets.token_urlsafe", lambda size: next(values))
    token, _, owner = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice")
    identity = HostedIdentity(hosted.state.session_factory, hosted.state.settings)
    assert identity.authenticate(_request(token))[0] == owner

    candidates = [f"unknown-{i}" for i in range(1000)]
    candidates.sort(key=digest)
    assert digest(candidates[0]) < digest(token) < digest(candidates[-1])
    for unknown in (candidates[0], candidates[-1]):
        with pytest.raises(HTTPException) as error:
            identity.authenticate(_request(unknown))
        assert error.value.status_code == 401
        assert error.value.detail["code"] == "session_revoked"


def test_session_expires_at_the_exact_absolute_deadline(hosted):
    now = utcnow()
    token, _, _ = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice", now=now - timedelta(hours=12))
    with hosted.state.session_factory.begin() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)))
        row.metadata_json = {**row.metadata_json, "last_seen_at": now.isoformat()}
    identity = HostedIdentity(hosted.state.session_factory, hosted.state.settings)
    with pytest.raises(HTTPException) as error:
        identity.authenticate(_request(token), now=now)
    assert error.value.status_code == 401
    assert error.value.detail["code"] == "session_expired"


@pytest.mark.parametrize("other", ["https://a-identity.example", "https://z-identity.example"])
def test_session_creation_requires_the_configured_issuer_even_if_other_issuer_is_allowlisted(hosted, other):
    settings = replace(hosted.state.settings, invite_allowlist=(*hosted.state.settings.invite_allowlist, (other, "alice")))
    with pytest.raises(HTTPException) as error:
        create_session(hosted.state.session_factory, settings, other, "alice")
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "invitation_required"


def test_login_state_kind_uses_value_and_requires_an_unexpired_login():
    now = utcnow()
    data = {"kind": json.loads('"login"'), "state": {}, "expires_at": (now + timedelta(seconds=1)).isoformat()}
    assert _valid_login(data, now)
    for invalid_kind in ("aaa", "session", "zzz"):
        assert not _valid_login({**data, "kind": invalid_kind}, now)
    assert not _valid_login({**data, "expires_at": now.isoformat()}, now)


def test_login_state_byte_limit_is_inclusive():
    overhead = len(json.dumps({"value": ""}, allow_nan=False).encode())
    at_limit = {"value": "x" * (16 * 1024 - overhead)}
    assert len(json.dumps(at_limit, allow_nan=False).encode()) == 16 * 1024
    assert _bounded_login_state(at_limit) == at_limit
    assert _bounded_login_state({"value": at_limit["value"] + "x"}) == {}


def test_idle_session_expires_at_the_exact_deadline(hosted):
    now = utcnow()
    token, _, _ = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice", now=now - timedelta(minutes=30))
    identity = HostedIdentity(hosted.state.session_factory, hosted.state.settings)
    with pytest.raises(HTTPException) as error:
        identity.authenticate(_request(token), now=now)
    assert error.value.detail["code"] == "session_expired"


def test_unknown_token_cannot_revoke_another_session(hosted, monkeypatch):
    from round_robin.security.hosted import revoke_session

    values = iter(("known-token", "known-csrf"))
    monkeypatch.setattr("round_robin.security.hosted.secrets.token_urlsafe", lambda size: next(values))
    token, _, owner = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice")
    candidates = sorted((f"unknown-{i}" for i in range(1000)), key=digest)
    for unknown in (candidates[0], candidates[-1]):
        revoke_session(hosted.state.session_factory, unknown)
        assert HostedIdentity(hosted.state.session_factory, hosted.state.settings).authenticate(_request(token))[0] == owner
    revoke_session(hosted.state.session_factory, token)
    with hosted.state.session_factory() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)))
        assert row.metadata_json["revoked"] is True
        assert row.metadata_json["csrf_token"] == ""


def test_login_state_accepts_eight_entries_but_not_nine():
    eight = {str(i): i for i in range(8)}
    assert _bounded_login_state(eight) == eight
    assert _bounded_login_state({**eight, "extra": 8}) == {}


def test_authentication_error_exposes_login_retry_scope():
    from round_robin.security.hosted import forbidden

    with pytest.raises(HTTPException) as denied:
        forbidden("session_required", "Sign in", 401)
    assert denied.value.detail["retry_scope"] == "login"
    with pytest.raises(HTTPException) as invalid:
        forbidden("oidc_callback_invalid", "Restart sign-in", 400)
    assert invalid.value.detail["retry_scope"] == "none"
    with pytest.raises(HTTPException) as forbidden_error:
        forbidden("invitation_required", "Not invited", 403)
    assert forbidden_error.value.detail["retry_scope"] == "none"


def test_callback_clears_login_cookie_with_secure_attributes(hosted):
    from fastapi.testclient import TestClient
    from round_robin.security.hosted import LOGIN_COOKIE

    with TestClient(hosted, base_url="https://app.example") as client:
        response = client.get("/auth/callback")
    cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith(LOGIN_COOKIE + "="))
    assert "Max-Age=0" in cookie
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie


def test_login_state_is_consumed_only_by_callback(hosted):
    from round_robin.security.hosted import OpaqueLoginState

    key = "login:known"
    initial = {"nonce": "synthetic"}
    with hosted.state.session_factory.begin() as session:
        session.add(PolicyBucket(id="login_known", key=key, limit_usd="0", spending_locked=True,
            metadata_json={"kind": "login", "expires_at": (utcnow() + timedelta(minutes=5)).isoformat(), "state": initial}))
    middleware = OpaqueLoginState(None, hosted.state.session_factory)
    assert middleware._reserve(key, "generation-login", "/auth/login") == (initial, False)
    with hosted.state.session_factory() as session:
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == key)).metadata_json["state"] == initial
    assert middleware._reserve(key, "generation-callback", "/auth/callback") == (initial, False)
    with hosted.state.session_factory() as session:
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == key)).metadata_json["state"] == {}


def test_unknown_login_state_key_cannot_update_an_existing_binding(hosted):
    from round_robin.security.hosted import OpaqueLoginState

    key = "login:middle"
    initial = {"kind": "login", "generation": "generation", "expires_at": (utcnow() + timedelta(minutes=5)).isoformat(), "state": {"nonce": "original"}}
    with hosted.state.session_factory.begin() as session:
        session.add(PolicyBucket(id="login_known", key=key, limit_usd="0", spending_locked=True, metadata_json=initial))
    middleware = OpaqueLoginState(None, hosted.state.session_factory)
    for unknown in ("login:aaa", "login:zzz"):
        middleware._save(unknown, "generation", {**initial, "state": {"nonce": "foreign"}})
        with hosted.state.session_factory() as session:
            assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == key)).metadata_json == initial


@pytest.mark.parametrize("kind", ["login", "zzz"])
def test_login_record_cannot_be_accepted_as_a_session(hosted, kind):
    token, _, _ = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice")
    with hosted.state.session_factory.begin() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)))
        row.metadata_json = {**row.metadata_json, "kind": kind}
    with pytest.raises(HTTPException) as error:
        HostedIdentity(hosted.state.session_factory, hosted.state.settings).authenticate(_request(token))
    assert error.value.detail["code"] == "session_revoked"


def test_session_cookie_length_boundary(hosted):
    identity = HostedIdentity(hosted.state.session_factory, hosted.state.settings)
    with pytest.raises(HTTPException) as at_limit:
        identity.authenticate(_request("x" * 128))
    assert at_limit.value.detail["code"] == "session_revoked"
    with pytest.raises(HTTPException) as over_limit:
        identity.authenticate(_request("x" * 129))
    assert over_limit.value.detail["code"] == "session_required"


def test_timestamps_keep_explicit_offsets_and_assume_utc_for_naive_values():
    from datetime import datetime

    from round_robin.security.hosted import instant

    assert instant("2026-01-02T03:04:05") == datetime.fromisoformat("2026-01-02T03:04:05+00:00")
    offset = instant("2026-01-02T03:04:05+05:30")
    assert offset.utcoffset() == timedelta(hours=5, minutes=30)
    assert offset.astimezone(UTC) == datetime.fromisoformat("2026-01-01T21:34:05+00:00")


def test_session_owner_must_match_issuer_and_subject_for_both_sort_orders(hosted):
    token, _, _ = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice")
    identity = HostedIdentity(hosted.state.session_factory, hosted.state.settings)
    for owner in ("owner_" + "0" * 48, "owner_" + "z" * 48):
        with hosted.state.session_factory.begin() as session:
            row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)))
            row.metadata_json = {**row.metadata_json, "owner_id": owner}
        with pytest.raises(HTTPException) as error:
            identity.authenticate(_request(token))
        assert error.value.detail["code"] == "session_revoked"


def test_cross_origin_commands_reject_origins_on_both_sides_of_public_origin(hosted):
    token, csrf, _ = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice")
    identity = HostedIdentity(hosted.state.session_factory, hosted.state.settings)
    for origin in ("https://a.example", "https://z.example"):
        request = Request({"type": "http", "method": "POST", "headers": [
            (b"cookie", f"{SESSION_COOKIE}={token}".encode()),
            (b"x-csrf-token", csrf.encode()), (b"origin", origin.encode()),
        ]})
        with pytest.raises(HTTPException) as error:
            identity.authenticate(request)
        assert error.value.detail["code"] == "origin_invalid"


def test_login_sets_secure_opaque_browser_cookie(hosted):
    from types import SimpleNamespace

    from fastapi.responses import RedirectResponse
    from fastapi.testclient import TestClient
    from round_robin.security.hosted import LOGIN_COOKIE

    async def redirect(request, *args, **kwargs):
        request.session["nonce"] = "synthetic"
        return RedirectResponse("https://identity.example/authorize")

    hosted.state.oidc = SimpleNamespace(authorize_redirect=redirect)
    with TestClient(hosted, base_url="https://app.example", follow_redirects=False) as client:
        response = client.get("/auth/login")
    cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith(LOGIN_COOKIE + "="))
    assert "Max-Age=600" in cookie
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "synthetic" not in cookie
    assert len(response.headers.get_list("content-length")) == 1


@pytest.mark.parametrize("metadata", [None, [], "not a mapping"])
def test_malformed_session_metadata_fails_closed(hosted, metadata):
    token, _, _ = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice")
    with hosted.state.session_factory.begin() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)))
        row.metadata_json = metadata
    with pytest.raises(HTTPException) as error:
        HostedIdentity(hosted.state.session_factory, hosted.state.settings).authenticate(_request(token))
    assert error.value.detail["code"] == "session_revoked"


def test_login_reservation_caps_active_bindings_at_one_thousand(hosted):
    from round_robin.security.hosted import OpaqueLoginState

    metadata = {"kind": "login", "expires_at": (utcnow() + timedelta(minutes=5)).isoformat(), "state": {}}
    with hosted.state.session_factory.begin() as session:
        session.add_all(PolicyBucket(id=f"login_limit_{i}", key=f"login:limit_{i}", limit_usd="0", spending_locked=True, metadata_json=metadata) for i in range(999))
    middleware = OpaqueLoginState(None, hosted.state.session_factory)
    assert middleware._reserve("login:allowed", "generation", "/auth/login") == ({}, False)
    assert middleware._reserve("login:new", "generation", "/auth/login") == ({}, True)
    with hosted.state.session_factory() as session:
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == "login:new")) is None
    with hosted.state.session_factory.begin() as session:
        session.add(PolicyBucket(id="login_excess", key="login:excess", limit_usd="0", spending_locked=True, metadata_json=metadata))
    assert middleware._reserve("login:new", "generation", "/auth/login") == ({}, True)


def test_wrong_nonempty_csrf_token_is_rejected(hosted):
    token, _, _ = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice")
    request = Request({"type": "http", "method": "POST", "headers": [
        (b"cookie", f"{SESSION_COOKIE}={token}".encode()), (b"x-csrf-token", b"wrong-nonempty-token"),
    ]})
    with pytest.raises(HTTPException) as error:
        HostedIdentity(hosted.state.session_factory, hosted.state.settings).authenticate(request)
    assert error.value.detail["code"] == "csrf_invalid"


def test_session_expired_before_now_is_rejected_even_if_recently_seen(hosted):
    now = utcnow()
    token, _, _ = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice", now=now - timedelta(hours=12, seconds=1))
    with hosted.state.session_factory.begin() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)))
        row.metadata_json = {**row.metadata_json, "last_seen_at": now.isoformat()}
    with pytest.raises(HTTPException) as error:
        HostedIdentity(hosted.state.session_factory, hosted.state.settings).authenticate(_request(token), now=now)
    assert error.value.detail["code"] == "session_expired"


@pytest.mark.parametrize("length,reuse", [(128, True), (129, False)])
def test_login_cookie_length_boundary(hosted, length, reuse):
    from types import SimpleNamespace

    from fastapi.responses import RedirectResponse
    from fastapi.testclient import TestClient
    from round_robin.security.hosted import LOGIN_COOKIE

    async def redirect(request, *args, **kwargs):
        return RedirectResponse("https://identity.example/authorize")

    hosted.state.oidc = SimpleNamespace(authorize_redirect=redirect)
    old_cookie = "x" * length
    with TestClient(hosted, base_url="https://app.example", follow_redirects=False) as client:
        client.cookies.set(LOGIN_COOKIE, old_cookie, domain="app.example", path="/")
        response = client.get("/auth/login")
    assert (response.cookies.get(LOGIN_COOKIE) == old_cookie) is reuse


def test_login_middleware_passes_non_http_scope_without_accessing_a_path(hosted):
    import asyncio

    from round_robin.security.hosted import OpaqueLoginState

    calls = []
    async def application(scope, receive, send):
        calls.append(scope["type"])

    asyncio.run(OpaqueLoginState(application, hosted.state.session_factory)({"type": "lifespan"}, None, None))
    assert calls == ["lifespan"]


def test_login_middleware_does_not_add_cookies_to_response_trailers(hosted):
    import asyncio

    from round_robin.security.hosted import OpaqueLoginState

    messages = []
    async def application(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [], "trailers": True})
        await send({"type": "http.response.body", "body": b"ok"})
        await send({"type": "http.response.trailers", "headers": [(b"x-example", b"ok")]})

    async def capture(message):
        messages.append(message)

    scope = {"type": json.loads('"http"'), "path": "/auth/login", "method": "GET", "headers": []}
    asyncio.run(OpaqueLoginState(application, hosted.state.session_factory)(scope, None, capture))
    assert any(name == b"set-cookie" for name, _ in messages[0]["headers"])
    assert "headers" not in messages[1]
    assert messages[2]["headers"] == [(b"x-example", b"ok")]


def test_reserved_login_state_expires_after_ten_minutes(hosted, monkeypatch):
    from round_robin.security.hosted import OpaqueLoginState, instant

    now = utcnow()
    monkeypatch.setattr("round_robin.security.hosted.utcnow", lambda: now)
    middleware = OpaqueLoginState(None, hosted.state.session_factory)
    assert middleware._reserve("login:new", "generation", "/auth/login") == ({}, False)
    with hosted.state.session_factory() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "login:new"))
        assert instant(row.metadata_json["expires_at"]) == now + timedelta(minutes=10)


def test_saved_login_state_expires_after_ten_minutes(hosted, monkeypatch):
    from types import SimpleNamespace

    from fastapi.responses import RedirectResponse
    from fastapi.testclient import TestClient
    from round_robin.security.hosted import LOGIN_COOKIE, instant

    now = utcnow()
    monkeypatch.setattr("round_robin.security.hosted.utcnow", lambda: now)
    async def redirect(request, *args, **kwargs):
        request.session["nonce"] = "synthetic"
        return RedirectResponse("https://identity.example/authorize")

    hosted.state.oidc = SimpleNamespace(authorize_redirect=redirect)
    with TestClient(hosted, base_url="https://app.example", follow_redirects=False) as client:
        response = client.get("/auth/login")
    token = response.cookies.get(LOGIN_COOKIE)
    assert token
    with hosted.state.session_factory() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "login:" + digest(token)))
        assert instant(row.metadata_json["expires_at"]) == now + timedelta(minutes=10)


@pytest.mark.parametrize("mutation", [{"revoked": 0}, {"csrf_token": 123}])
def test_session_rejects_wrong_security_field_types(hosted, mutation):
    from sqlalchemy.orm.attributes import flag_modified

    token, _, _ = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice")
    with hosted.state.session_factory.begin() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)))
        row.metadata_json = {**row.metadata_json, **mutation}
        flag_modified(row, "metadata_json")
    with pytest.raises(HTTPException) as error:
        HostedIdentity(hosted.state.session_factory, hosted.state.settings).authenticate(_request(token))
    assert error.value.detail["code"] == "session_revoked"


def test_session_remains_valid_until_absolute_deadline(hosted):
    now = utcnow()
    token, _, owner = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice", now=now - timedelta(hours=12) + timedelta(seconds=1))
    with hosted.state.session_factory.begin() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)))
        row.metadata_json = {**row.metadata_json, "last_seen_at": now.isoformat()}
    assert HostedIdentity(hosted.state.session_factory, hosted.state.settings).authenticate(_request(token), now=now)[0] == owner


def test_session_and_login_storage_buckets_are_spending_locked(hosted):
    from round_robin.security.hosted import OpaqueLoginState

    token, _, _ = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice")
    assert OpaqueLoginState(None, hosted.state.session_factory)._reserve("login:spending-check", "generation", "/auth/login") == ({}, False)
    with hosted.state.session_factory() as session:
        for key in ("session:" + digest(token), "login:spending-check"):
            row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == key))
            assert row.spending_locked is True
            assert row.limit_usd == "0"


def test_session_remains_valid_until_idle_deadline(hosted):
    now = utcnow()
    token, _, owner = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice", now=now - timedelta(minutes=30) + timedelta(seconds=1))
    assert HostedIdentity(hosted.state.session_factory, hosted.state.settings).authenticate(_request(token), now=now)[0] == owner


def test_session_helpers_accept_their_named_arguments(hosted):
    token, _, owner = create_session(
        session_factory=hosted.state.session_factory,
        settings=hosted.state.settings,
        issuer="https://identity.example",
        subject="alice",
    )
    identity = HostedIdentity(hosted.state.session_factory, hosted.state.settings)
    assert identity.authenticate(request=_request(token))[0] == owner


def test_hosted_tokens_and_generation_have_minimum_security_strength(hosted):
    import base64
    from types import SimpleNamespace

    from fastapi.responses import RedirectResponse
    from fastapi.testclient import TestClient
    from round_robin.security.hosted import LOGIN_COOKIE

    def decoded_length(value):
        return len(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))

    token, csrf, _ = create_session(hosted.state.session_factory, hosted.state.settings, "https://identity.example", "alice")
    assert decoded_length(token) >= 32
    assert decoded_length(csrf) >= 32

    async def redirect(request, *args, **kwargs):
        request.session["nonce"] = "synthetic"
        return RedirectResponse("https://identity.example/authorize")

    hosted.state.oidc = SimpleNamespace(authorize_redirect=redirect)
    with TestClient(hosted, base_url="https://app.example", follow_redirects=False) as client:
        response = client.get("/auth/login")
    login_token = response.cookies.get(LOGIN_COOKIE)
    assert login_token
    assert decoded_length(login_token) >= 32
    with hosted.state.session_factory() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "login:" + digest(login_token)))
        generation = row.metadata_json["generation"]
        assert len(bytes.fromhex(generation)) >= 16
