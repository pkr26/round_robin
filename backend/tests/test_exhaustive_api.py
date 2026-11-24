"""Fresh adversarial API review: failure atomicity, resource limits and privacy."""
from dataclasses import replace
from datetime import timedelta
import asyncio
import hashlib
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from backend.tests.test_artifacts_api import artifacts
from backend.tests.test_api import application, client, command, create, preflight, start
from round_robin.database.models import Command, Draft, ExportRecord, Meeting, PolicyBucket, ReportVersion, Run, Upload, utcnow


def test_export_commit_failure_removes_unreferenced_private_artifact(artifacts):
    app, client, _, run_id, report = artifacts
    def fail_final_commit(session):
        if any(isinstance(row, ExportRecord) and row.status == "ready" for row in session.identity_map.values()):
            raise OperationalError("private-export-commit-sentinel", {}, Exception("private"))
    event.listen(app.state.session_factory, "before_commit", fail_final_commit)
    try:
        response = client.post(f"/api/runs/{run_id}/exports", json=command(report_id=report["id"], format="json"))
    finally:
        event.remove(app.state.session_factory, "before_commit", fail_final_commit)
    assert response.status_code == 503
    assert "private-export-commit-sentinel" not in response.text
    assert not list(app.state.settings.artifact_directory.glob("*.bin"))
    with app.state.session_factory() as session:
        row = session.scalar(select(ExportRecord))
        assert row.path is None and row.status == "pending"


@pytest.mark.parametrize("headers,status", [
    ({"Content-Type": "application/octet-stream"}, 415),
    ({"X-Content-SHA256": "invalid"}, 422),
    ({"X-Filename": " "}, 422),
    ({"X-Filename": "a" * 201}, 422),
    ({"X-Filename": "file\x1f.txt"}, 422),
    ({"Content-Length": "invalid"}, 411),
    ({"Content-Length": "0"}, 413),
    ({"Content-Length": "-1"}, 413),
    ({"Content-Length": "1048577"}, 413),
])
def test_upload_header_attacks_do_not_create_reservations(artifacts, headers, status):
    app, client, meeting_id, _, _ = artifacts
    body = b"private upload"
    base = {"Content-Type": "text/plain", "X-Command-Id": str(uuid4()), "X-Expected-Revision": "1", "X-Filename": "source.txt", "X-Content-SHA256": hashlib.sha256(body).hexdigest()}
    response = client.post(f"/api/meetings/{meeting_id}/uploads", content=body, headers={**base, **headers})
    assert response.status_code == status, response.text
    with app.state.session_factory() as session:
        assert session.scalar(select(Upload)) is None
        assert session.get(Command, ("owner_local", base["X-Command-Id"])) is None


@pytest.mark.parametrize("body,declared", [(b"x" * (1048576 + 1), 1), (b"x", 2), (b"\x00", 1), (b"  ", 2), (b"\xff", 1)])
def test_invalid_upload_body_settles_without_retaining_private_text(artifacts, body, declared):
    app, client, meeting_id, _, _ = artifacts
    headers = {"Content-Type": "text/markdown", "Content-Length": str(declared), "X-Command-Id": str(uuid4()), "X-Expected-Revision": "1", "X-Filename": "source.txt", "X-Content-SHA256": "0" * 64}
    response = client.post(f"/api/meetings/{meeting_id}/uploads", content=body, headers=headers)
    assert response.status_code == 422
    with app.state.session_factory() as session:
        row = session.scalar(select(Upload))
        assert row.status == "failed" and row.canonical_text is None and row.byte_size == 0
    listing = client.get(f"/api/meetings/{meeting_id}/uploads").json()["items"]
    assert len(listing) == 1 and listing[0]["status"] == "failed"
    assert "canonical_text" not in listing[0]


@pytest.mark.parametrize("race,expected", [("delete", 410), ("expire", 422)])
def test_upload_stream_cannot_revive_deleted_or_expired_reservation(artifacts, monkeypatch, race, expected):
    from starlette.requests import Request
    app, client, meeting_id, _, _ = artifacts
    original = Request.stream
    async def raced_stream(request):
        if request.url.path.endswith("/uploads"):
            with app.state.session_factory.begin() as session:
                if race == "delete":
                    session.get(Meeting, meeting_id).deleted_at = utcnow()
                    for row in session.scalars(select(Command).where(Command.meeting_id == meeting_id)):
                        row.gone, row.result = True, {}
                else:
                    session.scalar(select(Upload)).status = "failed"
        async for chunk in original(request):
            yield chunk
    monkeypatch.setattr(Request, "stream", raced_stream)
    body = b"private supplier detail"
    headers = {"Content-Type": "text/plain", "X-Command-Id": str(uuid4()), "X-Expected-Revision": "1", "X-Filename": "source.txt", "X-Content-SHA256": hashlib.sha256(body).hexdigest()}
    response = client.post(f"/api/meetings/{meeting_id}/uploads", content=body, headers=headers)
    assert response.status_code == expected, response.text
    with app.state.session_factory() as session:
        row = session.scalar(select(Upload))
        assert row.canonical_text is None and row.byte_size == 0
        if race == "delete":
            assert session.get(Command, ("owner_local", headers["X-Command-Id"])).result == {}


