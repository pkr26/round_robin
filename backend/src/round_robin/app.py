"""Durable local API. Database migrations and separate workers are required."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .configuration import Settings
from .database.models import Base
from .database.session import configure_database
from .fixtures import SampleBundle, sample_bundle


class JsonBodyLimit:
    timeout_seconds = 20

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PATCH", "PUT", "DELETE"} or scope["path"].endswith(("/attachments", "/uploads", "/images")):
            return await self.app(scope, receive, send)
        parts, total = [], 0
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    part = message.get("body", b"")
                    total += len(part)
                    if total > 128 * 1024:
                        response = JSONResponse({"error": {"code": "request_too_large", "message": "JSON requests are limited to 128 KiB", "retry_scope": "none", "command_outcome": "not_committed"}}, status_code=413)
                        return await response(scope, receive, send)
                    parts.append(part)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            response = JSONResponse({"error": {"code": "request_timeout", "message": "Request body did not complete within twenty seconds", "retry_scope": "original_command", "command_outcome": "not_committed"}}, status_code=408)
            return await response(scope, receive, send)
        delivered = False

        async def buffered():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": b"".join(parts), "more_body": False}
        return await self.app(scope, buffered, send)


def _record_manifest(path: Path, meeting_id: str, owner: str, fence: int):
    import errno
    import fcntl
    import stat
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(errno.EINVAL, "Deletion manifest must be a regular private file")
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps({"meeting_id": meeting_id, "owner_id": owner, "execution_fence": fence}, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def create_app(database_url: str | None = None, *, settings: Settings | None = None, initialize: bool = False) -> FastAPI:
    from .security.logging import install_access_filter
    install_access_filter()
    configuration = settings or Settings()
    configuration.validate()
    from .database import claim_models, visual_models  # Register tables before metadata creation or backup inspection.
    url = database_url or configuration.database_url
    if initialize and not url.startswith("sqlite"):
        raise ValueError("Production PostgreSQL schema creation requires Alembic migrations")
    engine, session_factory = configure_database(url)
    if initialize:
        Base.metadata.create_all(engine)
    application = FastAPI(title="Round Robin", version="0.2.0", description="Durable owner-scoped project comparisons. Simulated mode is explicitly labeled; paid work is disabled until funded configuration is validated.")
    session_factory.configure(info={"artifact_directory": configuration.artifact_directory, "durability_control_directory": configuration.durability_control_directory})
    application.state.settings = configuration
    application.state.engine = engine
    application.state.session_factory = session_factory
    application.state.record_deletion_manifest = lambda meeting, owner, fence: _record_manifest(configuration.artifact_directory.parent / "deletion-manifest.jsonl", meeting, owner, fence)
    if not configuration.development:
        from .security.hosted import HostedIdentity, OpaqueLoginState, build_oidc
        application.state.identity_provider = HostedIdentity(session_factory, configuration)
        application.state.oidc = build_oidc(configuration)
        application.add_middleware(OpaqueLoginState, session_factory=session_factory)
    application.add_middleware(JsonBodyLimit)
    hosts = ["localhost", "127.0.0.1", "[::1]", "testserver"] if configuration.development else [urlsplit(configuration.public_origin).hostname]
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    application.add_middleware(CORSMiddleware, allow_origins=list(configuration.allowed_origins) if configuration.development else [configuration.public_origin], allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Last-Event-ID", "X-CSRF-Token", "X-Command-Id", "X-Expected-Revision", "X-Filename", "X-Content-SHA256"])

    @application.middleware("http")
    async def local_boundary(request: Request, call_next):
        if configuration.development:
            if request.headers.get("forwarded") or any(request.headers.get(name) for name in ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")):
                return JSONResponse({"error": {"code": "proxy_exposure_disabled", "message": "Development identity rejects external proxy exposure"}}, status_code=403)
            if request.headers.get("origin") and request.headers["origin"] not in configuration.allowed_origins:
                return JSONResponse({"error": {"code": "origin_invalid", "message": "Origin is not allowed"}}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not configuration.development:
            # Browsers enforce multiple policies together. Appending preserves
            # the stricter sandbox/default-src policy of native attachments.
            policy = "default-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            if (response.headers.get("content-type", "").split(";")[0] == "image/svg+xml"
                    and response.headers.get("content-disposition", "").startswith("attachment;")
                    and request.url.path.startswith("/api/design-versions/") and request.url.path.endswith("/source.svg")):
                # Generated diagrams carry the same trusted font as their
                # validated raster. The route's stricter sandbox/default-none
                # policy still intersects this policy and blocks other sources.
                policy += "; style-src 'unsafe-inline'; font-src data:"
            response.headers.append("Content-Security-Policy", policy)
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exception: HTTPException):
        detail = exception.detail if isinstance(exception.detail, dict) else {"code": "request_failed", "message": str(exception.detail)}
        return JSONResponse({"error": detail, "detail": detail}, status_code=exception.status_code, headers=exception.headers)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exception: RequestValidationError):
        return JSONResponse({"error": {"code": "validation_error", "message": "Request fields are invalid", "retry_scope": "none", "command_outcome": "not_committed", "fields": [{"location": list(error["loc"]), "message": error["msg"]} for error in exception.errors()]}}, status_code=422)

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exception: SQLAlchemyError):
        return JSONResponse({"error": {"code": "database_unavailable", "message": "Database operation could not be confirmed; reconcile the original command UUID before retrying", "retry_scope": "original_command", "command_outcome": "unknown"}}, status_code=503)

    from .reports.read_adapters import UnreadableReport

    @application.exception_handler(UnreadableReport)
    @application.exception_handler(ResponseValidationError)
    async def unreadable_saved_result(request: Request, exception):
        # Response validation errors contain private report/source inputs. Do
        # not let ASGI traceback logging serialize those inputs or send them.
        return JSONResponse({"error": {"code": "saved_result_unavailable", "message": "This saved response could not be safely read; its original record is preserved for operator review", "retry_scope": "read", "command_outcome": "unknown"}}, status_code=503)

    @application.exception_handler(OSError)
    async def storage_failure(request: Request, exception: OSError):
        import errno
        if exception.errno in {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS, errno.EACCES, errno.EPERM}:
            from .operations.durability import mark_failure
            mark_failure(session_factory, "artifact_storage_failed", artifact_directory=configuration.artifact_directory, control_directory=configuration.durability_control_directory)
        return JSONResponse({"error": {"code": "storage_unavailable", "message": "Storage could not be confirmed; reconcile the original action before retrying", "retry_scope": "original_command", "command_outcome": "unknown"}}, status_code=503)

    from .api import artifacts, assessments, commands, drafts, events, hosted, preflight, reuse, visual
    for router in (commands.router, drafts.router, events.router, preflight.router, artifacts.router, hosted.router, reuse.router, visual.router, assessments.router):
        application.include_router(router)

    @application.get("/api/sample", response_model=SampleBundle)
    def read_sample():
        return sample_bundle()

    @application.get("/health")
    async def health():
        configured = False
        if configuration.provider_config_file:
            try:
                result = await run_in_threadpool(preflight.run_configuration, configuration, "live")
                configured = bool(result.get("funded"))
            except HTTPException:
                pass
        return {"status": "ok", "mode": configuration.execution_mode, "paid_access_configured": configured, "authorization": "Every paid attempt requires current policy, capacity and budget checks"}

    @application.get("/ready")
    def readiness():
        from .operations.durability import marker_pending
        if marker_pending(configuration.artifact_directory, control_directory=configuration.durability_control_directory):
            return JSONResponse({"status": "not_ready", "reason": "durability_compromised"}, status_code=503)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                if engine.dialect.name != "sqlite":
                    from .operations.recovery import schema_head
                    if connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() != schema_head():
                        return JSONResponse({"status": "not_ready", "reason": "schema_incompatible"}, status_code=503)
                from .database.models import PolicyBucket
                from sqlalchemy import select
                policy = connection.execute(select(PolicyBucket.metadata_json).where(PolicyBucket.key == "deployment")).scalar_one_or_none()
                if policy and policy.get("durability_compromised"):
                    return JSONResponse({"status": "not_ready", "reason": "durability_compromised"}, status_code=503)
                if policy and (policy.get("restore_quarantine") or policy.get("dispatch_locked")):
                    return JSONResponse({"status": "not_ready", "reason": "restoration_or_dispatch_quarantine"}, status_code=503)
        except Exception:
            return JSONResponse({"status": "not_ready", "reason": "database_or_migrations_unavailable"}, status_code=503)
        return {"status": "ready", "schema_version": configuration.schema_version}

    return application


app = create_app()
