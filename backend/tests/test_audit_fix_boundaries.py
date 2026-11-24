"""Regressions for audited process, evidence, streaming and backup failures."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.tests.test_artifacts_api import artifacts
from backend.tests.test_operations import prepared
from backend.tests.test_deep_evidence_supervisors import verifier
from scripts import dev, verify

_REAL_POPEN = subprocess.Popen
_REAL_RUN = subprocess.run
_REAL_KILLPG = os.killpg
_REAL_SLEEP = time.sleep


def test_interrupt_replaces_old_success_before_checks(verifier, monkeypatch):
    verifier['output'].write_text('{"all_checks_passed":true,"marker":"old"}')
    def interrupt(*args, **kwargs):
        current = json.loads(verifier['output'].read_text())
        assert current['all_checks_passed'] is False and current['status'] == 'running'
        raise KeyboardInterrupt
    monkeypatch.setattr(verify.subprocess, 'run', interrupt)
    assert verify.main() == 1
    record = json.loads(verifier['output'].read_text())
    assert record['all_checks_passed'] is False
    assert record['status'] == 'interrupted'
    assert 'marker' not in record


@pytest.mark.parametrize('supervisor', ['dev', 'verify'])
def test_shutdown_kills_descendant_after_launcher_exits(tmp_path, monkeypatch, verifier, supervisor):
    # This child really ignores TERM. Its parent exits on TERM, which was the
    # audited escape from the old wait-only-on-launcher cleanup.
    launcher = tmp_path / 'launcher.py'
    launcher.write_text('''import os, signal, subprocess, sys, time
from pathlib import Path
root = Path(__file__).parent
if sys.argv[-1] == "child":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "child.pid").write_text(str(os.getpid()))
    while True: signal.pause()
subprocess.Popen([sys.executable, __file__, "child"])
while not (root / "child.pid").exists(): time.sleep(.01)
(root / "ready").touch()
while True: signal.pause()
''')
    real_popen, real_killpg, real_sleep = _REAL_POPEN, _REAL_KILLPG, _REAL_SLEEP
    owned = []
    def launch(*args, **kwargs):
        if owned:
            if supervisor == 'dev':
                handlers[signal.SIGINT]()
            return SimpleNamespace(pid=2000000000, returncode=0, poll=lambda: 0, wait=lambda **kw: 0)
        process = real_popen([sys.executable, str(launcher)], start_new_session=True)
        owned.append(process)
        deadline = time.monotonic() + 5
        while not (tmp_path / 'ready').exists():
            assert time.monotonic() < deadline
            real_sleep(.01)
        return process
    # verifier fixture patches shared stdlib methods; restore only those needed
    # for the owned real process, without launching product infrastructure.
    monkeypatch.setattr(subprocess, 'Popen', launch)
    monkeypatch.setattr(os, 'killpg', real_killpg)
    terminate = dev.terminate_owned
    monkeypatch.setattr(dev, 'terminate_owned', lambda processes: terminate(processes, grace_seconds=.3))
    monkeypatch.setattr(verify, 'terminate_owned', lambda processes: terminate(processes, grace_seconds=.3))
    try:
        if supervisor == 'verify':
            assert verify.main() == 1  # dummy siblings exit before readiness
        else:
            handlers = {}
            monkeypatch.setattr(sys, 'argv', ['dev', '--skip-services'])
            # The verifier deliberately inherits an isolated SQLite URL, while
            # dev validates PostgreSQL/Redis targets before launching. This
            # process-cleanup fixture owns all launches and must specify its
            # own valid synthetic targets instead of depending on its parent.
            monkeypatch.setenv('ROUND_ROBIN_DATABASE_URL', 'postgresql+psycopg://test:test@127.0.0.1:1/cleanup_test')
            monkeypatch.setenv('ROUND_ROBIN_BROKER_URL', 'redis://127.0.0.1:1/14')
            monkeypatch.setattr(dev, 'available', lambda port: True)
            monkeypatch.setattr(signal, 'signal', lambda number, handler: handlers.update({number: handler}))
            monkeypatch.setattr(time, 'sleep', lambda seconds: handlers[signal.SIGINT]())
            assert dev.main() == 0
        pid = int((tmp_path / 'child.pid').read_text())
        deadline = time.monotonic() + 5
        while True:
            # ps distinguishes a reaped/dead zombie from a running orphan on
            # both supported POSIX development platforms.
            # run() internally looks up Popen, so restore that temporarily for
            # this read-only liveness check after supervisor cleanup completes.
            with monkeypatch.context() as checks:
                checks.setattr(subprocess, 'Popen', real_popen)
                result = _REAL_RUN(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
            if result.returncode or not result.stdout.strip() or result.stdout.strip().startswith('Z'):
                break
            assert time.monotonic() < deadline, 'owned descendant survived shutdown'
            real_sleep(.02)
    finally:
        for process in owned:
            try:
                real_killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=5)


def test_fifteen_concurrent_export_downloads_release_initial_connections(artifacts, monkeypatch):
    app, client, _, run_id, report = artifacts
    exported = client.post(f'/api/runs/{run_id}/exports', json={
        'command_id': str(uuid4()), 'report_id': report['id'], 'format': 'json',
    }).json()
    pool = app.state.engine.pool
    capacity = pool.size() + pool._max_overflow
    assert capacity == 15
    pool._timeout = .5
    from round_robin.api import artifacts as routes
    original = routes.ArtifactStore.read
    barrier = threading.Barrier(capacity)
    def read_together(store, *args):
        content = original(store, *args)
        barrier.wait(timeout=10)
        return content
    monkeypatch.setattr(routes.ArtifactStore, 'read', read_together)
    with ThreadPoolExecutor(max_workers=capacity) as executor:
        responses = list(executor.map(lambda _: client.get(exported['download_url']), range(capacity)))
    assert all(response.status_code == 200 and response.json() == report for response in responses)
    assert pool.checkedout() == 0


def test_backup_rejects_unrestorable_size_before_publication_or_verification(prepared, tmp_path, monkeypatch):
    from round_robin.operations import recovery
    from round_robin.database.models import PolicyBucket
    app, _, _, _, settings = prepared
    monkeypatch.setattr(recovery, 'MAX_SNAPSHOT_BYTES', 1)
    destination = tmp_path / 'oversized_backup'
    manifest = settings.artifact_directory.parent / 'deletion-manifest.jsonl'
    with pytest.raises(recovery.OperationError, match='restoration size'):
        recovery.backup(app.state.engine, settings.artifact_directory, destination, manifest)
    assert not destination.exists()
    assert not list(tmp_path.glob('.backup_pending_*'))
    with app.state.session_factory() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == 'deployment'))
        assert policy.metadata_json['backup_failure_unresolved']
        assert 'last_verified_backup_hash' not in policy.metadata_json


def test_restore_size_boundary_accepts_exact_limit_and_rejects_one_byte_more(prepared, tmp_path, monkeypatch):
    from round_robin.operations import recovery
    app, _, _, _, settings = prepared
    source = tmp_path / 'boundary_backup'
    manifest = settings.artifact_directory.parent / 'deletion-manifest.jsonl'
    recovery.backup(app.state.engine, settings.artifact_directory, source, manifest)
    size = (source / 'snapshot.json').stat().st_size
    monkeypatch.setattr(recovery, 'MAX_SNAPSHOT_BYTES', size - 1)
    rejected = tmp_path / 'restore_test_rejected.db'
    with pytest.raises(recovery.OperationError, match='integrity'):
        recovery.restore_verify(source, f'sqlite:///{rejected}', tmp_path / 'rejected_assets', manifest)
    assert not rejected.exists()
    monkeypatch.setattr(recovery, 'MAX_SNAPSHOT_BYTES', size)
    result = recovery.restore_verify(source, f'sqlite:///{tmp_path / "restore_test_boundary.db"}', tmp_path / 'restored_assets', manifest)
    assert result['status'] == 'restored_quarantined'
