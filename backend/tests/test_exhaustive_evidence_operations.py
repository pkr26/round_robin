"""Storage, restore, cleanup and operator adversarial failure paths."""
from dataclasses import asdict, replace
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from round_robin.database.models import Attempt, BudgetOperation, Command, ExportRecord, Meeting, PolicyBucket, Run, Step, utcnow
from round_robin.operations import durability, recovery
from round_robin.operations.maintenance import maintenance
from round_robin.operations.recovery import OperationError, backup, read_manifest, reconcile, restore_verify
from round_robin.reports.storage import ArtifactStore
from test_deep_operator import operator, execute
from test_operations import prepared


@pytest.mark.parametrize("raw", [b"\xff", b'[]', b'{"meeting_id":1}', b'{"meeting_id":"meeting_one","owner_id":"owner_one","execution_fence":true}'])
def test_corrupt_manifest_never_permits_restore_or_purge(tmp_path, raw):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_bytes(raw)
    with pytest.raises(OperationError, match="corrupt"): read_manifest(manifest)
    manifest.write_text('\n{"meeting_id":"meeting_one","owner_id":"owner_one","execution_fence":1}\n\n')
    assert len(read_manifest(manifest)) == 1


@pytest.mark.parametrize("fault", ["invalid", "symlink", "count", "unreadable"])
def test_marker_corruption_is_closed_and_never_leaks_diagnostics(tmp_path, monkeypatch, fault):
    directory = tmp_path / "control"
    directory.mkdir()
    marker = directory / "failure_test.json"
    marker.write_text(json.dumps({"code": "invented", "timestamp": utcnow().isoformat()}))
    if fault == "symlink":
        marker.unlink()
        marker.symlink_to(tmp_path / "missing")
    elif fault == "count":
        class Scan:
            def __enter__(self): return iter([SimpleNamespace(name=f"failure_{n}.json") for n in range(10001)])
            def __exit__(self, *args): pass
        monkeypatch.setattr(durability.os, "scandir", lambda path: Scan())
    elif fault == "unreadable":
        monkeypatch.setattr(durability.os, "scandir", lambda path: (_ for _ in ()).throw(PermissionError("PRIVATE_PATH")))
    assert durability.marker_pending(tmp_path / "artifacts", control_directory=directory)
    with pytest.raises(ValueError): durability._markers(directory)


def test_failed_marker_storage_keeps_database_gate_and_low_disk_never_clears(prepared, monkeypatch):
    app, _, _, _, settings = prepared
    with pytest.raises(ValueError): durability.mark_failure(app.state.session_factory, "arbitrary", artifact_directory=settings.artifact_directory)
    with monkeypatch.context() as patch:
        patch.setattr(durability, "_sync_directory", lambda *a: (_ for _ in ()).throw(OSError("private filesystem error")))
        assert durability.mark_failure(app.state.session_factory, "backup_failed", artifact_directory=settings.artifact_directory)
    with app.state.session_factory() as session:
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment")).spending_locked
    monkeypatch.setattr(durability.shutil, "disk_usage", lambda _: SimpleNamespace(free=63*1024*1024))
    with pytest.raises(ValueError, match="64 MiB"):
        durability.storage_check(app.state.session_factory, settings.artifact_directory, clear=True)


