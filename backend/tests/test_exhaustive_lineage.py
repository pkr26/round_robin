"""Atomic parent replacement is explicit authority, never a side effect of edits."""
from dataclasses import replace
import json
import os
from pathlib import Path
import stat

import pytest
from sqlalchemy import select

from backend.tests.test_api import application, client, command, create, preflight, start
from round_robin.database.models import Command, Draft, Event, Meeting, Outbox, Run


def followup(client, meeting, parent):
    brief = {**meeting["draft"], "title": "A new expressly reviewed approach"}
    response = client.post(f"/api/meetings/{meeting['id']}/follow-ups", json=command(parent_run_id=parent["id"], expected_draft_revision=meeting["draft_revision"], brief=brief))
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_input", "cancel_requested"])
def test_active_followup_edits_are_harmless_and_start_atomically_replaces(client, application, status):
    meeting = create(client)
    parent = start(client, meeting).json()
    with application.state.session_factory.begin() as session:
        session.get(Run, parent["id"]).status = status
    linked = followup(client, meeting, parent)
    quote = preflight(client, linked)
    assert quote["parent_replacement"] == {"run_id": parent["id"], "version": 1, "status": status}
    with application.state.session_factory() as session:
        row = session.get(Run, parent["id"])
        assert row.status == status and row.execution_fence == 0
        assert row.input_snapshot == meeting["draft"]
    body = command(preflight_id=quote["id"], expected_draft_revision=2)
    denied = client.post(f"/api/meetings/{meeting['id']}/runs", json=body)
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "parent_replacement_stale"
    body["replace_parent_run_version"] = quote["parent_replacement"]["version"]
    accepted = client.post(f"/api/meetings/{meeting['id']}/runs", json=body)
    assert accepted.status_code == 202, accepted.text
    assert client.post(f"/api/meetings/{meeting['id']}/runs", json=body).json() == accepted.json()
    with application.state.session_factory() as session:
        row = session.get(Run, parent["id"])
        child = session.get(Run, accepted.json()["id"])
        assert row.status == "cancel_requested" and row.stop_reason == "replaced_by_follow_up"
        assert row.execution_fence == 1 and row.version == 2
        assert row.input_snapshot == meeting["draft"]
        assert child.parent_run_id == row.id and child.input_snapshot == linked["draft"]
        assert len(list(session.scalars(select(Run)))) == 2
        assert len(list(session.scalars(select(Outbox).where(Outbox.run_id == row.id, Outbox.kind == "finalize")))) == 1


@pytest.mark.parametrize("failure", ["stale", "quota", "bool", "terminal"])
def test_failed_replacement_does_not_partially_cancel_parent(client, application, failure):
    meeting = create(client)
    parent = start(client, meeting).json()
    linked = followup(client, meeting, parent)
    quote = preflight(client, linked)
    body = command(preflight_id=quote["id"], expected_draft_revision=2, replace_parent_run_version=True if failure == "bool" else 1)
    if failure == "quota":
        application.state.settings = replace(application.state.settings, max_queued_per_owner=0)
    if failure in {"stale", "terminal"}:
        with application.state.session_factory.begin() as session:
            row = session.get(Run, parent["id"])
            row.version += 1
            if failure == "terminal":
                row.status = "completed"
    response = client.post(f"/api/meetings/{meeting['id']}/runs", json=body)
    assert response.status_code == {"stale": 409, "terminal": 409, "quota": 429, "bool": 422}[failure], response.text
    with application.state.session_factory() as session:
        row = session.get(Run, parent["id"])
        assert row.execution_fence == 0 and row.status == ("completed" if failure == "terminal" else "queued")
        assert len(list(session.scalars(select(Run)))) == 1
        assert session.get(Command, ("owner_local", body["command_id"])) is None
        assert session.scalar(select(Outbox).where(Outbox.kind == "finalize")) is None


def test_terminal_parent_needs_no_stop_authority_but_forged_replacement_is_rejected(client, application):
    meeting = create(client)
    parent = start(client, meeting).json()
    with application.state.session_factory.begin() as session:
        session.get(Run, parent["id"]).status = "completed"
    linked = followup(client, meeting, parent)
    quote = preflight(client, linked)
    assert quote["parent_replacement"] is None
    body = command(preflight_id=quote["id"], expected_draft_revision=2, replace_parent_run_version=1)
    assert client.post(f"/api/meetings/{meeting['id']}/runs", json=body).status_code == 409
    body.pop("replace_parent_run_version")
    response = client.post(f"/api/meetings/{meeting['id']}/runs", json=body)
    assert response.status_code == 202 and response.json()["parent_run_id"] == parent["id"]


def test_foreign_parent_cannot_be_attached_or_laundered_through_a_draft(client, application):
    meeting, other = create(client), create(client)
    parent = start(client, other).json()
    response = client.post(f"/api/meetings/{meeting['id']}/follow-ups", json=command(parent_run_id=parent["id"], expected_draft_revision=1, brief=meeting["draft"]))
    assert response.status_code == 409
    with application.state.session_factory.begin() as session:
        session.get(Draft, meeting["id"]).parent_run_id = parent["id"]
    response = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1))
    assert response.status_code == 409


def test_missing_deletion_manifest_fails_before_any_private_state_is_deleted(client, application):
    meeting = create(client)
    application.state.record_deletion_manifest = None
    response = client.request("DELETE", f"/api/meetings/{meeting['id']}", json=command())
    assert response.status_code == 503 and response.json()["error"]["code"] == "deletion_manifest_unavailable"
    with application.state.session_factory() as session:
        assert session.get(Meeting, meeting["id"]).deleted_at is None
        assert session.get(Draft, meeting["id"]).payload == meeting["draft"]


def test_deletion_manifest_is_private_append_only_and_rejects_symlinks(tmp_path):
    from round_robin.app import _record_manifest
    path = tmp_path / "control" / "manifest.jsonl"
    _record_manifest(path, "meeting_first", "owner_first", 1)
    _record_manifest(path, "meeting_second", "owner_second", 2)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [json.loads(line)["execution_fence"] for line in path.read_text().splitlines()] == [1, 2]
    other = tmp_path / "private-other"
    other.write_text("untouched")
    linked = tmp_path / "linked"
    linked.symlink_to(other)
    with pytest.raises(OSError):
        _record_manifest(linked, "meeting_first", "owner_first", 1)
    assert other.read_text() == "untouched"


def test_deletion_manifest_refuses_nonregular_files_without_blocking(tmp_path):
    from round_robin.app import _record_manifest
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    # Keep a reader open so O_NONBLOCK open succeeds; the regular-file guard
    # must reject before writing any private ownership metadata.
    descriptor = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(OSError, match="regular private file"):
            _record_manifest(fifo, "meeting_first", "owner_first", 1)
        assert os.read(descriptor, 1024) == b""
    finally:
        os.close(descriptor)
