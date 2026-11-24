"""Deletion replay keeps its privacy and execution fences scoped to a meeting."""

from dataclasses import asdict
from datetime import timedelta

from round_robin.database.models import (
    Command,
    Meeting,
    Outbox,
    PreflightRecord,
    Run,
    Upload,
    utcnow,
)
from round_robin.database.visual_models import DesignSession
from round_robin.operations.recovery import purge_record
from round_robin.reports.storage import ArtifactStore
from round_robin.visual.iterations import VisualState

pytest_plugins = ("test_operations",)


def test_replayed_deletion_erases_only_target_and_fences_old_workers(prepared):
    app, meeting, run, _, settings = prepared
    visual_dir = settings.artifact_directory / "visual" / "owner_local" / "design_purge"
    visual_dir.mkdir(parents=True)
    visual_file = visual_dir / "orphan.png"
    visual_file.write_bytes(b"synthetic private visual bytes")
    with app.state.session_factory.begin() as session:
        target_run = session.get(Run, run["id"])
        target_run.execution_fence = 20
        target_run.input_snapshot = {"private": "synthetic input"}
        session.add(Meeting(id="meeting_other", owner_id="owner_local", title="Preserved"))
        session.flush()
        for suffix, meeting_id in (("target", meeting["id"]), ("other", "meeting_other")):
            session.add(PreflightRecord(id=f"preflight_{suffix}", meeting_id=meeting_id, owner_id="owner_local", draft_revision=1, draft_hash="a" * 64, configuration_hash="b" * 64, payload={"private": suffix}, expires_at=utcnow() + timedelta(minutes=30)))
        session.add(Upload(id="upload_target", meeting_id=meeting["id"], owner_id="owner_local", status="ready", filename="private.txt", media_type="text/plain", canonical_text="private text"))
        session.add(Outbox(id="work_purge", run_id=run["id"], kind="advance", status="pending", payload={"private": "work"}))
        session.add(Command(owner_id="owner_local", command_id="target-command", command_type="test", body_hash="c" * 64, meeting_id=meeting["id"], result={"private": "target"}))
        session.add(Command(owner_id="owner_other", command_id="foreign-command", command_type="test", body_hash="c" * 64, meeting_id=meeting["id"], result={"private": "other"}))
        session.add(DesignSession(id="design_purge", meeting_id=meeting["id"], owner_id="owner_local", mode="review_only", frozen_brief={"private": "visual"}, recipients=[], state=asdict(VisualState(mode="review_only", maximum_usd="0", admitted_at=utcnow().timestamp()))))
    with app.state.session_factory.begin() as session:
        purge_record(session, meeting["id"], "owner_local", 5, ArtifactStore(settings.artifact_directory))
    with app.state.session_factory() as session:
        target_run = session.get(Run, run["id"])
        assert target_run.execution_fence > 20
        assert target_run.status == "cancelled"
        assert target_run.stop_reason == "meeting_deleted"
        assert target_run.input_snapshot == {}
        assert target_run.configuration == {"deleted": True}
        assert session.get(PreflightRecord, "preflight_target").payload == {}
        assert session.get(PreflightRecord, "preflight_other").payload == {"private": "other"}
        upload = session.get(Upload, "upload_target")
        assert (upload.status, upload.filename, upload.canonical_text) == ("purged", "", None)
        work = session.get(Outbox, "work_purge")
        assert (work.status, work.payload) == ("cancelled", {})
        target_command = session.get(Command, ("owner_local", "target-command"))
        assert target_command.gone and target_command.result == {}
        foreign_command = session.get(Command, ("owner_other", "foreign-command"))
        assert not foreign_command.gone and foreign_command.result == {"private": "other"}
        design = session.get(DesignSession, "design_purge")
        assert design.status == "deleted" and design.frozen_brief == {}
    assert not visual_file.exists()