def test_storage_probe_validates_visual_original_and_each_native_source(prepared, tmp_path):
    from round_robin.database.visual_models import DesignSession
    from round_robin.visual.service import commit_version, save_native_sources
    from round_robin.visual.iterations import VisualState
    from round_robin.visual.renderers.diagram import Diagram, render_diagram
    app, meeting, _, _, settings = prepared
    rendered = render_diagram(Diagram(nodes=[{"id":"node_one", "label":"Bounded", "x":10, "y":10}]))
    with app.state.session_factory.begin() as session:
        design = DesignSession(id="design_storage", meeting_id=meeting["id"], owner_id="owner_local", mode="review_only", frozen_brief={}, recipients=[], state=asdict(VisualState(mode="review_only", maximum_usd="0", admitted_at=utcnow().timestamp())))
        session.add(design); session.flush()
        version = commit_version(session, design, rendered.raster, settings.artifact_directory / "visual", expected_fence=design.execution_fence, source_kind="native_diagram")
        save_native_sources(session, design, version, rendered, settings.artifact_directory / "visual")
    assert durability.storage_check(app.state.session_factory, settings.artifact_directory)["retained_artifacts_verified"] == 1
    source = tmp_path / "visual_backup"
    manifest = settings.artifact_directory.parent / "deletion-manifest.jsonl"
    backup(app.state.engine, settings.artifact_directory, source, manifest)
    restored = restore_verify(source, f"sqlite:///{tmp_path/'visual_restore_test.db'}", tmp_path/"restored_artifacts", manifest)
    assert restored["dispatch_locked"]


def test_cleanup_reaches_orphans_beyond_referenced_batch_and_refreshes_receipts(prepared, monkeypatch):
    app, meeting, run, _, settings = prepared
    now = utcnow()
    store = ArtifactStore(settings.artifact_directory)
    active = []
    with app.state.session_factory.begin() as session:
        for index in range(201):
            key, sha = store.put(f"active {index}".encode())
            active.append(store.directory / key)
            session.add(ExportRecord(id=f"export_active_{index}", run_id=run["id"], owner_id="owner_local", report_version=1,
                format="json", status="ready", path=key, sha256=sha))
        expired_key, sha = store.put(b"expired")
        expired = ExportRecord(id="export_old_receipt", run_id=run["id"], owner_id="owner_local", report_version=1,
                format="json", status="ready", path=expired_key, sha256=sha, created_at=now-timedelta(days=8))
        session.add(expired)
        session.add(Command(owner_id="owner_local", command_id=str(uuid4()), command_type="export", body_hash="a"*64,
            resource_id=expired.id, meeting_id=meeting["id"], result={"status":"ready", "download_url":"private-link"}))
    orphan_key, _ = store.put(b"unreferenced private artifact")
    orphan = store.directory/orphan_key
    old = (now-timedelta(days=8)).timestamp()
    os.utime(orphan,(old,old))
    pending = store.directory / ("export_" + "a"*32 + ".bin.pending")
    pending.write_bytes(b"interrupted private bytes"); os.utime(pending,(old,old))
    fresh = store.directory / ("export_" + "b"*32 + ".bin.pending")
    fresh.write_bytes(b"fresh pending")
    symlink = store.directory / ("export_" + "c"*32 + ".bin")
    symlink.symlink_to(active[0])
    vanished = store.directory / ("export_" + "d"*32 + ".bin")
    original_glob = Path.glob
    def listed(path, pattern):
        return iter([*active, orphan, pending, fresh, symlink, vanished]) if path == store.directory and pattern == 'export_*.bin*' else original_glob(path, pattern)
    monkeypatch.setattr(Path, "glob", listed)
    result = maintenance(app.state.session_factory, store.directory, now=now)
    assert result["orphaned_export_files"] == 2 and result["expired_exports"] == 1
    assert not orphan.exists() and not pending.exists() and fresh.exists() and symlink.is_symlink()
    assert all(path.exists() for path in active)
    with app.state.session_factory() as session:
        receipt = session.scalar(select(Command).where(Command.resource_id == "export_old_receipt"))
        assert receipt.result["status"] == "purged" and receipt.result["download_url"] is None


def _repack(source, packet):
    content = json.dumps(packet, sort_keys=True).encode()
    (source/"snapshot.json").write_bytes(content)
    (source/"snapshot.sha256").write_text(hashlib.sha256(content).hexdigest())


