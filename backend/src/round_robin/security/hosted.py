"""Opaque server sessions and Authlib OIDC; no handwritten token verification."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets

from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from round_robin.database.models import PolicyBucket, utcnow
from round_robin.database.services import lock_policies, owner_lock

SESSION_COOKIE = "__Host-round_robin"
LOGIN_COOKIE = "__Host-round_robin_login"
IDLE_SECONDS = 1800
ABSOLUTE_SECONDS = 43200
MAX_LOGIN_STATES = 1000
MAX_LOGIN_STATE_BYTES = 16384


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _valid_login(data, now):
    try:
        return isinstance(data, dict) and data.get("kind") == "login" and not data.get("revoked") and isinstance(data.get("state"), dict) and instant(data["expires_at"]) > now
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _bounded_login_state(state):
    try:
        if not isinstance(state, dict) or len(state) > 8 or len(json.dumps(state, allow_nan=False).encode()) > MAX_LOGIN_STATE_BYTES:
            return {}
        return state
    except (ValueError, TypeError, RecursionError):
        return {}


def forbidden(code: str, message: str, status=401):
    raise HTTPException(status, detail={"code": code, "message": message, "retry_scope": "login" if status == 401 else "none"})


def create_session(session_factory, settings, issuer: str, subject: str, *, now=None) -> tuple[str, str, str]:
    if issuer != settings.oidc_issuer or (issuer, subject) not in settings.invite_allowlist:
        forbidden("invitation_required", "This identity is not invited", 403)
    now = now or utcnow()
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    owner_id = "owner_" + digest(issuer + "\0" + subject)[:48]
    with session_factory.begin() as session:
        owner_lock(session, owner_id)
        session.add(PolicyBucket(id="session_" + digest(token), key="session:" + digest(token), limit_usd="0", spending_locked=True, metadata_json={"kind": "session", "owner_id": owner_id, "issuer": issuer, "subject": subject, "created_at": now.isoformat(), "last_seen_at": now.isoformat(), "expires_at": (now + timedelta(seconds=ABSOLUTE_SECONDS)).isoformat(), "csrf_token": csrf, "revoked": False}))
    return token, csrf, owner_id


class HostedIdentity:
    def __init__(self, session_factory, settings):
        self.session_factory, self.settings = session_factory, settings

    def authenticate(self, request: Request, *, now=None, csrf=True) -> tuple[str, dict]:
        token = request.cookies.get(SESSION_COOKIE)
        if not token or len(token) > 128:
            forbidden("session_required", "Sign in to continue")
        now = now or utcnow()
        with self.session_factory.begin() as session:
            row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)).with_for_update())
            data = row.metadata_json if row else {}
            if not row or not isinstance(data, dict) or data.get("kind") != "session" or data.get("revoked") is not False:
                forbidden("session_revoked", "Session is unavailable or revoked")
            try:
                expiry, last_seen = instant(data["expires_at"]), instant(data["last_seen_at"])
                if not isinstance(data["csrf_token"], str) or not data["csrf_token"] or data["owner_id"] != "owner_" + digest(data["issuer"] + "\0" + data["subject"])[:48]:
                    raise ValueError
            except (KeyError, ValueError, TypeError, AttributeError):
                forbidden("session_revoked", "Session is unavailable or revoked")
            if expiry <= now or (now - last_seen).total_seconds() >= IDLE_SECONDS:
                forbidden("session_expired", "Session expired; sign in again")
            if (data.get("issuer"), data.get("subject")) not in self.settings.invite_allowlist:
                forbidden("invitation_required", "Invited access is no longer available", 403)
            if csrf and request.method not in {"GET", "HEAD", "OPTIONS"}:
                supplied = request.headers.get("x-csrf-token", "")
                if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), data["csrf_token"].encode("utf-8")):
                    forbidden("csrf_invalid", "Refresh the session token before this command", 403)
                if request.headers.get("origin") and request.headers["origin"] != self.settings.public_origin:
                    forbidden("origin_invalid", "Command origin is not allowed", 403)
            row.metadata_json = {**data, "last_seen_at": now.isoformat()}
            return data["owner_id"], data

    def __call__(self, request: Request) -> str:
        return self.authenticate(request)[0]


def revoke_session(session_factory, token: str):
    with session_factory.begin() as session:
        row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)).with_for_update())
        if row:
            row.metadata_json = {**row.metadata_json, "revoked": True, "csrf_token": ""}


class OpaqueLoginState:
    """Supply Authlib's session contract using an opaque, server-stored browser binding."""
    def __init__(self, app, session_factory):
        self.app, self.session_factory = app, session_factory

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] not in {"/auth/login", "/auth/callback"}:
            return await self.app(scope, receive, send)
        request = Request(scope)
        existing = request.cookies.get(LOGIN_COOKIE)
        token = existing if existing and len(existing) <= 128 else secrets.token_urlsafe(32)
        key = "login:" + digest(token)
        generation = secrets.token_hex(16)
        state, capacity_exhausted = await run_in_threadpool(self._reserve, key, generation, scope["path"])
        if capacity_exhausted:
            from starlette.responses import JSONResponse
            response = JSONResponse({"error": {"code": "login_capacity_exhausted", "message": "Sign-in capacity is temporarily full; retry shortly", "retry_scope": "login"}}, status_code=429, headers={"Retry-After": "60"})
            return await response(scope, receive, send)
        scope["session"] = state

        async def save(message):
            if message["type"] == "http.response.start":
                payload = {"kind": "login", "generation": generation, "expires_at": (utcnow() + timedelta(minutes=10)).isoformat(), "state": {} if scope["path"] == "/auth/callback" else _bounded_login_state(scope["session"])}
                await run_in_threadpool(self._save, key, generation, payload)
                from starlette.responses import Response
                cookie = Response()
                if scope["path"] == "/auth/callback":
                    cookie.delete_cookie(LOGIN_COOKIE, secure=True, httponly=True, samesite="lax")
                else:
                    cookie.set_cookie(LOGIN_COOKIE, token, max_age=600, secure=True, httponly=True, samesite="lax")
                message.setdefault("headers", []).extend(header for header in cookie.raw_headers if header[0] == b"set-cookie")
            await send(message)
        return await self.app(scope, receive, save)

    def _reserve(self, key, generation, path):
        # Keep the complete synchronous transaction on one worker thread. Row
        # contention must not stop unrelated requests or SSE on the event loop.
        state, capacity_exhausted = {}, False
        with self.session_factory.begin() as session:
            # Anonymous login attempts share one serialized deployment quota.
            # Reserve a row before awaiting the identity provider so concurrent
            # requests cannot all pass a count and publish later.
            lock_policies(session, "owner_operator")
            now = utcnow()
            for stale in session.scalars(select(PolicyBucket).where(PolicyBucket.key.startswith("login:")).order_by(PolicyBucket.id).limit(MAX_LOGIN_STATES)):
                if not _valid_login(stale.metadata_json, now):
                    session.delete(stale)
            session.flush()
            row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == key).with_for_update())
            if row is None and path == "/auth/login":
                count = session.scalar(select(func.count()).select_from(PolicyBucket).where(PolicyBucket.key.startswith("login:")))
                if count >= MAX_LOGIN_STATES:
                    capacity_exhausted = True
                else:
                    row = PolicyBucket(id="login_" + key.removeprefix("login:"), key=key, limit_usd="0", spending_locked=True,
                        metadata_json={"kind": "login", "expires_at": (now + timedelta(minutes=10)).isoformat(), "state": {}})
                    session.add(row)
            if row and _valid_login(row.metadata_json, now):
                state = row.metadata_json.get("state", {})
                # Fence responses from overlapping browser requests. A late
                # login response must never restore a callback-consumed nonce.
                row.metadata_json = {**row.metadata_json, "generation": generation}
                if path == "/auth/callback":
                    # Consume the browser-bound login state before token exchange;
                    # a simultaneous callback cannot reuse it after this commit.
                    row.metadata_json = {**row.metadata_json, "state": {}}
        return state, capacity_exhausted

    def _save(self, key, generation, payload):
        with self.session_factory.begin() as session:
            row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == key).with_for_update())
            # Callback state stays single-use, and a late login response cannot
            # restore state already consumed by an overlapping callback.
            if row and row.metadata_json.get("generation") == generation:
                row.metadata_json = payload


def build_oidc(settings):
    oauth = OAuth()
    oauth.register(name="identity", client_id=settings.oidc_client_id, client_secret=settings.oidc_client_secret, server_metadata_url=settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration", client_kwargs={"scope": "openid profile", "code_challenge_method": "S256"})
    return oauth.create_client("identity")
