from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from round_robin.app import create_app
from round_robin.api.auth import require_owner
from round_robin.configuration import Settings
from round_robin.database.models import Command, Draft, Event, PreflightRecord, ReportVersion, Run, Step, utcnow
from round_robin.database.services import append_event
from round_robin.fixtures import sample_brief


@pytest.fixture
def application(tmp_path):
    settings = replace(Settings(), artifact_directory=tmp_path / "artifacts")
    return create_app(f"sqlite:///{tmp_path / 'api.db'}", settings=settings, initialize=True)


@pytest.fixture
def client(application):
    with TestClient(application) as connection:
        yield connection


def command(**fields):
    return {"command_id": str(uuid4()), **fields}


def create(client, brief=None):
    response = client.post("/api/meetings", json=command(brief=brief or sample_brief().model_dump(mode="json")))
    assert response.status_code == 201, response.text
    return response.json()


def preflight(client, meeting):
    response = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=meeting["draft_revision"]))
    assert response.status_code == 200, response.text
    return response.json()


def start(client, meeting, quote=None, payload=None):
    quote = quote or preflight(client, meeting)
    return client.post(f"/api/meetings/{meeting['id']}/runs", json=payload or command(preflight_id=quote["id"], expected_draft_revision=meeting["draft_revision"]))


def test_draft_save_is_persistent_revision_checked_and_performs_no_work(client, application):
    meeting = create(client)
    edited = meeting["draft"]
    edited["budget"] = "900"
    edited["title"] = "Renamed comparison"
    payload = command(expected_revision=1, brief=edited)
    response = client.patch(f"/api/meetings/{meeting['id']}/draft", json=payload)
    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert client.patch(f"/api/meetings/{meeting['id']}/draft", json=payload).json() == response.json()
    assert client.patch(f"/api/meetings/{meeting['id']}/draft", json=command(expected_revision=1, brief=edited)).status_code == 409
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0
        assert session.scalar(select(func.count()).select_from(Step)) == 0
    assert client.get(f"/api/meetings/{meeting['id']}/draft").json()["brief"]["budget"] == "900"
    assert client.get(f"/api/meetings/{meeting['id']}").json()["title"] == "Renamed comparison"
    assert client.get("/api/meetings").json()["items"][0]["title"] == "Renamed comparison"


def test_incomplete_draft_can_save_and_preflight_warns(client):
    brief = sample_brief().model_dump(mode="json")
    brief.update(options=[], horizon_months=None, budget=None)
    quote = preflight(client, create(client, brief))
    assert quote["warnings"]
    assert quote["blockers"] == []


def test_start_idempotency_single_use_preflight_and_command_conflict(client, application):
    meeting = create(client)
    quote = preflight(client, meeting)
    payload = command(preflight_id=quote["id"], expected_draft_revision=1)
    response = start(client, meeting, quote, payload)
    assert response.status_code == 202, response.text
    assert start(client, meeting, quote, payload).json() == response.json()
    assert start(client, meeting, quote).status_code == 409
    assert client.get(f"/api/commands/{payload['command_id']}").json()["outcome"] == "committed"
    assert start(client, meeting, quote, {**payload, "expected_draft_revision": 2}).status_code == 409
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 1


def test_stale_or_expired_preflight_does_not_start(client, application):
    meeting = create(client)
    quote = preflight(client, meeting)
    with application.state.session_factory.begin() as session:
        row = session.get(PreflightRecord, quote["id"])
        row.expires_at = utcnow() - timedelta(seconds=1)
    response = start(client, meeting, quote)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "preflight_expired"
    quote = preflight(client, meeting)
    changed = {**meeting["draft"], "title": "Changed after preflight"}
    assert client.patch(f"/api/meetings/{meeting['id']}/draft", json=command(expected_revision=1, brief=changed)).status_code == 200
    assert start(client, meeting, quote).json()["error"]["code"] == "preflight_stale"


def test_live_mode_is_blocked_without_funding_regardless_of_keys(client):
    meeting = create(client)
    quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1, mode="live")).json()
    assert quote["blockers"]
    assert start(client, meeting, quote).json()["error"]["code"] == "spending_locked"


def test_owner_isolation_on_routes_and_command_lookup(client, application):
    meeting = create(client)
    run = start(client, meeting).json()
    application.dependency_overrides[require_owner] = lambda: "owner_other"
    assert client.get(f"/api/meetings/{meeting['id']}").status_code == 404
    assert client.get(f"/api/runs/{run['id']}/snapshot").status_code == 404
    application.dependency_overrides.clear()