@pytest.mark.parametrize("mutation", ["hash", "version", "schema", "tables", "snapshot_manifest", "incomplete_manifest", "nonempty_artifacts"])
def test_restore_rejects_modified_archive_before_import(prepared, tmp_path, mutation):
    app, _, _, _, settings = prepared
    source, target = tmp_path/"backup", tmp_path/"target"
    manifest = settings.artifact_directory.parent/"deletion-manifest.jsonl"
    backup(app.state.engine, settings.artifact_directory, source, manifest)
    packet = json.loads((source/"snapshot.json").read_text())
    if mutation == "hash": (source/"snapshot.sha256").write_text("0"*64)
    elif mutation == "snapshot_manifest": (source/"deletion-manifest.snapshot.jsonl").write_bytes(b"tampered")
    elif mutation == "incomplete_manifest":
        archived = b'{"meeting_id":"meeting_missing","owner_id":"owner_local","execution_fence":1}\n'
        (source/"deletion-manifest.snapshot.jsonl").write_bytes(archived)
        packet["deletion_snapshot_sha256"] = hashlib.sha256(archived).hexdigest()
        _repack(source, packet)
    elif mutation == "nonempty_artifacts":
        target.mkdir(); (target/"keep").write_bytes(b"preserve")
    else:
        if mutation == "version": packet["format_version"] = 2
        elif mutation == "schema": packet["schema_revision"] = "unrecognized"
        else: packet["tables"].pop("runs")
        _repack(source, packet)
    db = tmp_path/"failed_restore_test.db"
    with pytest.raises(OperationError): restore_verify(source, f"sqlite:///{db}", target, manifest)
    assert not db.exists()


def test_restore_reserved_attempts_and_sessions_are_quarantined(prepared, tmp_path):
    app, _, run, _, settings = prepared
    with app.state.session_factory.begin() as session:
        session.add(Step(id="step_restore", run_id=run["id"], stage="synthesis", role_slot_id="synthesis", status="authorized"))
        session.flush()
        session.add(Attempt(id="attempt_restore", run_id=run["id"], step_id="step_restore", provider="openai", status="authorized", reserved_usd="0.2", usage_certainty="reserved"))
        session.add(BudgetOperation(id="operation_reserved", owner_id="owner_local", run_id=run["id"], kind="model", reserved_usd="0.2", status="reserved"))
        session.add(PolicyBucket(id="session_restore", key="session:restore", limit_usd="0", spending_locked=True, metadata_json={"kind":"session", "state":{"private":"state"}, "revoked":False}))
    source = tmp_path/"backup"
    manifest = settings.artifact_directory.parent/"deletion-manifest.jsonl"
    backup(app.state.engine, settings.artifact_directory, source, manifest)
    url = f"sqlite:///{tmp_path/'reserved_restore_test.db'}"
    assert restore_verify(source,url,tmp_path/"restored",manifest)["sessions_revoked"]
    engine = create_engine(url)
    with sessionmaker(engine)() as session:
        assert session.get(Attempt,"attempt_restore").usage_certainty == "unknown"
        assert session.get(BudgetOperation,"operation_reserved").status == "unknown"
        assert session.get(PolicyBucket,"session_restore").metadata_json == {"kind":"session","revoked":True,"state":{}}
    engine.dispose()


def test_purge_scope_and_idempotent_absent_meetings(prepared):
    app, meeting, _, _, settings = prepared
    with app.state.session_factory.begin() as session:
        with pytest.raises(OperationError,match="ownership"):
            recovery.purge_record(session,meeting["id"],"owner_intruder",1)
        recovery.purge_record(session,"meeting_absent","owner_local",3)
        recovery.purge_record(session,"meeting_absent","owner_local",3)


