"""Independent API audit: contracts, owner boundaries and stateful command replay."""
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi.testclient import TestClient
from hypothesis import settings as property_settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
import pytest
from sqlalchemy import func, select

from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, BudgetOperation, Command, Meeting, Run, utcnow
from round_robin.fixtures import sample_brief


def command(**fields):
    return {"command_id": str(uuid4()), **fields}


@pytest.fixture
def application(tmp_path):
    return create_app(f"sqlite:///{tmp_path / 'deep-api.sqlite'}", settings=replace(Settings(), artifact_directory=tmp_path / "artifacts"), initialize=True)


@pytest.fixture
def client(application):
    with TestClient(application, raise_server_exceptions=False) as value:
        yield value


def prepare(client):
    meeting = client.post("/api/meetings", json=command(brief=sample_brief().model_dump(mode="json"))).json()
    quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1)).json()
    run = client.post(f"/api/meetings/{meeting['id']}/runs", json=command(preflight_id=quote["id"], expected_draft_revision=1)).json()
    return meeting, run


@pytest.mark.parametrize("suffix", ["", "/snapshot", "/events", "/messages", "/report", "/reports/1", "/stream"])
def test_every_core_run_read_rejects_other_owners(client, application, suffix):
    _, run = prepare(client)
    application.dependency_overrides[require_owner] = lambda: "owner_intruder"
    response = client.get(f"/api/runs/{run['id']}{suffix}")
    assert response.status_code == 404, response.text
    assert run["meeting_id"] not in response.text


@pytest.mark.parametrize("suffix", ["", "/draft", "/runs", "/outcomes"])
def test_every_core_meeting_read_rejects_other_owners(client, application, suffix):
    meeting, _ = prepare(client)
    application.dependency_overrides[require_owner] = lambda: "owner_intruder"
    response = client.get(f"/api/meetings/{meeting['id']}{suffix}")
    assert response.status_code == 404, response.text
    assert meeting["title"] not in response.text


@pytest.mark.parametrize("method,suffix,fields", [
    ("PATCH", "/draft", {"expected_revision": 1, "brief": sample_brief().model_dump(mode="json")}),
    ("POST", "/preflight", {"expected_draft_revision": 1}),
    ("POST", "/runs", {"expected_draft_revision": 1, "preflight_id": "preflight_unknown"}),
    ("POST", "/follow-ups", {"expected_draft_revision": 1, "parent_run_id": "run_unknown", "brief": sample_brief().model_dump(mode="json")}),
    ("POST", "/outcomes", {"run_id": "run_unknown", "report_version": 1, "choice": "accepted"}),
    ("DELETE", "", {}),
])
def test_every_core_meeting_write_rejects_other_owners(client, application, method, suffix, fields):
    meeting, _ = prepare(client)
    application.dependency_overrides[require_owner] = lambda: "owner_intruder"
    response = client.request(method, f"/api/meetings/{meeting['id']}{suffix}", json=command(**fields))
    assert response.status_code == 404, response.text
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Command).where(Command.owner_id == "owner_intruder")) == 0


@pytest.mark.parametrize("suffix,fields", [
    ("/cancel", {"expected_run_version": 1}),
    ("/retries", {"expected_run_version": 1, "stage": "audit"}),
    ("/clarifications", {"expected_run_version": 1, "brief": sample_brief().model_dump(mode="json")}),
])
def test_every_core_run_write_rejects_other_owners(client, application, suffix, fields):
    _, run = prepare(client)
    application.dependency_overrides[require_owner] = lambda: "owner_intruder"
    response = client.post(f"/api/runs/{run['id']}{suffix}", json=command(**fields))
    assert response.status_code == 404, response.text


