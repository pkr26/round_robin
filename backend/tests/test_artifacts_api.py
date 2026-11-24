from dataclasses import replace
import hashlib
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Command, EvidenceRecord, ExportRecord, Meeting, ReportVersion, Run, utcnow
from round_robin.fixtures import sample_bundle
from round_robin.schemas.runtime import RuntimeDecisionReport
from round_robin.schemas.views import StoredEvidence


@pytest.fixture
def artifacts(tmp_path, request):
    app = create_app(f"sqlite:///{tmp_path / 'artifacts_test.sqlite'}", settings=replace(Settings(), artifact_directory=tmp_path / "private"), initialize=True)
    with TestClient(app, raise_server_exceptions=getattr(request, "param", True)) as client:
        bundle = sample_bundle()
        meeting = client.post("/api/meetings", json={"command_id": str(uuid4()), "brief": bundle.brief.model_dump(mode="json")}).json()
        meeting_id = meeting["id"]
        preflight = client.post(f"/api/meetings/{meeting_id}/preflight", json={"command_id": str(uuid4()), "expected_draft_revision": 1, "mode": "simulated"}).json()
        run = client.post(f"/api/meetings/{meeting_id}/runs", json={"command_id": str(uuid4()), "preflight_id": preflight["id"], "expected_draft_revision": 1}).json()
        run_id = run["id"]
        saved = bundle.scenarios[0].report.model_dump(mode="json")
        saved.update(id="report_" + uuid4().hex, run_id=run_id, simulated=True, readiness='conditional', readiness_reasons=['Simulated assessments cannot establish a real ready choice.'], evidence_ids=[item.id for item in bundle.evidence[:3]], contribution_ids=[], research_summary={'status':'not_requested','queries':0,'pages':0}, usage={'simulated':True,'status':'reported','actual_cost':'0','conservative_exposure_usd':'0','unknown_operation_ids':[],'request_count':0,'currency':'USD'})
        saved['essential_dependencies'].append({'id':'dependency_actual_review','description':'Real independent review remains unverified.','status':'unverified','accepted_scoped_support':False,'reference_ids':[],'origin':'user_supplied'})
        saved = RuntimeDecisionReport.model_validate(saved).model_dump(mode='json')
        with app.state.session_factory.begin() as session:
            row = session.get(Run, run_id)
            row.status, row.report_version = "completed", 1
            session.add(ReportVersion(id=saved["id"], run_id=run_id, version=1, event_cutoff=saved["event_cutoff"], content=saved))
            for source in bundle.evidence[:3]:
                content = {key: value for key, value in source.model_dump(mode="json").items() if key in StoredEvidence.model_fields}
                content.update(run_id=run_id, owner_id="owner_local", origin="user_supplied", verification_status="user_supplied")
                content = StoredEvidence.model_validate(content).model_dump(mode="json", exclude_unset=True)
                session.add(EvidenceRecord(id=content["id"], run_id=run_id, meeting_id=meeting_id, owner_id="owner_local", sha256=content["sha256"], content=content))
        yield app, client, meeting_id, run_id, saved


def test_exports_are_bound_to_exact_saved_version_and_idempotent(artifacts):
    app, client, meeting, run, report = artifacts
    command = {"command_id": str(uuid4()), "report_id": report["id"], "format": "json"}
    response = client.post(f"/api/runs/{run}/exports", json=command)
    assert response.status_code == 200, response.text
    export = response.json()
    assert export["status"] == "ready"
    assert client.post(f"/api/runs/{run}/exports", json=command).json() == export
    assert client.get(export["download_url"]).json() == report
    with app.state.session_factory() as session:
        assert len(list(session.scalars(select(ExportRecord)))) == 1
    changed = {**command, "format": "pdf"}
    assert client.post(f"/api/runs/{run}/exports", json=changed).status_code == 409


def test_source_and_calculation_lookup_are_scoped_to_saved_run(artifacts):
    _, client, _, run, report = artifacts
    assert client.get(f"/api/runs/{run}/sources/passage_offer_a").json()["text"].startswith("Option A")
    assert client.get(f"/api/runs/{run}/sources/foreign_run_source").status_code == 404
    calculation = client.get(f"/api/runs/{run}/calculations/{report['calculations'][0]['id']}?report_id={report['id']}")
    assert calculation.status_code == 200 and calculation.json()["report_id"] == report["id"]
    assert client.get(f"/api/runs/foreign_run/calculations/{report['calculations'][0]['id']}").status_code == 404


def test_missing_and_foreign_run_return_identical_usable_not_found(artifacts, monkeypatch):
    app, client, _, run_id, _ = artifacts
    missing = client.get(f"/api/runs/run_{uuid4().hex}/sources")
    monkeypatch.setitem(app.dependency_overrides, require_owner, lambda: "other_owner")
    foreign = client.get(f"/api/runs/{run_id}/sources")
    assert missing.status_code == foreign.status_code == 404
    assert missing.json()["error"] == foreign.json()["error"]
    assert missing.json()["error"]["code"] == "not_found"
    assert missing.json()["error"]["message"] == "Run not found"