def test_usage_reconciliation_requires_bound_final_receipt_and_never_reopens_settlement(prepared,tmp_path):
    app, _, _, _, _ = prepared
    factory = app.state.session_factory
    with pytest.raises(OperationError,match="not found"): reconcile(factory,"operation_absent",conservative_usd="0")
    with pytest.raises(OperationError,match="Supply"): reconcile(factory,"operation_unknown")
    receipt = tmp_path/"receipt.json"
    with factory.begin() as session:
        row = session.get(BudgetOperation,"operation_unknown")
        row.kind = "public_search"
        row.metadata_json = {"provider":"brave", "provider_request_id":"request_synthetic"}
    receipt.write_text(json.dumps({"operation_id":"operation_unknown","provider":"wrong","provider_request_id":"request_synthetic","final":True,"usage_evidence":{},"reported_usd":"0.1"}))
    with pytest.raises(OperationError,match="operation-bound"): reconcile(factory,"operation_unknown",provider_receipt=receipt)
    receipt.write_text(json.dumps({"operation_id":"operation_unknown","provider":"brave","provider_request_id":"request_synthetic","final":True,"usage_evidence":{},"reported_usd":"0.1"}))
    assert reconcile(factory,"operation_unknown",provider_receipt=receipt)["uncertainty_released"]
    assert reconcile(factory,"operation_unknown",conservative_usd="0.5")["status"] == "settled"
    with factory.begin() as session:
        row = session.get(BudgetOperation,"operation_unknown"); row.metadata_json={}
    with pytest.raises(OperationError,match="unresolved"): reconcile(factory,"operation_unknown",provider_receipt=receipt)


def test_operator_funding_schema_and_independent_recovery_guards(operator, tmp_path, monkeypatch, capsys):
    from round_robin import ops
    from round_robin.api import preflight
    from round_robin.providers import images
    app, settings = operator
    funded = replace(settings,paid_access_enabled=True)
    monkeypatch.delenv("ROUND_ROBIN_IMAGE_CONFIG_FILE",raising=False)
    with pytest.raises(OperationError,match="validated explicitly funded"): ops.funding_configuration(funded)
    monkeypatch.setattr(preflight,"run_configuration",lambda *a:{"funded":False})
    with pytest.raises(OperationError,match="not explicitly funded"): ops.funding_configuration(replace(funded,provider_config_file=tmp_path/"funding"))
    monkeypatch.setenv("ROUND_ROBIN_IMAGE_CONFIG_FILE",str(tmp_path/"image"))
    monkeypatch.setattr(images,"load_image_configuration",lambda *a: (_ for _ in ()).throw(images.ImageFundingLocked("private")))
    with pytest.raises(OperationError,match="Image configuration"): ops.funding_configuration(funded)
    with app.state.engine.begin() as connection: connection.execute(text("UPDATE alembic_version SET version_num='invalid'"))
    with pytest.raises(OperationError,match="migration revision"): ops.config_check(settings,app.state.engine)
    with monkeypatch.context() as patch:
        patch.setattr(ops,"funding_configuration",lambda s:{})
        with pytest.raises(OperationError,match="migration revision"): ops.config_check(funded,app.state.engine)
    with app.state.session_factory.begin() as session:
        from round_robin.database.services import lock_policies
        deployment,_ = lock_policies(session,"owner_operator")
        deployment.metadata_json={"restore_quarantine":True}
    with pytest.raises(OperationError,match="quarantine"): ops.set_policy_flag(app.state.session_factory,name="spending_locked",value=False)
    durability.mark_failure(app.state.session_factory,"artifact_storage_failed",artifact_directory=settings.artifact_directory)
    assert execute(capsys,"spend-lock","--unlock",status=1)["status"] == "blocked"


