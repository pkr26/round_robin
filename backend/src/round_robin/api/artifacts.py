"""Private evidence, bounded text uploads and exact saved-report exports."""

from datetime import timedelta
import asyncio
import hashlib
import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from round_robin.database.models import Command, Draft, Event, EvidenceRecord, ExportRecord, Meeting, PolicyBucket, ReportVersion, Run, Upload, utcnow
from round_robin.database.services import lock_policies, new_id, owner_lock
from round_robin.reports.exports import ExportFailure, UnsupportedPdfText, render_saved_report
from round_robin.reports.read_adapters import UnreadableReport, read_stored_evidence
from round_robin.reports.storage import ArtifactStore, StorageFailure
from round_robin.schemas.common import Contract, Id
from .auth import get_db, require_owner
from .commands import aware, command_begin, command_commit, fail, owned_meeting

router = APIRouter()
MAX_FILE = 1024 * 1024
MAX_MEETING = 5 * MAX_FILE
MAX_OWNER = 100 * MAX_FILE


@router.get('/api/runs/{run_id}/measurements')
def measurements(run_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    from round_robin.reports.measurements import measure_run
    run = owned_run(session, run_id, owner)
    events = [{"sequence": event.sequence, "event_type": event.event_type, "payload": event.payload, "timestamp": event.created_at} for event in session.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.sequence))]
    return measure_run(run, events)


class ExportRequest(Contract):
    command_id: UUID
    report_id: Id
    format: Literal["json", "markdown", "pdf"]


class ExportResult(Contract):
    id: Id
    status: Literal["pending", "ready", "failed", "purged"]
    report_id: Id
    report_version: int
    format: Literal["json", "markdown", "pdf"]
    download_url: str | None
    error: str | None = None


def owned_run(session: Session, run_id: str, owner: str) -> Run:
    run = session.get(Run, run_id)
    if not run or run.owner_id != owner:
        fail("not_found", "Run not found", 404)
    owned_meeting(session, run.meeting_id, owner)
    return run


def _export_available(row: ExportRecord) -> bool:
    return bool(row.status == "ready" and row.path and row.sha256 and row.created_at is not None
                and aware(row.created_at) + timedelta(days=7) >= utcnow())


def export_view(row: ExportRecord, report: ReportVersion) -> dict:
    return {"id": row.id, "status": row.status, "report_id": report.id, "report_version": report.version, "format": row.format, "download_url": f"/api/exports/{row.id}/download" if _export_available(row) else None, "error": row.error_code}


@router.get("/api/runs/{run_id}/sources")
def sources(run_id: str, limit: int = 24, cursor: str | None = None, owner=Depends(require_owner), session: Session = Depends(get_db)):
    run = owned_run(session, run_id, owner)
    query = select(EvidenceRecord).where(EvidenceRecord.run_id == run_id, EvidenceRecord.owner_id == owner)
    if cursor:
        query = query.where(EvidenceRecord.id > cursor)
    limit = min(24, max(1, limit))
    try:
        rows = list(session.scalars(query.order_by(EvidenceRecord.id).limit(limit + 1)))
    except (ValueError, RecursionError):
        raise UnreadableReport("Stored evidence could not be decoded") from None
    return {"items": [read_stored_evidence(row, run_id=run.id, owner_id=owner, meeting_id=run.meeting_id) for row in rows[:limit]], "next_cursor": rows[limit - 1].id if len(rows) > limit else None}