def test_remaining_request_count_includes_shared_visual_model_calls(client, application):
    _, run = prepare(client)
    with application.state.session_factory.begin() as session:
        session.add(BudgetOperation(id="image_operation_shared", run_id=run["id"], owner_id="owner_local", kind="image", status="settled", reserved_usd="0", settled_usd="0", metadata_json={"model_call": True}))
    response = client.get(f"/api/runs/{run['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["remaining_limits"]["requests"] == 23


def test_clock_regression_does_not_make_run_reads_or_cancellation_unavailable(client, application):
    _, run = prepare(client)
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run["id"])
        row.status = "running"
        row.active_started_at = utcnow() + timedelta(minutes=1)
    response = client.get(f"/api/runs/{run['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["active_seconds"] == 0
    cancelled = client.post(f"/api/runs/{run['id']}/cancel", json=command(expected_run_version=run["version"]))
    assert cancelled.status_code == 200, cancelled.text
    from round_robin.orchestration.controller import advance_run
    with application.state.session_factory.begin() as session:
        advance_run(session, session.get(Run, run["id"]))
    saved = client.get(f"/api/runs/{run['id']}/snapshot")
    assert saved.status_code == 200, saved.text
    assert saved.json()["run"]["status"] == "cancelled"
    assert saved.json()["run"]["remaining_limits"]["active_seconds"] == 0
    assert saved.json()["retry_eligible"] is False


def test_sqlite_application_enforces_foreign_keys_like_postgresql(application):
    from round_robin.database.models import Draft
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        with application.state.session_factory.begin() as session:
            session.add(Draft(meeting_id="meeting_does_not_exist", revision=1, payload={}))


def test_sse_database_reads_do_not_block_other_async_requests(client, application):
    import asyncio
    from types import SimpleNamespace
    import time
    from round_robin.api.events import stream_events

    _, run = prepare(client)
    with application.state.session_factory.begin() as session:
        session.get(Run, run["id"]).status = "completed"
    factory = application.state.session_factory
    def slow_factory():
        time.sleep(.04)
        return factory()
    application.state.session_factory = slow_factory
    async def connected():
        return False
    async def exercise():
        ticks = []
        async def heartbeat():
            for _ in range(10):
                await asyncio.sleep(.005)
                ticks.append(1)
        task = asyncio.create_task(heartbeat())
        request = SimpleNamespace(app=application, is_disconnected=connected)
        response = await stream_events(run["id"], request, after=0, last_event_id=None, owner="owner_local")
        await anext(response.body_iterator)
        assert len(ticks) >= 2, "Synchronous SSE database I/O blocked the application event loop"
        await response.body_iterator.aclose()
        await task
    asyncio.run(exercise())


@pytest.mark.parametrize("origin", ["https://app.example/", "https://APP.EXAMPLE", "https://app.example:443"])
def test_hosted_origin_uses_browser_canonical_origin(tmp_path, origin):
    from backend.tests.test_hosted import hosted_settings
    from round_robin.security.hosted import SESSION_COOKIE, create_session

    settings = replace(hosted_settings(tmp_path), public_origin=origin)
    app = create_app(f"sqlite:///{tmp_path / 'hosted-origin.sqlite'}", settings=settings, initialize=True)
    token, csrf, _ = create_session(app.state.session_factory, settings, settings.oidc_issuer, "alice")
    with TestClient(app, base_url="https://app.example") as client:
        client.cookies.set(SESSION_COOKIE, token, domain="app.example", path="/")
        response = client.post("/api/meetings", json=command(brief=sample_brief().model_dump(mode="json")), headers={"Origin": "https://app.example", "X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    assert response.headers["access-control-allow-origin"] == "https://app.example"


@pytest.mark.parametrize("address", ["https://app.example:notaport", "https://app.example:65536", "https://app.example:0", "https://%65xample.com", "https://example.com\\suffix", "https://app.\nexample", "https://127.000.000.001", "https://0x7f.1"])
def test_hosted_configuration_rejects_invalid_ports(tmp_path, address):
    from backend.tests.test_hosted import hosted_settings
    with pytest.raises(ValueError):
        replace(hosted_settings(tmp_path), public_origin=address).validate()


@pytest.mark.parametrize("origin,expected", [("https://faß.example/", "https://xn--fa-hia.example"), ("https://[0:0:0:0:0:0:0:1]:443/", "https://[::1]")])
def test_hosted_origin_normalization_agrees_with_browser_authority(tmp_path, origin, expected):
    from backend.tests.test_hosted import hosted_settings
    settings = replace(hosted_settings(tmp_path), public_origin=origin)
    settings.validate()
    assert settings.public_origin == expected


class DurableCommandMachine(RuleBasedStateMachine):
    """Generate action sequences against an independent revision/snapshot model."""
    def __init__(self):
        super().__init__()
        self.directory = TemporaryDirectory(prefix="round-robin-api-state-")
        root = Path(self.directory.name)
        self.app = create_app(f"sqlite:///{root / 'state.sqlite'}", settings=replace(Settings(), artifact_directory=root / "artifacts", max_queued_per_owner=20), initialize=True)
        self.client = TestClient(self.app)
        self.meeting = None
        self.revision = 0
        self.saved_brief = None
        self.runs = {}
        self.last = None

    def send(self, method, path, body):
        response = self.client.request(method, path, json=body)
        if response.is_success:
            self.last = (method, path, body, response.status_code, response.json())
        return response

    @precondition(lambda self: self.meeting is None)
    @rule()
    def create(self):
        brief = sample_brief().model_dump(mode="json")
        response = self.send("POST", "/api/meetings", command(brief=brief))
        assert response.status_code == 201
        self.meeting, self.revision = response.json()["id"], 1
        self.saved_brief = response.json()["draft"]

    @precondition(lambda self: self.meeting is not None)
    @rule(title=st.text(alphabet="abcXYZ 012é", min_size=1, max_size=40), stale=st.booleans())
    def save(self, title, stale):
        brief = {**self.saved_brief, "title": title}
        expected = max(0, self.revision - 1) if stale else self.revision
        response = self.send("PATCH", f"/api/meetings/{self.meeting}/draft", command(expected_revision=expected, brief=brief))
        if stale:
            assert response.status_code in {409, 422}
        else:
            assert response.status_code == 200
            self.revision += 1
            self.saved_brief = {**brief, "revision": self.revision}

    @precondition(lambda self: self.meeting is not None)
    @rule()
    def start_and_cancel(self):
        quote = self.send("POST", f"/api/meetings/{self.meeting}/preflight", command(expected_draft_revision=self.revision)).json()
        response = self.send("POST", f"/api/meetings/{self.meeting}/runs", command(preflight_id=quote["id"], expected_draft_revision=self.revision))
        assert response.status_code == 202
        run = response.json()
        self.runs[run["id"]] = dict(self.saved_brief)
        cancelled = self.send("POST", f"/api/runs/{run['id']}/cancel", command(expected_run_version=run["version"]))
        assert cancelled.status_code == 200

    @precondition(lambda self: self.meeting is not None)
    @rule()
    def stale_preflight(self):
        quote = self.send("POST", f"/api/meetings/{self.meeting}/preflight", command(expected_draft_revision=self.revision)).json()
        self.save("A newer explicitly saved revision", False)
        response = self.send("POST", f"/api/meetings/{self.meeting}/runs", command(preflight_id=quote["id"], expected_draft_revision=self.revision))
        assert response.status_code == 409

    @precondition(lambda self: self.last is not None)
    @rule()
    def replay_acknowledged_command(self):
        method, path, body, status, expected = self.last
        response = self.client.request(method, path, json=body)
        assert response.status_code == status
        assert response.json() == expected

    @invariant()
    def saved_state_matches_model_and_start_inputs_never_change(self):
        if self.meeting:
            response = self.client.get(f"/api/meetings/{self.meeting}/draft")
            assert response.status_code == 200
            assert response.json()["revision"] == self.revision
            assert response.json()["brief"] == self.saved_brief
        with self.app.state.session_factory() as session:
            for run_id, brief in self.runs.items():
                assert session.get(Run, run_id).input_snapshot == brief
            assert session.scalar(select(func.count()).select_from(Attempt)) == 0

    def teardown(self):
        self.client.close()
        self.app.state.engine.dispose()
        self.directory.cleanup()


TestDurableCommandSequences = DurableCommandMachine.TestCase
TestDurableCommandSequences.settings = property_settings(max_examples=20, stateful_step_count=20, deadline=None, derandomize=True)