def test_attachment_count_reservation_precedes_body_read(artifacts):
    app, client, meeting_id, _, _ = artifacts
    with app.state.session_factory.begin() as session:
        for index in range(5):
            session.add(Upload(id=f"upload_reserved_{index}", meeting_id=meeting_id, owner_id="owner_local", filename="source.txt", media_type="text/plain", byte_size=1, status="temporary"))
    headers = {"Content-Type": "text/plain", "X-Command-Id": str(uuid4()), "X-Expected-Revision": "1", "X-Filename": "source.txt", "X-Content-SHA256": hashlib.sha256(b"x").hexdigest()}
    response = client.post(f"/api/meetings/{meeting_id}/uploads", content=b"x", headers=headers)
    assert response.status_code == 429 and response.json()["error"]["code"] == "quota_exceeded"


@pytest.mark.parametrize("kind", ["pending", "missing_artifact", "storage_full", "report_missing"])
def test_export_capacity_integrity_and_version_gates(artifacts, kind, monkeypatch):
    app, client, _, run_id, report = artifacts
    if kind != "report_missing":
        with app.state.session_factory.begin() as session:
            session.add(ExportRecord(id="export_reserved", run_id=run_id, owner_id="owner_local", report_version=1, format="json", status="pending" if kind == "pending" else "ready", path="export_" + "a" * 32 + ".bin"))
    if kind == "storage_full":
        from round_robin.reports.storage import ArtifactStore
        monkeypatch.setattr(ArtifactStore, "_path", lambda *args: SimpleNamespace(stat=lambda: SimpleNamespace(st_size=100 * 1024 * 1024)))
    response = client.post(f"/api/runs/{run_id}/exports", json=command(report_id="report_missing" if kind == "report_missing" else report["id"], format="json"))
    assert response.status_code == {"pending": 429, "missing_artifact": 503, "storage_full": 429, "report_missing": 409}[kind], response.text


def test_unready_or_expired_export_never_downloads(artifacts):
    app, client, _, run_id, report = artifacts
    assert client.get("/api/exports/export_missing/download").status_code == 404
    exported = client.post(f"/api/runs/{run_id}/exports", json=command(report_id=report["id"], format="json")).json()
    with app.state.session_factory.begin() as session:
        session.get(ExportRecord, exported["id"]).created_at = utcnow() - timedelta(days=8)
    response = client.get(exported["download_url"])
    assert response.status_code == 409 and response.json()["error"]["retry_scope"] == "export"


@pytest.mark.parametrize("race", ["owner_change", "auth_failure", "quarantine", "delete"])
def test_download_rechecks_identity_and_access_between_chunks(artifacts, race):
    from round_robin.api.artifacts import download_export
    from fastapi import HTTPException
    app, client, meeting_id, run_id, report = artifacts
    exported = client.post(f"/api/runs/{run_id}/exports", json=command(report_id=report["id"], format="json")).json()
    request = SimpleNamespace(app=app)
    async def consume():
        with app.state.session_factory() as session:
            response = download_export(exported["id"], request, owner="owner_local", session=session)
        if race in {"owner_change", "auth_failure"}:
            def identity(_):
                if race == "auth_failure":
                    raise HTTPException(401)
                return "owner_intruder"
            app.state.identity_provider = identity
        else:
            with app.state.session_factory.begin() as session:
                if race == "quarantine":
                    row = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
                    row.metadata_json = {"restore_quarantine": True}
                else:
                    session.get(Meeting, meeting_id).deleted_at = utcnow()
        chunks = [value async for value in response.body_iterator]
        assert chunks == []
    asyncio.run(consume())


def test_missing_calculations_do_not_fall_back_to_other_versions(artifacts):
    _, client, _, run_id, report = artifacts
    assert client.get(f"/api/runs/{run_id}/calculations/calculation_missing").status_code == 404
    assert client.get(f"/api/runs/{run_id}/calculations/{report['calculations'][0]['id']}?report_id=report_missing").status_code == 404


