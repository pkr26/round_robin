"""Regression cases discovered by the November 2026 adversarial audit."""
from dataclasses import replace
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Command, Event, Meeting, Run, Step
from round_robin.database.services import append_event
from round_robin.fixtures import sample_brief


@pytest.fixture
def application(tmp_path):
    return create_app(f"sqlite:///{tmp_path / 'audit.db'}", settings=replace(Settings(), artifact_directory=tmp_path / "artifacts"), initialize=True)


@pytest.fixture
def client(application):
    with TestClient(application, raise_server_exceptions=False) as connection:
        yield connection


def command(**fields):
    return {"command_id": str(uuid4()), **fields}


def create_run(client):
    response = client.post("/api/meetings", json=command(brief=sample_brief().model_dump(mode="json")))
    assert response.status_code == 201, response.text
    meeting = response.json()
    quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1)).json()
    response = client.post(f"/api/meetings/{meeting['id']}/runs", json=command(preflight_id=quote["id"], expected_draft_revision=1))
    assert response.status_code == 202, response.text
    return meeting, response.json()


@pytest.mark.parametrize("text", ["\ud800", "\udfff", "invalid\x00text"])
@pytest.mark.parametrize("field", ["title", "background"])
def test_invalid_database_text_is_rejected_before_command_commit(client, application, text, field):
    brief = sample_brief().model_dump(mode="json")
    brief[field] = text
    # ASCII JSON escapes are valid at the transport boundary, including lone
    # surrogates that cannot be encoded into canonical UTF-8 or PostgreSQL text.
    response = client.post("/api/meetings", content=json.dumps(command(brief=brief)), headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Meeting)) == 0
        assert session.scalar(select(func.count()).select_from(Command)) == 0


@pytest.mark.parametrize("term", ["   ", "\n", "supplier\tname", "supplier\rname"])
def test_public_terms_rejected_at_intake_instead_of_stranding_worker(client, term):
    brief = sample_brief().model_dump(mode="json")
    brief["public_research_terms"] = [{"id": "term_one", "text": term}]
    response = client.post("/api/meetings", json=command(brief=brief))
    assert response.status_code == 422, response.text


def test_duplicate_selected_attachment_ids_are_rejected(client):
    brief = sample_brief().model_dump(mode="json")
    brief["selected_attachment_ids"] = ["upload_one", "upload_one"]
    response = client.post("/api/meetings", json=command(brief=brief))
    assert response.status_code == 422, response.text


def add_failed_step(session, run_id, stage, error, *, calls=1, iteration=0):
    step = Step(id=f"step_{uuid4().hex}", run_id=run_id, stage=stage, role_slot_id=stage, provider="openai", status="failed", calls=calls, infrastructure_claims=1, error_code=error, stage_iteration=iteration)
    session.add(step)
    return step


def test_retry_does_not_use_other_stages_eligibility_to_repeat_refusal(client, application):
    _, run = create_run(client)
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        row.status = "completed_with_limitations"
        refused = add_failed_step(session, row.id, "synthesis", "refusal")
        refused_id = refused.id
        add_failed_step(session, row.id, "audit", "timeout")
    assert client.get(f"/api/runs/{run['id']}/snapshot").json()["retryable_stages"] == ["audit"]
    response = client.post(f"/api/runs/{run['id']}/retries", json=command(expected_run_version=run["version"], stage="synthesis"))
    assert response.status_code == 409, response.text
    with application.state.session_factory() as session:
        assert session.get(Step, refused_id).status == "failed"
        assert session.get(Run, run["id"]).recovery_generation == 0


def test_historical_failed_stage_cannot_reopen_after_later_iteration(client, application):
    _, run = create_run(client)
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        row.status = "completed_with_limitations"
        row.configuration = {**row.configuration, "stage_iteration": 1}
        add_failed_step(session, row.id, "synthesis", "timeout", iteration=0)
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["retry_eligible"] is False
    response = client.post(f"/api/runs/{run['id']}/retries", json=command(expected_run_version=run["version"], stage="synthesis"))
    assert response.status_code == 409, response.text


def test_retry_honors_lower_original_call_limit(client, application):
    _, run = create_run(client)
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        row.status = "completed_with_limitations"
        row.configuration = {**row.configuration, "limits": {**row.configuration["limits"], "max_calls_per_step": 1}}
        add_failed_step(session, row.id, "synthesis", "timeout")
    response = client.get(f"/api/runs/{run['id']}/snapshot")
    assert response.status_code == 200, response.text
    assert response.json()["retry_eligible"] is False
    response = client.post(f"/api/runs/{run['id']}/retries", json=command(expected_run_version=run["version"], stage="synthesis"))
    assert response.status_code == 409, response.text


