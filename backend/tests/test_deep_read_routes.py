"""Exercise HTTP read/revocation boundaries that service-level tests bypassed."""
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from backend.tests.test_artifacts_api import artifacts
from backend.tests.test_hosted import hosted, session_client
from round_robin.api.auth import get_db, require_owner
from round_robin.database.models import Attempt, BudgetOperation, Command, EvidenceRecord, Event, Outbox, ReportVersion, Run
from round_robin.fixture_api import app as fixture_app
from round_robin.fixtures import SampleBundle, sample_bundle
from round_robin.security.hosted import SESSION_COOKIE


def test_source_pages_are_complete_scoped_and_read_only(artifacts):
    app, client, _, run_id, _ = artifacts
    meeting = client.post("/api/meetings", json={"command_id": str(uuid4()), "brief": sample_bundle().brief.model_dump(mode="json")}).json()
    preflight = client.post(f"/api/meetings/{meeting['id']}/preflight", json={"command_id": str(uuid4()), "expected_draft_revision": 1, "mode": "simulated"}).json()
    other_run = client.post(f"/api/meetings/{meeting['id']}/runs", json={"command_id": str(uuid4()), "preflight_id": preflight["id"], "expected_draft_revision": 1}).json()
    with app.state.session_factory.begin() as session:
        for source_id, target_run, owner in [("passage_foreign_run", other_run["id"], "owner_local"), ("passage_foreign_owner", run_id, "other_owner")]:
            content = {**sample_bundle().evidence[0].model_dump(mode="json"), "id": source_id, "run_id": target_run, "owner_id": owner}
            session.add(EvidenceRecord(id=source_id, run_id=target_run, meeting_id=meeting["id"], owner_id=owner, sha256=content["sha256"], content=content))
    def snapshot():
        with app.state.session_factory() as session:
            return json.dumps({model.__tablename__: [
                {column.name: getattr(row, column.name) for column in model.__table__.columns}
                for row in session.scalars(select(model).order_by(*model.__table__.primary_key.columns))
            ] for model in (Command, BudgetOperation, Attempt, Outbox, Run)}, sort_keys=True, default=str)
    before = snapshot()
    first = client.get(f"/api/runs/{run_id}/sources", params={"limit": 0})
    assert first.status_code == 200 and len(first.json()["items"]) == 1
    items, cursor, seen_cursors = [], None, set()
    for _ in range(5):
        response = client.get(f"/api/runs/{run_id}/sources", params={"limit": 1, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        page = response.json()
        items.extend(item["id"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] not in seen_cursors
        cursor = page["next_cursor"]
        seen_cursors.add(cursor)
    else:
        raise AssertionError("Pagination did not terminate within the seeded source count")
    assert items == sorted(source.id for source in sample_bundle().evidence[:3])
    assert client.get(f"/api/runs/{run_id}/sources?cursor=zzzz").json() == {"items": [], "next_cursor": None}
    assert client.get(f"/api/runs/{run_id}/sources?limit=invalid").status_code == 422
    app.dependency_overrides[require_owner] = lambda: "other_owner"
    assert client.get(f"/api/runs/{run_id}/sources").status_code == 404
    assert client.get(f"/api/runs/{run_id}/measurements").status_code == 404
    app.dependency_overrides.clear()
    assert snapshot() == before


def test_measurements_use_saved_clock_samples_and_never_promote_failed_run(artifacts):
    app, client, _, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        sequence = session.scalar(select(func.max(Event.sequence)).where(Event.run_id == run_id)) or 0
        for offset, (kind, payload) in enumerate([
            ("contribution.recorded", {"qualifying_content": True, "active_seconds": 23}),
            ("report.published", {"mandatory_work_complete": True, "active_seconds": 91}),
        ], start=1):
            session.add(Event(id="event_" + uuid4().hex, run_id=run_id, sequence=sequence + offset, event_type=kind, payload=payload))
    response = client.get(f"/api/runs/{run_id}/measurements")
    assert response.status_code == 200, response.text
    assert response.json()["first_useful_active_seconds"] == 23
    assert response.json()["mandatory_completion_active_seconds"] == 91
    assert response.json()["deadline_attainment"] == {"first_useful_within_60": True, "complete_within_120": True, "complete_within_300": True}
    with app.state.session_factory.begin() as session:
        session.get(Run, run_id).status = "failed"
    failed = client.get(f"/api/runs/{run_id}/measurements").json()
    assert failed["mandatory_work_complete"] is False
    assert not any(failed["deadline_attainment"].values())


def test_export_status_is_private_and_disappearing_saved_version_is_gone(artifacts):
    app, client, _, run_id, report = artifacts
    exported = client.post(f"/api/runs/{run_id}/exports", json={"command_id": str(uuid4()), "report_id": report["id"], "format": "json"}).json()
    path = f"/api/exports/{exported['id']}"
    assert client.get(path).json() == exported
    assert client.get("/api/exports/nonexistent").status_code == 404
    app.dependency_overrides[require_owner] = lambda: "other_owner"
    assert client.get(path).status_code == 404
    app.dependency_overrides.clear()
    with app.state.session_factory.begin() as session:
        session.delete(session.get(ReportVersion, report["id"]))
    response = client.get(path)
    assert response.status_code == 410 and response.json()["error"]["code"] == "report_unavailable"


def test_hosted_logout_requires_csrf_then_revokes_replayed_cookie(hosted):
    client, token, csrf, _ = session_client(hosted)
    with client:
        assert client.post("/auth/logout", follow_redirects=False).status_code == 403
        assert client.get("/auth/session").status_code == 200
        response = client.post("/auth/logout", headers={"x-csrf-token": csrf, "origin": "https://app.example"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "https://app.example"
        cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith(SESSION_COOKIE + "="))
        assert "Max-Age=0" in cookie and "Secure" in cookie and "HttpOnly" in cookie
        client.cookies.set(SESSION_COOKIE, token, domain="app.example", path="/")
        assert client.get("/auth/session").status_code == 401
        assert client.get("/api/meetings").status_code == 401


def test_actual_api_database_failure_preserves_unknown_outcome_without_private_sql(artifacts):
    app, client, _, _, _ = artifacts
    def broken_db():
        raise OperationalError("PRIVATE_SQL_STATEMENT", {"secret": "PRIVATE_SQL_VALUE"}, RuntimeError("PRIVATE_DRIVER_DETAILS"))
    app.dependency_overrides[get_db] = broken_db
    response = client.get("/api/meetings")
    app.dependency_overrides.clear()
    assert response.status_code == 503, response.text
    assert response.json()["error"]["command_outcome"] == "unknown"
    assert response.json()["error"]["retry_scope"] == "original_command"
    assert "PRIVATE_" not in response.text


def test_actual_sample_and_fixture_schema_routes_preserve_contracts(artifacts):
    _, client, _, _, _ = artifacts
    actual = client.get("/api/sample")
    assert actual.status_code == 200
    SampleBundle.model_validate(actual.json())
    with TestClient(fixture_app) as fixture:
        assert fixture.get("/api/sample").json() == actual.json()
        for schema in ("brief", "sample"):
            response = fixture.get(f"/api/schemas/{schema}")
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert response.json()["type"] == "object" and "properties" in response.json()
            assert fixture.post(f"/api/schemas/{schema}", json={}).status_code == 405
        assert fixture.get("/api/schemas/sample", headers={"origin": "https://outside.example"}).status_code == 403


@pytest.mark.parametrize("corruption", ["owner_id", "run_id", "row_hash"])
def test_corrupt_saved_support_cannot_retain_readiness_or_escape_snapshot_binding(artifacts, corruption):
    app, client, meeting_id, run_id, report = artifacts
    assert client.get(f"/api/runs/{run_id}").json()["readiness"] == "conditional"
    with app.state.session_factory.begin() as session:
        row = session.get(EvidenceRecord, report["evidence_ids"][0])
        if corruption == "row_hash":
            row.sha256 = "f" * 64
        else:
            row.content = {**row.content, corruption: "owner_different" if corruption == "owner_id" else "run_different"}
    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "saved_result_unavailable"
    for path in (f"/api/runs/{run_id}/report", f"/api/runs/{run_id}/reports/1"):
        response = client.get(path)
        assert response.status_code == 503, response.text
        assert "owner_different" not in response.text and "run_different" not in response.text
    # The run itself remains inspectable, including its terminal state. Its
    # public readiness must not certify unreadable/corrupt saved support.
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200 and response.json()["readiness"] is None
    assert client.get(f"/api/meetings/{meeting_id}").json()["runs"][0]["readiness"] is None
    exported = client.post(f"/api/runs/{run_id}/exports", json={"command_id": str(uuid4()), "report_id": report["id"], "format": "json"})
    assert exported.status_code == 409 and exported.json()["error"]["code"] == "report_invalid"
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(BudgetOperation)) == 0
