"""Fresh adversarial cases derived from source, independent of earlier audits."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import logging
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from round_robin.database.models import PolicyBucket, utcnow
from round_robin.reports.storage import ArtifactStore
from round_robin.research.fetcher import FetchRejected, validate_target
from round_robin.security.hosted import digest
from test_hosted import hosted, session_client


@pytest.mark.parametrize("query", [
    "label=%23safe&token=SYNTHETIC_PRIVATE",
    "label=%2523safe&apiKey=SYNTHETIC_PRIVATE",
    "label=%3Fsafe%23&client_secret=SYNTHETIC_PRIVATE",
    "label=%23safe%26accessToken%3DSYNTHETIC_PRIVATE",
])
def test_encoded_fragment_cannot_hide_query_credentials(query):
    resolved = []
    with pytest.raises(FetchRejected, match="Secret-bearing"):
        validate_target("https://example.com/?" + query,
                        resolver=lambda host: resolved.append(host) or ["93.184.216.34"])
    assert resolved == []


def test_artifact_directory_fsync_failure_removes_uncommitted_private_bytes(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "private")
    original = os.fsync
    calls = 0
    def failing_directory_sync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic directory durability failure")
        original(descriptor)
    monkeypatch.setattr(os, "fsync", failing_directory_sync)
    with pytest.raises(OSError):
        store.put(b"SYNTHETIC_PRIVATE_EXPORT")
    assert list(store.directory.iterdir()) == []


@pytest.mark.parametrize("mutation", [
    {"expires_at": "not-an-instant"}, {"last_seen_at": None},
    {"csrf_token": None}, {"owner_id": "owner_foreign"},
    {"kind": "session", "revoked": "false"},
])
def test_damaged_hosted_session_is_rejected_with_no_private_error(hosted, mutation):
    client, token, csrf, _ = session_client(hosted)
    with hosted.state.session_factory.begin() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)))
        row.metadata_json = {**row.metadata_json, **mutation}
    response = client.post("/auth/logout", headers={"x-csrf-token": csrf})
    assert response.status_code == 401
    assert "SYNTHETIC_PRIVATE" not in response.text


def test_invitation_removal_revokes_existing_session_and_oversized_cookie_is_denied(hosted):
    client, _, _, _ = session_client(hosted)
    hosted.state.settings = replace(hosted.state.settings, invite_allowlist=())
    # The dependency owns the frozen runtime settings; construct it directly to
    # model an actual process restart with the changed operator allowlist.
    from fastapi import HTTPException, Request
    from round_robin.security.hosted import HostedIdentity, SESSION_COOKIE, revoke_session
    identity = HostedIdentity(hosted.state.session_factory, hosted.state.settings)
    cookie = next(iter(client.cookies))
    token = client.cookies.get(cookie)
    def request(value):
        return Request({"type": "http", "method": "GET", "headers": [(b"cookie", f"{SESSION_COOKIE}={value}".encode())]})
    with pytest.raises(HTTPException) as denied:
        identity.authenticate(request(token))
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied:
        identity.authenticate(request("x" * 129))
    assert denied.value.status_code == 401
    revoke_session(hosted.state.session_factory, "unknown-synthetic-cookie")


def test_access_log_filter_preserves_nonquery_record_shapes():
    from round_robin.security.logging import RedactAccessQueries
    for args in [(), ("message",), ("127.0.0.1", "GET", "/health", "HTTP/1.1", 200),
                 ("127.0.0.1", "GET", None, "HTTP/1.1", 200)]:
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "message", args, None)
        assert RedactAccessQueries().filter(record)
        assert record.args == args


def test_login_callback_error_cannot_restore_consumed_browser_state(hosted):
    import asyncio
    from round_robin.security.hosted import LOGIN_COOKIE, OpaqueLoginState
    token = "synthetic-browser-binding"
    key = "login:" + digest(token)
    state = {"_state_identity_nonce": {"data": {"nonce": "synthetic-nonce"}}}
    with hosted.state.session_factory.begin() as session:
        session.add(PolicyBucket(id="login_synthetic", key=key, limit_usd="0", spending_locked=True,
            metadata_json={"kind": "login", "expires_at": (utcnow()+timedelta(minutes=5)).isoformat(), "state": state}))
    observed = []
    async def denied(scope, receive, send):
        observed.append(deepcopy(scope["session"]))
        await send({"type": "http.response.start", "status": 400, "headers": []})
        await send({"type": "http.response.body", "body": b"identity exchange denied"})
    from copy import deepcopy
    middleware = OpaqueLoginState(denied, hosted.state.session_factory)
    async def invoke(path, binding=token):
        async def send(message): pass
        async def receive(): return {"type": "http.request", "body": b""}
        await middleware({"type": "http", "path": path, "method": "GET",
            "headers": [(b"cookie", f"{LOGIN_COOKIE}={binding}".encode())]}, receive, send)
    asyncio.run(invoke("/auth/login"))
    asyncio.run(invoke("/auth/callback"))
    asyncio.run(invoke("/auth/callback"))
    assert observed == [state, state, {}]
    asyncio.run(invoke("/auth/callback", "unknown-binding"))
    asyncio.run(invoke("/auth/login", "new-binding"))
    with hosted.state.session_factory() as session:
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == key)).metadata_json["state"] == {}
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == "login:"+digest("unknown-binding"))) is None
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == "login:"+digest("new-binding"))).metadata_json["state"] == {}


def test_anonymous_login_reservations_are_bounded_before_oidc_and_expired_states_are_cleaned(hosted,monkeypatch):
    from fastapi.responses import RedirectResponse
    from fastapi.testclient import TestClient
    from round_robin.security import hosted as identity
    monkeypatch.setattr(identity,"MAX_LOGIN_STATES",2)
    calls=[]
    async def redirect(request,*args,**kwargs):
        calls.append(True)
        request.session["nonce"]={"secret":"synthetic"}
        return RedirectResponse("https://identity.example/authorize")
    hosted.state.oidc=SimpleNamespace(authorize_redirect=redirect)
    for _ in range(2):
        with TestClient(hosted,base_url="https://app.example",follow_redirects=False) as client:
            assert client.get("/auth/login").status_code==307
    with TestClient(hosted,base_url="https://app.example",follow_redirects=False) as client:
        response=client.get("/auth/login")
        assert response.status_code==429 and response.headers["retry-after"]=="60"
    assert len(calls)==2
    with hosted.state.session_factory.begin() as session:
        rows=list(session.scalars(select(PolicyBucket).where(PolicyBucket.key.startswith("login:"))))
        assert len(rows)==2
        rows[0].metadata_json={**rows[0].metadata_json,"expires_at":"corrupt"}
        rows[1].metadata_json={**rows[1].metadata_json,"expires_at":(utcnow()-timedelta(seconds=1)).isoformat()}
    with TestClient(hosted,base_url="https://app.example",follow_redirects=False) as client:
        assert client.get("/auth/login").status_code==307
    with hosted.state.session_factory() as session:
        assert len(list(session.scalars(select(PolicyBucket).where(PolicyBucket.key.startswith("login:")))))==1
    assert len(calls)==3


@pytest.mark.parametrize("state",[None, {str(n):{} for n in range(9)}, {"nonce":"x"*16384}, {"nan":float("nan")}, {"set":set()}])
def test_unbounded_or_nonjson_oidc_state_is_never_persisted(state):
    from round_robin.security.hosted import _bounded_login_state
    assert _bounded_login_state(state)=={}


@pytest.mark.parametrize("paused_path", ["/auth/login", "/auth/callback"])
def test_late_oidc_response_cannot_republish_consumed_state_or_erase_new_login(hosted, paused_path):
    import asyncio
    from copy import deepcopy
    from round_robin.security.hosted import LOGIN_COOKIE, OpaqueLoginState
    binding = "synthetic-overlapping-browser"
    key = "login:" + digest(binding)
    old_state = {"nonce": {"value": "synthetic-old"}}
    new_state = {"nonce": {"value": "synthetic-new"}}
    with hosted.state.session_factory.begin() as session:
        session.add(PolicyBucket(id="login_overlap", key=key, limit_usd="0", spending_locked=True,
            metadata_json={"kind": "login", "expires_at": (utcnow()+timedelta(minutes=5)).isoformat(), "state": old_state}))
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        observed = []
        async def oidc(scope, receive, send):
            observed.append(deepcopy(scope["session"]))
            if scope.get("pause"):
                entered.set()
                await release.wait()
            elif scope["path"] == "/auth/login":
                scope["session"] = deepcopy(new_state)
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"synthetic response"})
        middleware = OpaqueLoginState(oidc, hosted.state.session_factory)
        async def invoke(path, pause=False):
            async def send(message): pass
            async def receive(): return {"type": "http.request", "body": b""}
            await middleware({"type": "http", "path": path, "method": "GET", "pause": pause,
                "headers": [(b"cookie", f"{LOGIN_COOKIE}={binding}".encode())]}, receive, send)
        pending = asyncio.create_task(invoke(paused_path, True))
        await entered.wait()
        await invoke("/auth/callback" if paused_path == "/auth/login" else "/auth/login")
        release.set()
        await pending
        await invoke("/auth/callback")
        assert observed[-1] == ({} if paused_path == "/auth/login" else new_state)
    asyncio.run(exercise())