def test_retryable_current_stage_can_still_resume_once_idempotently(client, application):
    _, run = create_run(client)
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        row.status = "completed_with_limitations"
        add_failed_step(session, row.id, "synthesis", "timeout")
    payload = command(expected_run_version=run["version"], stage="synthesis")
    response = client.post(f"/api/runs/{run['id']}/retries", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert client.post(f"/api/runs/{run['id']}/retries", json=payload).json() == response.json()
    with application.state.session_factory() as session:
        assert session.get(Run, run["id"]).recovery_generation == 1


def test_stale_follow_up_cannot_overwrite_a_newer_draft(client, application):
    meeting, run = create_run(client)
    with application.state.session_factory.begin() as session:
        session.get(Run, run["id"]).status = "completed"
    newer = {**meeting["draft"], "title": "A newer saved draft"}
    saved = client.patch(f"/api/meetings/{meeting['id']}/draft", json=command(expected_revision=1, brief=newer))
    assert saved.status_code == 200, saved.text
    payload = command(parent_run_id=run["id"], expected_draft_revision=1, brief=meeting["draft"])
    response = client.post(f"/api/meetings/{meeting['id']}/follow-ups", json=payload)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "draft_conflict"
    current = client.get(f"/api/meetings/{meeting['id']}").json()
    assert current["draft_revision"] == 2
    assert current["draft"]["title"] == newer["title"]
    payload = {**payload, "command_id": str(uuid4()), "expected_draft_revision": 2}
    response = client.post(f"/api/meetings/{meeting['id']}/follow-ups", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["draft_revision"] == 3
    assert client.post(f"/api/meetings/{meeting['id']}/follow-ups", json=payload).json() == response.json()


def test_event_page_never_returns_events_after_its_declared_cutoff(client, application, monkeypatch):
    from round_robin.api import events

    _, run = create_run(client)
    original_owned_run = events.owned_run
    def commit_between_read_and_page(session, run_id, owner, **kwargs):
        row = original_owned_run(session, run_id, owner, **kwargs)
        # Reproduce a commit between reading the run sequence and querying its
        # events, as PostgreSQL READ COMMITTED permits across two statements.
        with application.state.session_factory.begin() as writer:
            append_event(writer, writer.get(Run, run_id), "audit.concurrent", {})
        return row
    monkeypatch.setattr(events, "owned_run", commit_between_read_and_page)
    response = client.get(f"/api/runs/{run['id']}/events")
    assert response.status_code == 200, response.text
    page = response.json()
    assert all(item["sequence"] <= page["event_cutoff"] for item in page["items"])
    assert page["next_cursor"] <= page["event_cutoff"]


def test_negative_event_cursor_is_normalized_for_an_empty_page(client, application):
    _, run = create_run(client)
    with application.state.session_factory.begin() as session:
        session.query(Event).filter(Event.run_id == run["id"]).delete()
    response = client.get(f"/api/runs/{run['id']}/events?after=-1")
    assert response.status_code == 200, response.text
    assert response.json()["next_cursor"] == 0


def test_future_event_cursor_cannot_overflow_database_integer(client):
    _, run = create_run(client)
    response = client.get(f"/api/runs/{run['id']}/events?after={10**100}")
    assert response.status_code == 200, response.text
    assert response.json()["next_cursor"] <= response.json()["event_cutoff"]


def test_retry_honors_infrastructure_claims_across_role_iterations(client, application):
    _, run = create_run(client)
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        row.status = "completed_with_limitations"
        row.configuration = {**row.configuration, "stage_iteration": 1}
        prior = add_failed_step(session, row.id, "synthesis", "timeout", calls=0, iteration=0)
        prior.infrastructure_claims = 2
        add_failed_step(session, row.id, "synthesis", "timeout", calls=1, iteration=1)
    response = client.get(f"/api/runs/{run['id']}/snapshot")
    assert response.status_code == 200, response.text
    assert response.json()["retry_eligible"] is False


def test_retry_stage_availability_does_not_depend_on_the_bounded_event_page(client, application):
    _, run = create_run(client)
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        row.status = "completed_with_limitations"
        for _ in range(105):
            append_event(session, row, "audit.test_progress", {})
        add_failed_step(session, row.id, "audit", "timeout")
        append_event(session, row, "step.failed", {"stage": "audit"})
    response = client.get(f"/api/runs/{run['id']}/snapshot")
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert len(snapshot["events"]) == 100
    assert not any(item["event_type"] == "step.failed" for item in snapshot["events"])
    assert snapshot["retry_eligible"] is True
    assert snapshot["retryable_stages"] == ["audit"]
