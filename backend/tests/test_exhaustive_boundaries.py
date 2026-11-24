"""Fresh HTTP/ASGI boundary attacks, lifecycle failures and contract tooling."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import errno
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

from alembic import command as migration
from alembic.config import Config
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from backend.tests.test_api import application, client, command, create, start
from backend.tests.test_hosted import hosted, hosted_settings
from round_robin.api import auth, events
from round_robin.app import JsonBodyLimit, create_app
from round_robin.configuration import Settings, _canonical_public_origin
from round_robin.database.models import PolicyBucket, Run, utcnow


@pytest.mark.parametrize("mode", ["chunked", "disconnect", "timeout", "overflow", "passthrough"])
def test_json_boundary_bounds_time_and_bytes_before_calling_application(mode):
    async def exercise():
        sent, delivered = [], []
        packets = [{"type": "http.request", "body": b'{"a":', "more_body": True}, {"type": "http.request", "body": b"1}", "more_body": False}, {"type": "http.disconnect"}]
        if mode == "disconnect":
            packets = [{"type": "http.disconnect"}]
        elif mode == "overflow":
            packets[0]["body"] = b"x" * (128 * 1024)
        async def receive():
            if mode == "timeout":
                await asyncio.Event().wait()
            return packets.pop(0)
        async def send(item):
            sent.append(item)
        async def downstream(scope, receive, send):
            delivered.append(await receive())
            delivered.append(await receive())
        middleware = JsonBodyLimit(downstream)
        middleware.timeout_seconds = .005
        scope = {"type": "http", "method": "POST", "path": "/api/upload/images" if mode == "passthrough" else "/api/meetings"}
        await middleware(scope, receive, send)
        if mode in {"timeout", "overflow"}:
            assert not delivered
            assert sent[0]["status"] == (408 if mode == "timeout" else 413)
            assert json.loads(sent[1]["body"])["error"]["command_outcome"] == "not_committed"
        elif mode == "disconnect":
            assert not delivered and not sent
        elif mode == "chunked":
            assert delivered == [{"type": "http.request", "body": b'{"a":1}', "more_body": False}, {"type": "http.disconnect"}]
        else:
            assert delivered[0]["more_body"] is True
    asyncio.run(exercise())


@pytest.mark.parametrize("origin", ["https://[", "http://localhost:bad", "http://localhost:0", "http://localhost@outside.example", "file://localhost", "http://localhost/private", "http://lo\ncalhost"])
def test_fixture_origin_parser_rejects_malformed_or_non_web_origins(origin):
    from round_robin.fixture_api import app
    with TestClient(app) as client:
        assert client.get("/api/sample", headers={"origin": origin}).status_code == 403


def test_fixture_loopback_origin_and_forwarded_protocol_boundary():
    from round_robin.fixture_api import app
    with TestClient(app) as client:
        assert client.get("/api/sample", headers={"origin": "http://localhost:3000"}).status_code == 200
        assert client.get("/api/sample", headers={"x-forwarded-proto": "https"}).status_code == 403


@pytest.mark.parametrize("change", [
    {"command_hash_key": "short"},
    {"invite_allowlist": (("https://other.example", "alice"),)},
    {"oidc_issuer": "https://user:password@identity.example"},
    {"oidc_issuer": "https://identity.example/?secret=x"},
    {"public_origin": "http://app.example"},
    {"public_origin": "https://app.example/private"},
    {"execution_mode": "unreviewed"},
    {"execution_mode": "live", "paid_access_enabled": False},
])
def test_hosted_configuration_fails_before_opening_untrusted_connections(tmp_path, change):
    with pytest.raises(ValueError):
        replace(hosted_settings(tmp_path), **change).validate()


def test_no_metadata_creation_for_postgres_and_default_session_is_usable(application, monkeypatch):
    with pytest.raises(ValueError, match="Alembic"):
        create_app("postgresql+psycopg://operator@127.0.0.1/guard_test", initialize=True)
    from round_robin.database import session as database
    monkeypatch.setattr(database, "SessionLocal", application.state.session_factory)
    generator = database.get_session()
    with next(generator) as session:
        assert session.scalar(select(PolicyBucket)) is None
    with pytest.raises(StopIteration):
        next(generator)


@pytest.mark.parametrize("metadata,reason", [({"durability_compromised": True}, "durability_compromised"), ({"restore_quarantine": True}, "restoration_or_dispatch_quarantine"), ({"dispatch_locked": True}, "restoration_or_dispatch_quarantine")])
def test_readiness_reflects_persistent_safety_gates(client, application, metadata, reason):
    with application.state.session_factory.begin() as session:
        session.add(PolicyBucket(id="policy_readiness", key="deployment", metadata_json=metadata))
    response = client.get("/ready")
    assert response.status_code == 503 and response.json()["reason"] == reason
    if metadata.get("restore_quarantine"):
        assert client.get("/api/meetings").json()["error"]["code"] == "restore_quarantine"


def test_health_configuration_failure_is_nonsecret_and_makes_no_provider_call(client, application, tmp_path):
    application.state.settings = replace(application.state.settings, provider_config_file=tmp_path / "absent")
    # The health closure intentionally uses the startup configuration, so
    # create a separate app with the invalid operator file at startup.
    app = create_app(f"sqlite:///{tmp_path / 'health.db'}", settings=application.state.settings, initialize=True)
    with TestClient(app) as connection:
        response = connection.get("/health")
    assert response.status_code == 200 and not response.json()["paid_access_configured"]
    assert str(tmp_path) not in response.text


def test_storage_failures_return_sanitized_unknown_outcome(client, application):
    @application.get("/api/test-storage-error")
    def fail_storage():
        raise OSError(errno.ENOENT, "PRIVATE_STORAGE_LOCATION")
    response = client.get("/api/test-storage-error")
    assert response.status_code == 503 and response.json()["error"]["command_outcome"] == "unknown"
    assert "PRIVATE_STORAGE_LOCATION" not in response.text


def request_for(app, method="GET", path="/api/meetings", host="127.0.0.1"):
    return SimpleNamespace(app=app, method=method, url=SimpleNamespace(path=path), client=SimpleNamespace(host=host))


def test_auth_fails_closed_if_hosted_identity_is_absent_or_local_host_is_remote(application):
    with pytest.raises(HTTPException) as failure:
        auth.require_owner(request_for(application, host="203.0.113.7"))
    assert failure.value.status_code == 403
    application.state.settings = replace(application.state.settings, development=False)
    with pytest.raises(HTTPException) as failure:
        auth.require_owner(request_for(application))
    assert failure.value.status_code == 503


def test_db_dependency_rechecks_durability_marker_after_authentication(application, monkeypatch):
    from round_robin.operations import durability
    request = request_for(application, method="PATCH")
    assert auth.require_owner(request) == "owner_local"
    monkeypatch.setattr(durability, "marker_pending", lambda *args, **kwargs: True)
    with pytest.raises(HTTPException) as failure:
        next(auth.get_db(request))
    assert failure.value.detail["code"] == "durability_compromised"


@pytest.mark.parametrize("path", ["/auth/login", "/auth/callback"])
def test_hosted_login_routes_are_closed_in_local_mode(client, path):
    assert client.get(path).status_code == 404
    assert client.get("/auth/session").json()["csrf_token"] is None


def test_invalid_oidc_callback_is_sanitized(hosted):
    from authlib.integrations.base_client.errors import OAuthError
    async def reject(*args, **kwargs):
        raise OAuthError("PRIVATE-OIDC-state")
    hosted.state.oidc = SimpleNamespace(authorize_access_token=reject)
    with TestClient(hosted, base_url="https://app.example") as client:
        response = client.get("/auth/callback")
    assert response.status_code == 400 and "PRIVATE-OIDC-state" not in response.text


@pytest.mark.parametrize("cursor", ["foreign_run:1", "run:-1", "run:9223372036854775808", "run:nan", "malformed"])
def test_sse_rejects_hostile_cursor_before_streaming(client, cursor):
    meeting = create(client)
    run = start(client, meeting).json()
    cursor = cursor.replace("run:", run["id"] + ":") if cursor.startswith("run:") else cursor
    response = client.get(f"/api/runs/{run['id']}/stream", headers={"Last-Event-ID": cursor})
    assert response.status_code == 400


@pytest.mark.parametrize("mode", ["already_disconnected", "revoked", "owner_changed", "heartbeat", "between_events"])
def test_sse_closes_on_revocation_and_disconnect_without_private_frames(client, application, monkeypatch, mode):
    meeting = create(client)
    run = start(client, meeting).json()
    async def exercise():
        checks = 0
        async def disconnected():
            nonlocal checks
            checks += 1
            return mode == "already_disconnected" or mode == "between_events" and checks >= 2 or mode == "heartbeat" and checks >= 2
        async def sleep(_):
            return None
        monkeypatch.setattr(events.asyncio, "sleep", sleep)
        request = SimpleNamespace(app=application, is_disconnected=disconnected)
        if mode in {"revoked", "owner_changed"}:
            def identity(_):
                if mode == "revoked":
                    raise HTTPException(401)
                return "owner_intruder"
            application.state.identity_provider = identity
        response = await events.stream_events(run["id"], request, after=1 if mode == "heartbeat" else 0, last_event_id=None, owner="owner_local")
        content = "".join([value async for value in response.body_iterator])
        assert content == (": heartbeat\n\n" if mode == "heartbeat" else "")
    asyncio.run(exercise())


def test_remaining_active_allowance_never_renews_on_clock_regression(client, application):
    meeting = create(client)
    run = start(client, meeting).json()
    with application.state.session_factory() as session:
        row = session.get(Run, run["id"])
        row.active_seconds = 35
        row.active_started_at = utcnow() + timedelta(seconds=100)
        assert events.run_view_remaining_active(row) == 565
        row.active_started_at = None
        row.active_seconds = 900
        assert events.run_view_remaining_active(row) == 0
        row.configuration = {**row.configuration, "active_clock_invalid": True}
        assert events.run_view_remaining_active(row) == 0
        assert not events.retry_eligibility(session, row)


def test_contract_export_detects_changed_or_missing_artifacts(tmp_path, capsys):
    from round_robin.export_contracts import check, export
    export(tmp_path)
    assert check(tmp_path)
    (tmp_path / "brief.schema.json").write_text("{}")
    (tmp_path / "events.schema.json").unlink()
    assert not check(tmp_path)
    output = capsys.readouterr().err
    assert "brief.schema.json" in output and "events.schema.json" in output


def test_migration_offline_sql_is_complete_and_honors_explicit_configuration(monkeypatch):
    config = Config("alembic.ini", output_buffer=StringIO())
    monkeypatch.setenv("ROUND_ROBIN_DATABASE_URL", "postgresql+psycopg://operator@127.0.0.1/offline_test")
    migration.upgrade(config, "head", sql=True)
    sql = config.output_buffer.getvalue()
    assert "CREATE TABLE owners" in sql and "ADD COLUMN configuration" in sql
    assert "INSERT INTO alembic_version" in sql
    config = Config(output_buffer=StringIO())
    config.set_main_option("script_location", "backend/migrations")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://operator@127.0.0.1/offline_test")
    monkeypatch.delenv("ROUND_ROBIN_DATABASE_URL")
    migration.upgrade(config, "head", sql=True)
    assert "CREATE TABLE runs" in config.output_buffer.getvalue()