def test_owner_meeting_quota_and_bounded_pagination(client, application):
    first, second = create(client), create(client)
    page = client.get("/api/meetings?limit=1").json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    page2 = client.get("/api/meetings", params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert {page["items"][0]["id"], page2["items"][0]["id"]} == {first["id"], second["id"]}
    application.state.settings = replace(application.state.settings, max_meetings_per_owner=2)
    response = client.post("/api/meetings", json=command(brief=first["draft"]))
    assert response.status_code == 429
    assert client.get(f"/api/meetings/{first['id']}/runs?cursor=run_unknown").status_code == 400
    assert client.get(f"/api/commands/{uuid4()}").status_code == 404


def test_outcome_versions_are_owner_scoped_append_only_and_idempotent(artifacts):
    app, client, meeting_id, run_id, report = artifacts
    path = f"/api/meetings/{meeting_id}/outcomes"
    assert client.post(path, json=command(run_id="run_missing", report_version=1, choice="accepted")).status_code == 404
    assert client.post(path, json=command(run_id=run_id, report_version=2, choice="accepted")).status_code == 409
    payload = command(run_id=run_id, report_version=1, choice="modified", reason="Changed the supplier terms", success_criterion="Spend less than the agreed ceiling", observed_outcome="Awaiting delivery")
    one = client.post(path, json=payload)
    assert one.status_code == 201, one.text
    assert client.post(path, json=payload).json() == one.json()
    two = client.post(path, json={**payload, "command_id": str(uuid4()), "observed_outcome": "Delivered within budget"})
    assert two.json()["revision"] == 2
    history = client.get(path).json()["items"]
    assert [row["revision"] for row in history] == [1, 2]
    assert history[0]["payload"]["observed_outcome"] == "Awaiting delivery"


def test_cancel_terminal_run_does_not_add_work_and_conflict_is_explicit(client, application):
    from round_robin.database.models import Event, Outbox
    meeting = create(client)
    run = start(client, meeting).json()
    assert client.post(f"/api/runs/{run['id']}/cancel", json=command(expected_run_version=99)).status_code == 409
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        row.status = "completed"
    response = client.post(f"/api/runs/{run['id']}/cancel", json=command(expected_run_version=1))
    assert response.status_code == 200 and response.json()["status"] == "completed"
    with application.state.session_factory() as session:
        assert len(list(session.scalars(select(Event)))) == 1
        assert len(list(session.scalars(select(Outbox)))) == 1


@pytest.mark.parametrize("mutation,code", [("expired", "input_expired"), ("incomplete", "brief_incomplete"), ("scope", "input_scope_invalid")])
def test_clarification_cannot_renew_deadline_or_expand_approved_scope(client, application, mutation, code):
    meeting = create(client)
    run = start(client, meeting).json()
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        row.status = "awaiting_input"
        row.input_expires_at = utcnow() + timedelta(minutes=-1 if mutation == "expired" else 1)
    brief = dict(meeting["draft"])
    if mutation == "incomplete":
        brief["options"] = []
    if mutation == "scope":
        brief["public_research_terms"] = [{"id": "term_new", "text": "previously private"}]
    response = client.post(f"/api/runs/{run['id']}/clarifications", json=command(expected_run_version=1, brief=brief))
    assert response.status_code == 409 and response.json()["error"]["code"] == code
    with application.state.session_factory() as session:
        row = session.get(Run, run["id"])
        assert row.input_snapshot == meeting["draft"] and row.status == "awaiting_input"


def test_preflight_conflict_missing_start_and_queue_ceiling(client, application):
    meeting = create(client)
    assert client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=2)).status_code == 409
    assert client.post(f"/api/meetings/{meeting['id']}/runs", json=command(preflight_id="preflight_absent", expected_draft_revision=1)).status_code == 409
    application.state.settings = replace(application.state.settings, max_queued_per_owner=0)
    response = start(client, meeting)
    assert response.status_code == 429
    with application.state.session_factory() as session:
        assert session.scalar(select(Run)) is None


def test_expired_export_reservation_cannot_be_resurrected(artifacts, monkeypatch):
    app, client, _, run_id, report = artifacts
    from round_robin.api import artifacts as routes
    original = routes.render_saved_report
    def expire_during_render(*args, **kwargs):
        result = original(*args, **kwargs)
        with app.state.session_factory.begin() as session:
            row = session.scalar(select(ExportRecord))
            row.status, row.error_code = "failed", "export_reservation_expired"
        return result
    monkeypatch.setattr(routes, "render_saved_report", expire_during_render)
    response = client.post(f"/api/runs/{run_id}/exports", json=command(report_id=report["id"], format="json"))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["download_url"] is None
    assert not list(app.state.settings.artifact_directory.glob("*.bin"))