def test_manifest_reapplication_removes_all_content_families_but_preserves_accounting(prepared,tmp_path):
    from round_robin.database.models import Upload
    app,meeting,run,_,settings=prepared
    store=ArtifactStore(settings.artifact_directory)
    key,digest=store.put(b"private archived export")
    with app.state.session_factory.begin() as session:
        session.add(Upload(id="upload_purge",meeting_id=meeting["id"],owner_id="owner_local",status="ready",filename="private.txt",media_type="text/plain",canonical_text="private",byte_size=7))
        session.add(Step(id="step_purge",run_id=run["id"],stage="proposal",role_slot_id="proposal",result_json={"private":"result"}));session.flush()
        session.add(Attempt(id="attempt_purge",run_id=run["id"],step_id="step_purge",provider="openai",request_json={"private":"request"},response_json={"private":"reply"},configuration={"private":"config"}))
        for identity,state,path in [("export_purge","ready",key),("export_pending","pending",None)]:
            session.add(ExportRecord(id=identity,run_id=run["id"],owner_id="owner_local",report_version=1,format="json",status=state,path=path,sha256=digest))
    # Pending artifacts are not archived; ready bytes are integrity checked and
    # restored exactly before any newer independent deletion is reapplied.
    source=tmp_path/"backup_all"
    manifest=settings.artifact_directory.parent/"deletion-manifest.jsonl"
    backup(app.state.engine,settings.artifact_directory,source,manifest)
    target=f"sqlite:///{tmp_path/'all_restore_test.db'}"
    restore_verify(source,target,tmp_path/"restored_all",manifest)
    assert (tmp_path/"restored_all"/key).read_bytes()==b"private archived export"
    manifest.write_text(json.dumps({"meeting_id":meeting["id"],"owner_id":"owner_local","execution_fence":3})+"\n")
    assert recovery.purge_pending(app.state.session_factory,settings.artifact_directory,manifest)["records_reapplied"]==1
    with app.state.session_factory() as session:
        assert session.get(Upload,"upload_purge").canonical_text is None
        assert session.get(Step,"step_purge").result_json is None
        assert session.get(Attempt,"attempt_purge").request_json=={}
        assert session.get(Attempt,"attempt_purge").response_json is None
        assert session.get(ExportRecord,"export_pending").status=="purged"
        assert session.get(BudgetOperation,"operation_unknown").status=="unknown"
    assert not (store.directory/key).exists()


def test_restore_rechecks_empty_database_after_destination_validation(prepared,tmp_path,monkeypatch):
    app,_,_,_,settings=prepared
    source=tmp_path/"backup_race";manifest=settings.artifact_directory.parent/"deletion-manifest.jsonl"
    backup(app.state.engine,settings.artifact_directory,source,manifest)
    original=recovery.create_engine
    raced=tmp_path/"raced_restore_test.db"
    def race(url,**kwargs):
        engine=original(url,**kwargs)
        with engine.begin() as connection: connection.execute(text("CREATE TABLE preserve_me (id INTEGER)"))
        return engine
    monkeypatch.setattr(recovery,"create_engine",race)
    with pytest.raises(OperationError,match="not empty"):
        restore_verify(source,f"sqlite:///{raced}",tmp_path/"target_race",manifest)
    with original(f"sqlite:///{raced}").connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM preserve_me")).scalar_one()==0


def test_failed_restore_disposes_engine_and_retains_quarantine(prepared,tmp_path,monkeypatch):
    app,_,run,_,settings=prepared
    store=ArtifactStore(settings.artifact_directory)
    key,digest=store.put(b"private export")
    with app.state.session_factory.begin() as session:
        session.add(ExportRecord(id="export_restore_cleanup",run_id=run["id"],owner_id="owner_local",report_version=1,format="json",status="ready",path=key,sha256=digest))
    source=tmp_path/"backup_dispose";manifest=settings.artifact_directory.parent/"deletion-manifest.jsonl"
    backup(app.state.engine,settings.artifact_directory,source,manifest)
    (source/"artifacts"/key).write_bytes(b"corrupt")
    original=recovery.create_engine;disposed=[]
    def tracked(*args,**kwargs):
        engine=original(*args,**kwargs);dispose=engine.dispose
        def finish():disposed.append(True);dispose()
        engine.dispose=finish
        return engine
    monkeypatch.setattr(recovery,"create_engine",tracked)
    target=f"sqlite:///{tmp_path/'cleanup_restore_test.db'}"
    with pytest.raises(OperationError,match="integrity"):
        restore_verify(source,target,tmp_path/"failed_artifacts",manifest)
    assert disposed==[True]
    engine=original(target)
    with sessionmaker(engine)() as session:
        policy=session.scalar(select(PolicyBucket).where(PolicyBucket.key=="deployment"))
        assert policy.spending_locked and policy.metadata_json["restore_quarantine"]
    engine.dispose()
