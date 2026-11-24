import pytest
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import (
    Attempt,
    ExportRecord,
    Meeting,
    Outbox,
    Outcome,
    PreflightRecord,
    Run,
    Step,
    utcnow,
)
from round_robin.database.visual_models import (
    DesignReview,
    DesignSession,
    DesignVersion,
)
from round_robin.reports.storage import ArtifactStore

from backend.tests.test_api import command, create, start

pytest_plugins = ("backend.tests.test_api",)


@pytest.mark.parametrize("damage", ["attempt_forward", "attempt_reverse", "outbox", "claim_step", "design_review", "duplicate_export", "reverse_preflight", "consumed_foreign_run", "outcome_cross_meeting"])
def test_cross_meeting_links_refuse_before_manifest(application, client, damage):
    first, second = create(client), create(client)
    first_run, second_run = start(client, first).json()["id"], start(client, second).json()["id"]
    store = ArtifactStore(application.state.settings.artifact_directory)
    path = None
    with application.state.session_factory.begin() as session:
        if damage in {"attempt_forward", "attempt_reverse", "outbox", "claim_step"}:
            session.add_all([Step(id="step_one", run_id=first_run, stage="proposal", role_slot_id="one", provider="openai"), Step(id="step_two", run_id=second_run, stage="proposal", role_slot_id="two", provider="openai")])
            session.flush()
            if damage in {"attempt_forward", "attempt_reverse"}:
                session.add(Attempt(id="attempt_cross", run_id=first_run if damage == "attempt_forward" else second_run, step_id="step_two" if damage == "attempt_forward" else "step_one", provider="openai"))
            elif damage == "outbox":
                session.add(Outbox(id="outbox_cross", run_id=first_run, step_id="step_two", kind="execute"))
            else:
                session.add(ClaimVersion(id="claim_cross", run_id=first_run, meeting_id=first["id"], owner_id="owner_local", step_id="step_two", version=1, sha256="0" * 64, content={}))
        elif damage == "design_review":
            for index, meeting in enumerate((first, second), 1):
                session.add(DesignSession(id=f"design_{index}", meeting_id=meeting["id"], owner_id="owner_local", mode="review_only", frozen_brief={}, state={}))
            session.flush()
            session.add(DesignVersion(id="version_other", session_id="design_2", version=1, asset_path="/synthetic/not-opened", canonical_hash="0" * 64, original_hash="0" * 64, width=1, height=1, source_kind="upload", provenance={}))
            session.flush()
            session.add(DesignReview(id="review_cross", session_id="design_1", version_id="version_other", provider="openai", receipt={}, critique={}))
        elif damage == "duplicate_export":
            key, digest = store.put(b"shared export remains intact")
            path = store.directory / key
            session.add_all([ExportRecord(id=f"export_{index}", run_id=run, owner_id="owner_local", report_version=1, format="json", status="ready", path=key, sha256=digest) for index, run in enumerate((first_run, second_run), 1)])
        elif damage == "reverse_preflight":
            record = PreflightRecord(id="preflight_cross", meeting_id=first["id"], owner_id="owner_local", draft_revision=1, draft_hash="0" * 64, configuration_hash="0" * 64, payload={}, expires_at=utcnow())
            session.add(record)
            session.flush()
            session.get(Run, second_run).preflight_id = record.id
        elif damage == "consumed_foreign_run":
            session.get(PreflightRecord, session.get(Run, second_run).preflight_id).consumed_by_run_id = None
            session.flush()
            session.get(PreflightRecord, session.get(Run, first_run).preflight_id).consumed_by_run_id = second_run
        else:
            session.add(Outcome(id="outcome_cross", meeting_id=second["id"], owner_id="owner_local", run_id=first_run, revision=1, payload={}))
    calls = []
    application.state.record_deletion_manifest = lambda *args: calls.append(args)
    from round_robin.operations.deletion_scope import deletion_scopes_consistent
    with application.state.session_factory() as session:
        assert not deletion_scopes_consistent(session, [(second["id"], "owner_local"), (first["id"], "owner_local")])
    response = client.request("DELETE", f"/api/meetings/{first['id']}", json=command())
    assert response.status_code == 409 and response.json()["error"]["code"] == "deletion_scope_invalid", response.text
    assert not calls
    if path:
        assert path.read_bytes() == b"shared export remains intact"
    with application.state.session_factory() as session:
        assert session.get(Meeting, first["id"]).deleted_at is None
