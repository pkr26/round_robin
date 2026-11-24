from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import DateTime, create_engine, delete, inspect, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from round_robin.database.models import Attempt, Base, BriefRevision, BudgetOperation, Command, DeletionRecord, Draft, Event, EvidenceRecord, ExportRecord, Meeting, Outcome, Outbox, PolicyBucket, PreflightRecord, ReportVersion, Run, Step, Upload, utcnow
from round_robin.database import visual_models  # Operator CLI must include optional persisted tables too.
from round_robin.database.claim_models import ClaimVersion, ReuseRecord
from round_robin.database.services import lock_policies, new_id
from round_robin.reports.storage import ArtifactStore
from .artifact_lifecycle import artifact_lifecycle, hold_artifact_lifecycle


class OperationError(ValueError):
    pass


MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024


def alembic_config(url: str | None = None):
    path = Path(__file__).resolve().parents[4] / "alembic.ini"
    config = Config(str(path))
    config.set_main_option("script_location", str(path.parent / "backend/migrations"))
    if url:
        config.attributes["url_override"] = url
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def schema_head() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _manifest_snapshot(path: Path) -> tuple[bytes, list[dict]]:
    if not path.is_file() or path.is_symlink():
        raise OperationError("The complete independently retained deletion manifest is required")
    # Match the append writer's file lock. Backup must validate and archive the
    # same bytes, rather than racing a second read against a partial append.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OperationError("Deletion manifest must be a regular file")
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        content = handle.read()
    records = []
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise OperationError("Deletion manifest is corrupt or incomplete; restoration remains closed") from exc
    for line in lines:
        if not line:
            continue
        records.append(_manifest_record(line))
    return content, records


def _manifest_record(line):
    try:
        record = json.loads(line)
        if set(record) != {"meeting_id", "owner_id", "execution_fence"} or not all(re.fullmatch(r"[a-z][a-z0-9_:-]{2,95}", record[key]) for key in ("meeting_id", "owner_id")) or type(record["execution_fence"]) is not int or record["execution_fence"] < 1:
            raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise OperationError("Deletion manifest is corrupt or incomplete; restoration remains closed") from exc
    return record