@pytest.mark.parametrize("artifacts", [False], indirect=True)
def test_corrupt_artifact_does_not_claim_ready_download_or_restart_review(artifacts):
    app, client, _, run, report = artifacts
    result = client.post(f"/api/runs/{run}/exports", json={"command_id": str(uuid4()), "report_id": report["id"], "format": "json"})
    assert result.status_code == 200, result.text
    exported = result.json()
    assert exported["status"] == "ready"
    url = exported["download_url"]
    assert isinstance(url, str) and url.startswith("/api/exports/") and url.endswith("/download")
    with app.state.session_factory() as session:
        row = session.get(ExportRecord, exported["id"])
        (app.state.settings.artifact_directory / row.path).write_bytes(b"corrupt")
    response = client.get(url)
    assert response.status_code == 503 and response.json()["error"]["retry_scope"] == "export"


def test_renderer_failure_is_saved_and_retry_is_only_an_export(artifacts, monkeypatch):
    app, client, _, run, report = artifacts
    from round_robin.api import artifacts as routes
    real = routes.render_saved_report
    monkeypatch.setattr(routes, "render_saved_report", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private renderer details")))
    result = client.post(f"/api/runs/{run}/exports", json={"command_id": str(uuid4()), "report_id": report["id"], "format": "pdf"}).json()
    assert result["status"] == "failed" and result["download_url"] is None
    assert "private renderer" not in json.dumps(result)
    monkeypatch.setattr(routes, "render_saved_report", real)
    retried = client.post(f"/api/runs/{run}/exports", json={"command_id": str(uuid4()), "report_id": report["id"], "format": "pdf"}).json()
    assert retried["status"] == "ready" and retried["report_version"] == 1


def test_malformed_saved_report_cannot_be_exported_as_ready(artifacts):
    app, client, _, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        row = session.get(ReportVersion, report['id'])
        row.content = {**row.content, 'readiness': 'ready_to_choose'}
    response = client.post(f'/api/runs/{run_id}/exports', json={'command_id':str(uuid4()),'report_id':report['id'],'format':'json'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'report_invalid'
    with app.state.session_factory() as session:
        assert not list(session.scalars(select(ExportRecord)))


def test_meeting_deletion_revokes_existing_download_immediately(artifacts):
    _, client, meeting, run, report = artifacts
    result = client.post(f"/api/runs/{run}/exports", json={"command_id": str(uuid4()), "report_id": report["id"], "format": "json"}).json()
    assert client.request("DELETE", f"/api/meetings/{meeting}", json={"command_id": str(uuid4())}).status_code == 200
    assert client.get(result["download_url"]).status_code == 410


def test_deletion_during_renderer_fences_late_artifact_commit(artifacts, monkeypatch):
    app, client, meeting_id, run_id, report = artifacts
    from round_robin.api import artifacts as routes
    real = routes.render_saved_report
    def late_render(*args, **kwargs):
        content = real(*args, **kwargs)
        with app.state.session_factory.begin() as session:
            meeting = session.get(Meeting, meeting_id)
            meeting.deleted_at = utcnow()
            meeting.execution_fence += 1
            session.get(Run, run_id).execution_fence += 1
        return content
    monkeypatch.setattr(routes, 'render_saved_report', late_render)
    result = client.post(f'/api/runs/{run_id}/exports', json={'command_id': str(uuid4()), 'report_id': report['id'], 'format': 'json'}).json()
    assert result['status'] == 'failed' and result['download_url'] is None
    assert not list(app.state.settings.artifact_directory.glob('*.bin'))


def test_text_upload_hashes_canonical_bytes_and_enforces_revision(artifacts):
    _, client, meeting, _, _ = artifacts
    raw = b"Line one\r\nLine two\n"
    canonical = b"Line one\nLine two\n"
    headers = {"Content-Type": "text/plain", "X-Command-Id": str(uuid4()), "X-Expected-Revision": "1", "X-Filename": "supplier.txt", "X-Content-SHA256": hashlib.sha256(canonical).hexdigest()}
    response = client.post(f"/api/meetings/{meeting}/uploads", content=raw, headers=headers)
    assert response.status_code == 201, response.text
    record = response.json()
    assert {"id", "meeting_id", "status", "filename", "sha256", "byte_size", "recipients"} <= record.keys()
    assert isinstance(record["id"], str) and record["id"]
    assert record["meeting_id"] == meeting and record["filename"] == "supplier.txt"
    assert record["sha256"] == hashlib.sha256(canonical).hexdigest()
    assert record["status"] == "ready" and record["byte_size"] == len(canonical)
    assert set(record["recipients"]) == {"openai", "anthropic", "google"}
    assert client.post(f"/api/meetings/{meeting}/uploads", content=raw, headers=headers).json() == record
    invalid = {**headers, "X-Command-Id": str(uuid4()), "X-Content-SHA256": "0" * 64}
    assert client.post(f"/api/meetings/{meeting}/uploads", content=raw, headers=invalid).status_code == 422
    stale = {**headers, "X-Command-Id": str(uuid4()), "X-Expected-Revision": "99"}
    assert client.post(f"/api/meetings/{meeting}/uploads", content=raw, headers=stale).status_code == 409
