"""Bounded cleanup of expiring temporary inputs and regenerable exports only."""

from datetime import timedelta
from itertools import islice
from pathlib import Path
import stat

from sqlalchemy import and_, or_, select

from round_robin.database.models import Command, ExportRecord, Upload, utcnow
from round_robin.database.services import lock_policies
from round_robin.reports.storage import ArtifactStore, StorageFailure
from .artifact_lifecycle import artifact_lifecycle, hold_artifact_lifecycle
from .recovery import purge_pending


def maintenance(session_factory, artifact_directory: Path, *, now=None) -> dict:
    now = now or utcnow()
    store = ArtifactStore(artifact_directory)
    expired_uploads, expired_exports, interrupted_exports, orphaned_files = 0, 0, 0, 0
    with session_factory.begin() as session:
        lock_policies(session, "owner_operator")
        hold_artifact_lifecycle(session, store.directory)
        for row in session.scalars(select(Upload).where(Upload.status.in_(['temporary', 'failed']), Upload.created_at < now - timedelta(hours=1)).order_by(Upload.created_at).limit(100).with_for_update(skip_locked=True)):
            row.status, row.canonical_text, row.filename, row.byte_size = 'purged', None, '', 0
            for command in session.scalars(select(Command).where(Command.resource_id == row.id)):
                command.gone, command.result = True, {}
            expired_uploads += 1
        for row in session.scalars(select(ExportRecord).where(or_(and_(ExportRecord.status == 'pending', ExportRecord.created_at < now - timedelta(hours=1)), and_(ExportRecord.status == 'ready', ExportRecord.created_at < now - timedelta(days=7)))).order_by(ExportRecord.created_at).limit(100).with_for_update(skip_locked=True)):
            if row.path:
                store.purge(row.path)
            if row.status == 'pending':
                row.status, row.error_code = 'failed', 'export_interrupted'
                interrupted_exports += 1
            else:
                row.status, row.error_code = 'purged', 'export_expired'
                expired_exports += 1
            row.path = None
            for command in session.scalars(select(Command).where(Command.resource_id == row.id, Command.gone.is_(False))):
                command.result = {**command.result, 'status': row.status, 'error': row.error_code, 'download_url': None}
        referenced = {path for path in session.scalars(select(ExportRecord.path).where(ExportRecord.path.is_not(None)))}
    # Only the export namespace is owned here. Visual originals and canonical
    # uploads have separate lifecycle records and are never guessed as orphans.
    # Retained exports must not consume the orphan cleanup batch forever.
    # A concurrent finalizer/cleanup can already have removed a candidate.
    candidates = (path for path in store.directory.glob('export_*.bin*') if path.name not in referenced)
    for path in islice(candidates, 200):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        age = now.timestamp() - metadata.st_mtime
        if path.name.endswith('.pending') and age > 3600:
            store._path(path.name.removesuffix('.pending'))
            with artifact_lifecycle(store.directory):
                path.unlink(missing_ok=True)
            orphaned_files += 1
        elif path.name.endswith('.bin') and age > 7 * 86400:
            store.purge(path.name); orphaned_files += 1
    manifest = artifact_directory.parent / 'deletion-manifest.jsonl'
    purged = purge_pending(session_factory, artifact_directory, manifest, limit=100)['records_reapplied'] if manifest.exists() else 0
    return {'expired_uploads':expired_uploads, 'expired_exports':expired_exports, 'interrupted_exports':interrupted_exports, 'orphaned_export_files':orphaned_files, 'deletion_records_reapplied':purged, 'provider_requests':0}