@router.get("/api/runs/{run_id}/sources/{source_id}")
def read_source(run_id: str, source_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    run = owned_run(session, run_id, owner)
    try:
        row = session.scalar(select(EvidenceRecord).where(EvidenceRecord.id == source_id, EvidenceRecord.run_id == run_id, EvidenceRecord.owner_id == owner))
    except (ValueError, RecursionError):
        raise UnreadableReport("Stored evidence could not be decoded") from None
    if row is None:
        fail("not_found", "Stored evidence not found in this run", 404)
    return read_stored_evidence(row, run_id=run.id, owner_id=owner, meeting_id=run.meeting_id)


@router.get("/api/runs/{run_id}/calculations/{calculation_id}")
def read_calculation(run_id: str, calculation_id: str, report_id: str | None = None, owner=Depends(require_owner), session: Session = Depends(get_db)):
    run = owned_run(session, run_id, owner)
    from .events import ledger_unavailable
    if not report_id and run.configuration.get("saved_state_unavailable") is True:
        fail("saved_state_integrity_failed", "The current report is unavailable because saved state could not be verified.", 503, "read")
    if not report_id and ledger_unavailable(session, run):
        fail("ledger_integrity_failed", "The current report is unavailable because saved accounting records could not be verified.", 503, "read")
    query = select(ReportVersion).where(ReportVersion.run_id == run_id)
    if report_id:
        query = query.where(ReportVersion.id == report_id)
    try:
        row = session.scalar(query.order_by(ReportVersion.version.desc()).limit(1))
    except (ValueError, RecursionError):
        fail("report_invalid", "The saved report failed its version or reference checks", 409, "read")
    if row:
        from round_robin.reports.read_adapters import read_runtime_report
        try:
            report = read_runtime_report(session, row)
        except ValueError:
            fail("report_invalid", "The saved report failed its version or reference checks", 409, "read")
        for calculation in report.get("calculations", []):
            if calculation.get("id") == calculation_id:
                return {**calculation, "report_id": row.id, "report_version": row.version}
    fail("not_found", "Saved calculation not found in this report version", 404)


def upload_view(row: Upload) -> dict:
    return {"id": row.id, "meeting_id": row.meeting_id, "status": row.status, "filename": row.filename, "sha256": row.sha256, "byte_size": row.byte_size, "recipients": row.recipients}


@router.get("/api/meetings/{meeting_id}/uploads")
def uploads(meeting_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    owned_meeting(session, meeting_id, owner)
    return {"items": [upload_view(row) for row in session.scalars(select(Upload).where(Upload.meeting_id == meeting_id, Upload.owner_id == owner, Upload.status != "purged").order_by(Upload.created_at))]}


@router.post("/api/meetings/{meeting_id}/uploads", status_code=201)
async def upload_text(meeting_id: str, request: Request, x_command_id: UUID = Header(), x_expected_revision: int = Header(), x_filename: str = Header(), x_content_sha256: str = Header(), owner=Depends(require_owner), session: Session = Depends(get_db)):
    if request.headers.get("content-type", "").split(";")[0] not in {"text/plain", "text/markdown"}:
        fail("unsupported_media", "Upload UTF-8 text or Markdown only", 415)
    if not re.fullmatch(r"[a-f0-9]{64}", x_content_sha256) or len(x_filename) > 200 or not x_filename.strip() or any(ord(c) < 32 for c in x_filename):
        fail("invalid_upload", "Provide a bounded filename and canonical UTF-8 SHA-256", 422)
    try:
        declared_size = int(request.headers.get("content-length", "0"))
    except ValueError:
        fail("invalid_upload", "A valid Content-Length is required", 411)
    if not 0 < declared_size <= MAX_FILE:
        fail("upload_size", "One text upload must contain at most one MiB", 413)
    kind = "upload_text"
    command_body = {"meeting_id": meeting_id, "expected_revision": x_expected_revision, "filename": x_filename, "sha256": x_content_sha256, "byte_size": declared_size}
    with session.begin():
        prior = command_begin(session, request, owner, x_command_id, kind, command_body)
        if prior:
            return prior.result
        owned_meeting(session, meeting_id, owner, lock=True)
        draft = session.get(Draft, meeting_id)
        if draft.revision != x_expected_revision:
            fail("draft_conflict", "Draft changed before upload reservation")
        rows = list(session.scalars(select(Upload).where(Upload.owner_id == owner, Upload.status.in_(["temporary", "ready"]))))
        meeting_rows = [row for row in rows if row.meeting_id == meeting_id]
        if len(meeting_rows) >= 5 or sum(row.byte_size for row in meeting_rows) + declared_size > MAX_MEETING or sum(row.byte_size for row in rows) + declared_size > MAX_OWNER:
            fail("quota_exceeded", "Text attachment storage limit reached", 429)
        row = Upload(id=new_id("upload"), meeting_id=meeting_id, owner_id=owner, filename=x_filename, media_type=request.headers["content-type"].split(";")[0], byte_size=declared_size, status="temporary")
        session.add(row)
        session.flush()
        result = upload_view(row)
        command_commit(session, request, owner, x_command_id, kind, result, resource_id=row.id, meeting_id=meeting_id, status_code=201)
        upload_id = row.id
    chunks = bytearray()
    failure = None
    text = None
    try:
        async with asyncio.timeout(20):
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > MAX_FILE:
                    raise ValueError("size")
        if len(chunks) != declared_size:
            raise ValueError("size")
        # These are the documented canonical bytes: strict UTF-8; line endings LF.
        text = bytes(chunks).decode("utf-8", errors="strict").replace("\r\n", "\n").replace("\r", "\n")
        if "\x00" in text or not text.strip():
            raise ValueError("text")
        canonical = text.encode()
        if hashlib.sha256(canonical).hexdigest() != x_content_sha256:
            raise ValueError("hash")
    except (ValueError, UnicodeError, TimeoutError, ClientDisconnect):
        failure = "upload_validation_failed"
    with session.begin():
        owner_lock(session, owner)
        row = session.get(Upload, upload_id, populate_existing=True)
        meeting = session.get(Meeting, meeting_id, populate_existing=True)
        if not meeting or meeting.deleted_at:
            row.status, row.canonical_text, row.byte_size = "purged", None, 0
            failure = "command_resource_gone"
        elif row.status != 'temporary':
            row.canonical_text, row.byte_size = None, 0
            failure = 'upload_reservation_expired'
        elif failure:
            row.status, row.canonical_text, row.byte_size = "failed", None, 0
        else:
            row.status, row.canonical_text, row.sha256, row.byte_size = "ready", text, hashlib.sha256(canonical).hexdigest(), len(canonical)
        result = upload_view(row)
        command = session.get(Command, (owner, str(x_command_id)), populate_existing=True)
        if not command.gone:
            command.result = result
    if failure:
        fail(failure, "Upload is unusable; validation failed or the meeting was deleted", 422 if failure != "command_resource_gone" else 410, "new_upload")
    return result


@router.post("/api/runs/{run_id}/exports", response_model=ExportResult)
def create_export(run_id: str, body: ExportRequest, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "create_export", {"run_id": run_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        # This capacity is deployment-wide. Owner locks alone allow different
        # accounts to reserve the final renderer slot at the same time.
        lock_policies(session, owner)
        run = owned_run(session, run_id, owner)
        try:
            report = session.scalar(select(ReportVersion).where(ReportVersion.id == body.report_id, ReportVersion.run_id == run_id))
        except (ValueError, RecursionError):
            fail("report_invalid", "The saved report failed its version or reference checks; no export was created", 409, "read")
        if report is None:
            fail("report_pending", "The requested saved report version does not exist", 409)
        from round_robin.reports.read_adapters import read_runtime_report
        try:
            report_payload = read_runtime_report(session, report)
        except ValueError:
            fail('report_invalid', 'The saved report failed its version or reference checks; no export was created', 409, 'read')
        pending = list(session.scalars(select(ExportRecord).where(ExportRecord.status == "pending")))
        if len(pending) >= 5 or any(item.owner_id == owner for item in pending):
            fail("export_capacity", "An export is already running; retry the export later", 429, "export")
        exports = list(session.scalars(select(ExportRecord).where(ExportRecord.owner_id == owner, ExportRecord.status == "ready")))
        store = ArtifactStore(request.app.state.settings.artifact_directory)
        stored_bytes = 0
        for item in exports:
            try:
                stored_bytes += store._path(item.path).stat().st_size if item.path else 0
            except (OSError, StorageFailure):
                fail("storage_unavailable", "Saved artifact storage could not be inspected", 503, "export")
        if stored_bytes + 10 * MAX_FILE > MAX_OWNER:
            fail("quota_exceeded", "Export storage is full; explicitly clear expired artifacts first", 429)
        row = ExportRecord(id=new_id("export"), run_id=run_id, owner_id=owner, report_version=report.version, format=body.format, status="pending")
        session.add(row)
        session.flush()
        result = export_view(row, report)
        command_commit(session, request, owner, body.command_id, "create_export", result, resource_id=row.id, meeting_id=run.meeting_id)
        export_id, report_identity = row.id, report.id
        title = run.input_snapshot.get("brief", run.input_snapshot).get("title", "Decision comparison")
        source_records = [read_stored_evidence(source, run_id=run.id, owner_id=owner, meeting_id=run.meeting_id) for source in session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id, EvidenceRecord.owner_id == owner, EvidenceRecord.id.in_(report_payload['evidence_ids']), EvidenceRecord.created_at <= report.created_at))]
        meeting_id, accepted_fence = run.meeting_id, run.execution_fence
    artifact_key = None
    error = None
    try:
        content, _ = render_saved_report(report_payload, body.format, title=title, sources=source_records)
        artifact_key, digest = store.put(content)
    except OSError:
        from round_robin.operations.durability import mark_failure
        mark_failure(request.app.state.session_factory, "artifact_storage_failed", artifact_directory=request.app.state.settings.artifact_directory, control_directory=request.app.state.settings.durability_control_directory)
        error = "export_storage_failed"
    except UnsupportedPdfText:
        error = "pdf_font_unsupported"
    except Exception:
        error = "export_render_failed"
    committed = False
    try:
        with session.begin():
            owner_lock(session, owner)
            row = session.get(ExportRecord, export_id, populate_existing=True)
            run = session.get(Run, run_id, populate_existing=True)
            meeting = session.get(Meeting, meeting_id, populate_existing=True)
            if not meeting or meeting.deleted_at or run.execution_fence != accepted_fence:
                error = "meeting_deleted_or_cancelled"
            elif row.status != "pending":
                error = "export_reservation_expired"
            if error:
                row.status, row.error_code = "failed", error
                if artifact_key:
                    store.purge(artifact_key)
                    artifact_key = None
            else:
                row.status, row.path, row.sha256 = "ready", artifact_key, digest
            # Deletion can remove the saved report while the renderer finishes.
            result = {"id": row.id, "status": row.status, "report_id": report_identity, "report_version": row.report_version, "format": row.format, "download_url": f"/api/exports/{row.id}/download" if _export_available(row) else None, "error": row.error_code}
            command = session.get(Command, (owner, str(body.command_id)), populate_existing=True)
            if command and not command.gone:
                command.result = result
        committed = True
    finally:
        # The renderer's opaque key is unique to this attempt. A failed DB
        # finalization must not leave private bytes outside retention tracking.
        # An ambiguous commit may need a new export; the saved report is intact.
        if not committed and artifact_key:
            store.purge(artifact_key)
    return result


@router.get("/api/exports/{export_id}", response_model=ExportResult)
def export_status(export_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    row = session.get(ExportRecord, export_id)
    if not row or row.owner_id != owner:
        fail("not_found", "Export not found", 404)
    owned_run(session, row.run_id, owner)
    report = session.execute(select(ReportVersion.id, ReportVersion.version).where(ReportVersion.run_id == row.run_id, ReportVersion.version == row.report_version)).first()
    if not report:
        fail("report_unavailable", "Saved report is unavailable", 410)
    return export_view(row, report)


@router.get("/api/exports/{export_id}/download")
def download_export(export_id: str, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    row = session.get(ExportRecord, export_id)
    if not row or row.owner_id != owner:
        fail("not_found", "Export not found", 404)
    run = owned_run(session, row.run_id, owner)
    if not _export_available(row):
        fail("export_unavailable", "Export is not ready or has expired; create a new export from the saved report", 409, "export")
    try:
        content = ArtifactStore(request.app.state.settings.artifact_directory).read(row.path, row.sha256)
    except (OSError, StorageFailure):
        fail("storage_unavailable", "Artifact integrity or storage check failed; retry only the export", 503, "export")
    meeting_id, run_id, fence = run.meeting_id, run.id, run.execution_fence
    media = {"pdf": "application/pdf", "json": "application/json", "markdown": "text/markdown; charset=utf-8"}[row.format]
    suffix = {"pdf": "pdf", "json": "json", "markdown": "md"}[row.format]
    filename = f"round-robin-{run.id}-v{row.report_version}.{suffix}"
    # All response metadata and bytes are materialized. Release the initial
    # connection before streaming needs its own short authorization sessions.
    session.close()

    def chunks():
        for position in range(0, len(content), 16384):
            identity = getattr(request.app.state, "identity_provider", None)
            if identity is not None:
                try:
                    if identity(request) != owner:
                        return
                except Exception:
                    return
            with request.app.state.session_factory() as check:
                deployment = check.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
                if deployment and deployment.metadata_json.get('restore_quarantine'):
                    return
                active = check.get(Meeting, meeting_id)
                current = check.get(Run, run_id)
                if not active or active.deleted_at or active.owner_id != owner or not current or current.execution_fence != fence:
                    return
            yield content[position:position + 16384]

    return StreamingResponse(chunks(), media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