def test_cancel_commit_fences_new_work_and_is_idempotent(client, application):
    meeting = create(client)
    run = start(client, meeting).json()
    payload = command(expected_run_version=run["version"])
    response = client.post(f"/api/runs/{run['id']}/cancel", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "cancel_requested"
    assert client.post(f"/api/runs/{run['id']}/cancel", json=payload).json() == response.json()
    with application.state.session_factory() as session:
        assert session.get(Run, run["id"]).execution_fence == 1


def test_events_snapshot_and_sse_replay_preserve_commit_order(client, application):
    meeting = create(client)
    run = start(client, meeting).json()
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        append_event(session, row, "content.committed", {"record_id": "message_one"})
        append_event(session, row, "run.completed", {})
        row.status = "completed"
    response = client.get(f"/api/runs/{run['id']}/events?after=1").json()
    assert [item["sequence"] for item in response["items"]] == [2, 3]
    stream = client.get(f"/api/runs/{run['id']}/stream", headers={"last-event-id": f"{run['id']}:1"})
    assert f"id: {run['id']}:2" in stream.text
    assert f"id: {run['id']}:1\n" not in stream.text


@pytest.mark.parametrize("invalidation", ["deletion", "session_revoked", "restore_epoch"])
def test_sse_rechecks_authority_between_events_in_one_batch(client, application, invalidation):
    import asyncio
    from types import SimpleNamespace
    from fastapi import HTTPException
    from round_robin.api.events import stream_events
    from round_robin.database.models import Meeting, PolicyBucket

    meeting = create(client)
    run = start(client, meeting).json()
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        owner = row.owner_id
        append_event(session, row, "content.committed", {"record_id": "protected_later_event"})
        row.status = "completed"
    revoked = False

    def identity(request):
        if revoked:
            raise HTTPException(401, "Session revoked")
        return owner

    application.state.identity_provider = identity

    async def connected():
        return False

    async def exercise():
        nonlocal revoked
        request = SimpleNamespace(app=application, is_disconnected=connected)
        response = await stream_events(run["id"], request, after=0, last_event_id=None, owner=owner)
        assert f"id: {run['id']}:1" in await anext(response.body_iterator)
        with application.state.session_factory.begin() as session:
            if invalidation == "deletion":
                session.get(Meeting, meeting["id"]).deleted_at = utcnow()
            elif invalidation == "restore_epoch":
                policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
                policy.metadata_json = {**policy.metadata_json, "deployment_epoch": 2}
            else:
                revoked = True
        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)

    asyncio.run(exercise())


def test_clarification_freezes_once_and_never_reschedules_intake(client, application):
    meeting = create(client)
    run = start(client, meeting).json()
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        row.status, row.input_expires_at = "awaiting_input", utcnow() + timedelta(hours=24)
        row.configuration = {**row.configuration, "questions": ["Confirm your horizon"]}
    payload = command(expected_run_version=run["version"], brief=meeting["draft"])
    result = client.post(f"/api/runs/{run['id']}/clarifications", json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["stage"] == "proposals"
    assert client.post(f"/api/runs/{run['id']}/clarifications", json=payload).json() == result.json()
    assert client.post(f"/api/runs/{run['id']}/clarifications", json=command(expected_run_version=1, brief=meeting["draft"])).status_code == 409


def test_delete_preserves_tombstones_and_cannot_recreate_paid_work(client, application):
    meeting = create(client)
    quote = preflight(client, meeting)
    payload = command(preflight_id=quote["id"], expected_draft_revision=1)
    run = start(client, meeting, quote, payload).json()
    result = client.request("DELETE", f"/api/meetings/{meeting['id']}", json=command())
    assert result.status_code == 200, result.text
    assert client.get(f"/api/runs/{run['id']}").status_code == 410
    assert start(client, meeting, quote, payload).status_code == 410
    assert client.get(f"/api/commands/{payload['command_id']}").json()["outcome"] == "gone"
    with application.state.session_factory() as session:
        assert session.get(Draft, meeting["id"]) is None
        assert session.get(Run, run["id"]).input_snapshot == {}
    assert (application.state.settings.artifact_directory.parent / "deletion-manifest.jsonl").exists()


def test_local_origin_unknown_fields_and_json_body_size_fail_closed(client):
    assert client.get("/api/meetings", headers={"origin": "https://outside.example"}).status_code == 403
    assert client.get("/api/meetings", headers={"x-forwarded-for": "203.0.113.1"}).status_code == 403
    assert client.post("/api/meetings", content=b"x" * (128 * 1024 + 1), headers={"content-type": "application/json"}).status_code == 413
    assert client.post("/api/meetings", json={**command(brief=sample_brief().model_dump(mode="json")), "owner_id": "owner_attacker"}).status_code == 422


def test_run_history_is_bounded_and_paginates_without_duplicates(client, application):
    meeting = create(client)
    for _ in range(23):
        run = start(client, meeting).json()
        with application.state.session_factory.begin() as session:
            session.get(Run, run["id"]).status = "cancelled"
    overview = client.get(f"/api/meetings/{meeting['id']}").json()
    assert len(overview["runs"]) == 20
    assert overview["next_run_cursor"]
    identities, cursor = [], None
    while True:
        query = f"/api/meetings/{meeting['id']}/runs?limit=7" + (f"&cursor={cursor}" if cursor else "")
        response = client.get(query)
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) <= 7
        identities.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(identities) == len(set(identities)) == 23


def test_unreadable_saved_report_preserves_original_without_private_validation_leak(client, application, caplog):
    meeting = create(client)
    run = start(client, meeting).json()
    original = {"simulated": True, "private_unexpected": "PRIVATE_VALIDATION_INPUT_SENTINEL"}
    with application.state.session_factory.begin() as session:
        session.add(ReportVersion(id="report_invalid_private", run_id=run["id"], version=1, event_cutoff=1, content=original))
    response = client.get(f"/api/runs/{run['id']}/report")
    assert response.status_code == 503 and response.json()["error"]["code"] == "saved_result_unavailable"
    assert "PRIVATE_VALIDATION_INPUT_SENTINEL" not in response.text + caplog.text
    with application.state.session_factory() as session:
        assert session.get(ReportVersion, "report_invalid_private").content == original


def test_response_contract_failure_is_sanitized_before_asgi_traceback_logging(client, application, caplog):
    from round_robin.schemas.runtime import RuntimeDecisionReport

    @application.get("/api/test-private-response", response_model=RuntimeDecisionReport)
    def private_response():
        return {"private": "PRIVATE_RESPONSE_INPUT_SENTINEL"}

    response = client.get("/api/test-private-response")
    assert response.status_code == 503 and response.json()["error"]["code"] == "saved_result_unavailable"
    assert "PRIVATE_RESPONSE_INPUT_SENTINEL" not in response.text + caplog.text