def _manifest_batch(path, cursor, limit):
    """Read a bounded append-only suffix; retain the full manifest for restore."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise OperationError("Deletion manifest must be a regular file")
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        identity = [metadata.st_dev, metadata.st_ino]
        cursor = cursor if isinstance(cursor, dict) else {}
        offset = cursor.get("offset", 0)
        if cursor.get("identity") != identity or type(offset) is not int or not 0 <= offset <= metadata.st_size:
            offset = 0
        if offset:
            handle.seek(max(0, offset - 256))
            if hashlib.sha256(handle.read(min(offset, 256))).hexdigest() != cursor.get("anchor"):
                offset = 0
        handle.seek(offset)
        records = []
        for _ in range(limit):
            line = handle.readline(1025)
            if not line:
                break
            if len(line) > 1024 or not line.endswith(b"\n"):
                raise OperationError("Deletion manifest contains an incomplete or oversized record")
            if line.strip():
                records.append(_manifest_record(line))
        offset = handle.tell()
        handle.seek(max(0, offset - 256))
        anchor = hashlib.sha256(handle.read(min(offset, 256))).hexdigest()
        return records, {"identity": identity, "offset": offset, "anchor": anchor}


def read_manifest(path: Path) -> list[dict]:
    return _manifest_snapshot(path)[1]


def _validated_deletion_meeting(session, meeting_id, owner_id):
    meeting = session.get(Meeting, meeting_id)
    if meeting and meeting.owner_id != owner_id:
        raise OperationError("Deletion manifest ownership does not match restored records")
    from round_robin.operations.deletion_scope import deletion_scope_consistent
    if not deletion_scope_consistent(session, meeting_id, owner_id):
        raise OperationError("Deletion manifest cannot be applied to inconsistent saved ownership")
    return meeting


def purge_record(session, meeting_id: str, owner_id: str, fence: int, store: ArtifactStore | None = None):
    if store:
        lock_policies(session, "owner_operator")
        hold_artifact_lifecycle(session, store.directory)
    meeting = _validated_deletion_meeting(session, meeting_id, owner_id)
    if meeting:
        meeting.deleted_at, meeting.title = meeting.deleted_at or utcnow(), ""
        meeting.execution_fence = max(meeting.execution_fence, fence)
    runs = list(session.scalars(select(Run).where(Run.meeting_id == meeting_id)))
    if store:
        from round_robin.visual.service import purge_designs
        purge_designs(session, meeting_id, store.directory)
    run_ids = [run.id for run in runs]
    for run in runs:
        from round_robin.orchestration.budget import release_unused_finish
        release_unused_finish(session, run)
        run.execution_fence = max(run.execution_fence + 1, fence)
        run.status, run.stop_reason = "cancelled", "meeting_deleted"
        run.input_snapshot, run.configuration = {}, {"deleted": True}
        run.active_started_at = None
    for model in (Event, EvidenceRecord, ReportVersion, BriefRevision, ClaimVersion, ReuseRecord):
        session.execute(delete(model).where(model.run_id.in_(run_ids)))
    for model in (Draft, Outcome):
        session.execute(delete(model).where(model.meeting_id == meeting_id))
    for upload in session.scalars(select(Upload).where(Upload.meeting_id == meeting_id)):
        upload.canonical_text, upload.filename, upload.status = None, "", "purged"
    for row in session.scalars(select(PreflightRecord).where(PreflightRecord.meeting_id == meeting_id)):
        row.payload = {}
    for step in session.scalars(select(Step).where(Step.run_id.in_(run_ids))):
        step.result_json = None
    for attempt in session.scalars(select(Attempt).where(Attempt.run_id.in_(run_ids))):
        attempt.request_json, attempt.response_json, attempt.configuration = {}, None, {}
    for work in session.scalars(select(Outbox).where(Outbox.run_id.in_(run_ids))):
        work.status, work.payload = "cancelled", {}
    for artifact in session.scalars(select(ExportRecord).where(ExportRecord.run_id.in_(run_ids))):
        if artifact.path and store:
            store.purge(artifact.path)
        artifact.path, artifact.status = None, "purged"
    session.execute(update(Command).where(Command.owner_id == owner_id, Command.meeting_id == meeting_id).values(gone=True, result={}))
    deletion = session.get(DeletionRecord, meeting_id)
    if deletion is None:
        deletion = DeletionRecord(meeting_id=meeting_id, owner_id=owner_id, execution_fence=fence)
        session.add(deletion)
    else:
        deletion.execution_fence = max(deletion.execution_fence, fence)
    deletion.status, deletion.purged_at = "purged", utcnow()


def purge_pending(session_factory, artifact_directory: Path, manifest_path: Path, *, limit: int | None = None) -> dict:
    if limit is not None and not 1 <= limit <= 100:
        raise ValueError("Routine deletion batches must contain between one and one hundred records")
    records = read_manifest(manifest_path) if limit is None else None
    store = ArtifactStore(artifact_directory)
    applied = 0
    with session_factory.begin() as session:
        policy, _ = lock_policies(session, "owner_operator")
        hold_artifact_lifecycle(session, store.directory)
        if limit is not None:
            records, cursor = _manifest_batch(manifest_path, policy.metadata_json.get("deletion_manifest_cursor"), limit)
        # Validate every selected record before unlinking any files.
        from round_robin.operations.deletion_scope import deletion_scopes_consistent
        existing_by_id = {}
        if limit is not None and records:
            existing_by_id = {item.meeting_id: item for item in session.scalars(select(DeletionRecord).where(
                DeletionRecord.meeting_id.in_({record["meeting_id"] for record in records})))}
        selected = []
        for record in records:
            existing = existing_by_id.get(record["meeting_id"])
            if limit is not None and existing and existing.status == "purged" and existing.owner_id == record["owner_id"] and existing.execution_fence >= record["execution_fence"]:
                continue
            selected.append((record["meeting_id"], record["owner_id"]))
        if not deletion_scopes_consistent(session, selected):
            raise OperationError("Deletion manifest cannot be applied to inconsistent saved ownership")
        for record in records:
            existing = session.get(DeletionRecord, record["meeting_id"])
            if limit is not None and existing and existing.status == "purged" and existing.owner_id == record["owner_id"] and existing.execution_fence >= record["execution_fence"]:
                continue
            purge_record(session, record["meeting_id"], record["owner_id"], record["execution_fence"], store)
            applied += 1
        if limit is not None:
            policy.metadata_json = {**policy.metadata_json, "deletion_manifest_cursor": cursor}
    return {"records_reapplied": applied, "status": "active_storage_purged", "backup_erasure": "expires_under_retention_policy"}


def _private_write(path: Path, content: bytes):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _capture_backup(engine, artifact_directory, temporary, manifest_path):
    # Pin referenced files before the database snapshot; deletion keeps the
    # same guard until its database transaction ends. No DB writes in here.
    with artifact_lifecycle(artifact_directory):
        connection = engine.connect()
        if engine.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with connection, connection.begin():
            if engine.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
                # Legacy sqlite3 transaction mode does not begin a physical
                # snapshot for SELECTs. Pin one before reading related tables.
                connection.exec_driver_sql("BEGIN")
            tables = {table.name: [dict(row) for row in connection.execute(select(table)).mappings()] for table in Base.metadata.sorted_tables}
            if engine.dialect.name == "postgresql":
                actual = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                if actual != schema_head():
                    raise OperationError("Backup requires the current compatible migrated schema")
        if not manifest_path.exists() and not tables["deletion_records"]:
            manifest_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        external, _ = _manifest_snapshot(manifest_path)
        store = ArtifactStore(artifact_directory)
        (temporary / "artifacts").mkdir(mode=0o700)
        artifacts = []
        for row in tables["exports"]:
            if row["status"] != "ready" or not row["path"]:
                continue
            content = store.read(row["path"], row["sha256"])
            _private_write(temporary / "artifacts" / row["path"], content)
            artifacts.append({"key": row["path"], "sha256": row["sha256"]})
        visual_artifacts = []
        if tables.get("design_versions"):
            from round_robin.visual.service import backup_visual_assets
            visual_artifacts = backup_visual_assets(tables, artifact_directory, temporary)
        return tables, external, artifacts, visual_artifacts


def backup(engine, artifact_directory: Path, destination: Path, manifest_path: Path, *, control_directory: Path | None = None) -> dict:
    if destination.exists():
        raise OperationError("Backup destination must be new; existing backups are never overwritten")
    temporary = destination.parent / (".backup_pending_" + uuid4().hex)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary.mkdir(mode=0o700)
        tables, external, artifacts, visual_artifacts = _capture_backup(engine, artifact_directory, temporary, manifest_path)
        _private_write(temporary / "deletion-manifest.snapshot.jsonl", external)
        packet = {"format_version": 1, "schema_revision": schema_head(), "created_at": utcnow().isoformat(), "tables": tables, "artifacts": artifacts, "visual_artifacts": visual_artifacts, "deletion_snapshot_sha256": hashlib.sha256(external).hexdigest()}
        serialized = json.dumps(packet, sort_keys=True, default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value)).encode()
        if len(serialized) > MAX_SNAPSHOT_BYTES:
            raise OperationError("Backup snapshot exceeds the supported restoration size")
        _private_write(temporary / "snapshot.json", serialized)
        _private_write(temporary / "snapshot.sha256", hashlib.sha256(serialized).hexdigest().encode())
        for directory_path in (temporary / "artifacts", temporary):
            directory_fd = os.open(directory_path, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        os.rename(temporary, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        from .durability import record_backup
        record_backup(sessionmaker(engine, expire_on_commit=False), hashlib.sha256(serialized).hexdigest())
        return {"status": "backup_saved", "schema_revision": packet["schema_revision"], "tables": len(tables), "artifacts": len(artifacts), "sha256": hashlib.sha256(serialized).hexdigest()}
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        from .durability import mark_failure
        mark_failure(sessionmaker(engine, expire_on_commit=False), "backup_failed", artifact_directory=artifact_directory, control_directory=control_directory)
        raise


def restore_verify(source: Path, target_url: str, artifact_directory: Path, manifest_path: Path) -> dict:
    records = read_manifest(manifest_path)
    url = make_url(target_url)
    if url.get_backend_name() == "sqlite":
        path = Path(url.database or "")
        if "restore_test" not in path.name or path.exists() or (url.database or "").startswith("file:") or "uri" in url.query:
            raise OperationError("Synthetic restore requires a new restore_test SQLite file")
    else:
        from round_robin.database.safety import require_disposable_postgres
        try:
            require_disposable_postgres(target_url, suffix="_restore_test")
        except ValueError:
            raise OperationError("Refusing restore outside an empty loopback *_restore_test database") from None
    if artifact_directory.exists() and any(artifact_directory.iterdir()):
        raise OperationError("Restore artifact destination must be empty")
    # Bound the read itself and share the producer's size contract.
    with (source / "snapshot.json").open('rb') as handle:
        content = handle.read(MAX_SNAPSHOT_BYTES + 1)
    if len(content) > MAX_SNAPSHOT_BYTES or hashlib.sha256(content).hexdigest() != (source / "snapshot.sha256").read_text():
        raise OperationError("Backup snapshot integrity failed")
    packet = json.loads(content)
    if packet.get("format_version") != 1 or packet.get("schema_revision") != schema_head() or set(packet.get("tables", {})) != set(Base.metadata.tables):
        raise OperationError("Backup format or schema is unsupported")
    snapshot_manifest = (source / "deletion-manifest.snapshot.jsonl").read_bytes()
    if hashlib.sha256(snapshot_manifest).hexdigest() != packet["deletion_snapshot_sha256"]:
        raise OperationError("Backup deletion snapshot integrity failed")
    snapshot_records = [json.loads(line) for line in snapshot_manifest.splitlines() if line]
    if not {json.dumps(item, sort_keys=True) for item in snapshot_records} <= {json.dumps(item, sort_keys=True) for item in records}:
        raise OperationError("Current deletion manifest is incomplete relative to the backup")
    engine = create_engine(target_url, hide_parameters=True)
    try:
        if inspect(engine).get_table_names():
            raise OperationError("Restore target is not empty")
        command.upgrade(alembic_config(target_url), "head")
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        source_deployment = next((row for row in packet["tables"]["policy_buckets"] if row["key"] == "deployment"), None)
        epoch = max(int((source_deployment or {}).get("metadata_json", {}).get("deployment_epoch", 1)), max((row["deployment_epoch"] for row in packet["tables"]["runs"]), default=1)) + 1
        quarantine = {"deployment_epoch": epoch, "dispatch_locked": True, "restore_quarantine": True, "draining": True}
        # This commit precedes archived artifacts and content imports. Every failure
        # leaves an independently visible closed target, even if import never starts.
        with factory.begin() as session:
            session.add(PolicyBucket(id=new_id("policy"), key="deployment", limit_usd="0", spending_locked=True, metadata_json=quarantine))
        store = ArtifactStore(artifact_directory)
        for item in packet["artifacts"]:
            # Validate opaque key and checksum before copying any archived bytes.
            path = store._path(item["key"])
            data = (source / "artifacts" / item["key"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != item["sha256"] or len(data) > 10 * 1024 * 1024:
                raise OperationError("Archived artifact integrity failed")
            _private_write(path, data)
        if packet.get("visual_artifacts"):
            from round_robin.visual.service import restore_visual_assets
            restore_visual_assets(source, artifact_directory, packet["visual_artifacts"], packet["tables"])
        with factory.begin() as session:
            for table in Base.metadata.sorted_tables:
                normalized = []
                for row in packet["tables"][table.name]:
                    row = dict(row)
                    if table.name == "policy_buckets" and row["key"] == "deployment":
                        continue
                    for column in table.columns:
                        if isinstance(column.type, DateTime) and row.get(column.name) is not None:
                            row[column.name] = datetime.fromisoformat(row[column.name])
                    normalized.append(row)
                if normalized:
                    session.execute(table.insert(), normalized)
            deployment = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment").with_for_update())
            deployment.spending_locked = True
            deployment.version = (deployment.version or 0) + 1
            deployment.metadata_json = {**(source_deployment or {}).get("metadata_json", {}), **quarantine}
            for run in session.scalars(select(Run)):
                run.execution_fence += 1
                run.active_started_at = None
                if run.status in {"queued", "running", "awaiting_input", "cancel_requested"}:
                    run.status, run.stop_reason = "failed", "restore_quarantine"
                    from round_robin.orchestration.budget import release_unused_finish
                    release_unused_finish(session, run)
            for attempt in session.scalars(select(Attempt).where(Attempt.usage_certainty == "reserved")):
                attempt.status, attempt.usage_certainty = "unknown", "unknown"
            for operation in session.scalars(select(BudgetOperation).where(BudgetOperation.status == "reserved")):
                operation.status = "unknown"
            for row in session.scalars(select(PolicyBucket)):
                if row.metadata_json.get("kind") in {"session", "login"}:
                    row.metadata_json = {"kind": row.metadata_json["kind"], "revoked": True, "state": {}}
            for record in records:
                purge_record(session, record["meeting_id"], record["owner_id"], record["execution_fence"], store)
            from round_robin.visual.service import quarantine_restored_designs
            quarantine_restored_designs(session, epoch)
        return {"status": "restored_quarantined", "deployment_epoch": epoch, "deletion_records_reapplied": len(records), "dispatch_locked": True, "sessions_revoked": True, "provider_requests": 0}
    finally:
        engine.dispose()


def reconcile(session_factory, operation_id: str, *, conservative_usd: str | None = None, provider_receipt: Path | None = None) -> dict:
    from round_robin.orchestration.budget import amount, reconcile_operation
    with session_factory.begin() as session:
        snapshot = session.get(BudgetOperation, operation_id)
        if snapshot is None:
            raise OperationError("Usage operation not found")
        lock_policies(session, snapshot.owner_id)
        if snapshot.run_id:
            from round_robin.database.services import locked_run
            locked_run(session, snapshot.run_id)
        operation = session.scalar(select(BudgetOperation).where(BudgetOperation.id == operation_id).with_for_update().execution_options(populate_existing=True))
        if provider_receipt:
            raw = provider_receipt.read_bytes()
            receipt = json.loads(raw)
            attempt = session.scalar(select(Attempt).where(Attempt.id == operation_id).with_for_update().execution_options(populate_existing=True))
            if operation.kind == "model" and attempt is None:
                raise OperationError("Provider request identity is unresolved; only conservative booking is permitted")
            recorded_request = attempt.provider_request_id if attempt else operation.metadata_json.get("provider_request_id")
            recorded_provider = attempt.provider if attempt else operation.metadata_json.get("provider")
            if not recorded_request or not recorded_provider:
                raise OperationError("Provider request identity is unresolved; only conservative booking is permitted")
            if receipt.get("operation_id") != operation_id or receipt.get("final") is not True or receipt.get("provider_request_id") != recorded_request or receipt.get("provider") != recorded_provider or receipt.get("usage_evidence") is None:
                raise OperationError("A final, operation-bound provider receipt is required")
            reported = str(amount(receipt["reported_usd"]))
            reconcile_operation(session, operation_id, reported, "reported")
            operation.metadata_json = {**operation.metadata_json, "provider_receipt_sha256": hashlib.sha256(raw).hexdigest(), "provider_request_id": receipt["provider_request_id"]}
        elif conservative_usd is not None:
            booked = amount(conservative_usd)
            operation.metadata_json = {**operation.metadata_json, "conservative_booking_usd": str(max(booked, amount(operation.reserved_usd))), "operator_booking": True}
            if operation.status != "settled":
                operation.status = "unknown"
        else:
            raise OperationError("Supply a conservative booking or final provider receipt")
        return {"operation_id": operation.id, "status": operation.status, "uncertainty_released": operation.status == "settled"}
