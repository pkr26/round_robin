"""Deterministic circuit policy checks; probes use the normal counted ledger."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from round_robin.app import create_app
from round_robin.database.models import BudgetOperation, PolicyBucket, Run, Step
from round_robin.database.services import new_id
from round_robin.fixtures import sample_brief
from round_robin.orchestration import budget, circuits
from round_robin.orchestration.budget import authorize_operation
from round_robin.providers.base import ProviderFailure


@pytest.fixture
def circuit_context(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 16, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(circuits, "utcnow", lambda: clock[0])
    monkeypatch.setattr(budget, "utcnow", lambda: clock[0])
    app = create_app(f"sqlite:///{tmp_path / 'circuit_test.sqlite'}", initialize=True)
    with TestClient(app) as client:
        meeting = client.post("/api/meetings", json={"command_id": str(uuid4()), "brief": sample_brief().model_dump(mode="json")}).json()
        preflight = client.post(f"/api/meetings/{meeting['id']}/preflight", json={"command_id": str(uuid4()), "expected_draft_revision": 1}).json()
        run = client.post(f"/api/meetings/{meeting['id']}/runs", json={"command_id": str(uuid4()), "preflight_id": preflight["id"], "expected_draft_revision": 1}).json()
    with app.state.session_factory.begin() as session:
        saved = session.get(Run, run["id"])
        saved.status, saved.first_admitted_at, saved.active_started_at = "running", clock[0], clock[0]
        saved.configuration = {**saved.configuration, "credential_versions": {"openai": "credential-v1"}}
    return app, run["id"], clock


def failures(app, run_id, clock):
    baseline = clock[0]
    for seconds in (0, 10, 20):
        clock[0] = baseline + timedelta(seconds=seconds)
        with app.state.session_factory.begin() as session:
            circuits.record_outcome(session, session.get(Run, run_id), "openai", ProviderFailure("timeout", may_have_sent=True, cost_usd=None))
    return baseline


def test_three_transient_failures_open_for_30_seconds_and_isolate_model_credential_and_provider(circuit_context):
    app, run_id, clock = circuit_context
    baseline = failures(app, run_id, clock)
    clock[0] = baseline + timedelta(seconds=25)
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        with pytest.raises(ProviderFailure) as error:
            circuits.before_attempt(session, run, "openai")
        assert error.value.code == "circuit_open" and error.value.may_have_sent is False
        circuits.before_attempt(session, run, "anthropic")
        changed_model = SimpleNamespace(owner_id=run.owner_id, configuration={**run.configuration, "models": {**run.configuration["models"], "openai": "another-explicit-model"}})
        circuits.before_attempt(session, changed_model, "openai")
        changed_key_version = SimpleNamespace(owner_id=run.owner_id, configuration={**run.configuration, "credential_versions": {"openai": "credential-v2"}})
        circuits.before_attempt(session, changed_key_version, "openai")
    clock[0] = baseline + timedelta(seconds=49)
    with app.state.session_factory.begin() as session:
        with pytest.raises(ProviderFailure):
            circuits.before_attempt(session, session.get(Run, run_id), "openai")


def test_half_open_probe_has_one_durable_slot_and_uses_original_paid_request_counter(circuit_context):
    app, run_id, clock = circuit_context
    baseline = failures(app, run_id, clock)
    clock[0] = baseline + timedelta(seconds=51)
    step_id = new_id("step")
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        circuits.before_attempt(session, run, "openai")
        step = Step(id=step_id, run_id=run_id, stage="proposal", role_slot_id="openai", provider="openai", status="claimed", calls=0)
        session.add(step)
        session.flush()
        authorize_operation(session, run, "operation_half_open_probe", "model", "0", step=step)
    # Another eligible contender sees the already committed probe lease. This
    # is the deterministic transaction assertion, not proof of a PG race.
    with app.state.session_factory.begin() as session:
        with pytest.raises(ProviderFailure):
            circuits.before_attempt(session, session.get(Run, run_id), "openai")
        assert session.get(Step, step_id).calls == 1
        assert session.get(BudgetOperation, "operation_half_open_probe").metadata_json["model_call"] is True
        circuits.record_outcome(session, session.get(Run, run_id), "openai", None)
    with app.state.session_factory.begin() as session:
        circuits.before_attempt(session, session.get(Run, run_id), "openai")
        assert session.get(Step, step_id).calls == 1


def test_old_failures_and_permanent_configuration_errors_do_not_accumulate_a_transient_open(circuit_context):
    app, run_id, clock = circuit_context
    baseline = clock[0]
    for seconds in (0, 61, 122):
        clock[0] = baseline + timedelta(seconds=seconds)
        with app.state.session_factory.begin() as session:
            run = session.get(Run, run_id)
            circuits.record_outcome(session, run, "openai", ProviderFailure("transport_failure", may_have_sent=False, cost_usd=None))
            circuits.before_attempt(session, run, "openai")
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        for _ in range(5):
            circuits.record_outcome(session, run, "anthropic", ProviderFailure("authentication", may_have_sent=False, cost_usd=None))
        circuits.before_attempt(session, run, "anthropic")
        bucket = session.query(PolicyBucket).filter_by(key="deployment").one()
        assert "credential-v1" not in str(bucket.metadata_json)


def test_empty_circuit_accepts_attempt_and_success(circuit_context):
    app, run_id, _ = circuit_context
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        circuits.before_attempt(session, run, "openai")
        circuits.record_outcome(session, run, "openai", None)
        assert session.query(PolicyBucket).filter_by(key="owner:None").one_or_none() is None


def test_default_scope_matches_explicit_fallbacks_and_separates_versions(circuit_context):
    app, run_id, _ = circuit_context
    with app.state.session_factory.begin() as session:
        owner = session.get(Run, run_id).owner_id
        def run(configuration):
            return SimpleNamespace(owner_id=owner, configuration=configuration)

        default = run({})
        explicit = run({"models": {"openai": "openai"}, "credential_versions": {"openai": "simulated"}})
        for _ in range(3):
            circuits.record_outcome(session, default, "openai", ProviderFailure("timeout"))
        with pytest.raises(ProviderFailure, match="circuit_open"):
            circuits.before_attempt(session, explicit, "openai")
        circuits.before_attempt(session, run({"version": "new-version"}), "openai")
        circuits.before_attempt(session, run({"credential_versions": {"openai": "new-key"}}), "openai")

        versioned = run({"version": "v1"})
        for _ in range(3):
            circuits.record_outcome(session, versioned, "openai", ProviderFailure("timeout"))
        with pytest.raises(ProviderFailure, match="circuit_open"):
            circuits.before_attempt(session, run({"credential_versions": {"openai": "v1"}}), "openai")
        circuits.before_attempt(session, run({"version": "v2"}), "openai")


@pytest.mark.parametrize("elapsed,opens", [(60, True), (60.001, False)])
def test_transient_failure_window_includes_exactly_sixty_seconds(circuit_context, elapsed, opens):
    app, run_id, clock = circuit_context
    start = clock[0]
    for seconds in (0, 1, elapsed):
        clock[0] = start + timedelta(seconds=seconds)
        with app.state.session_factory.begin() as session:
            circuits.record_outcome(session, session.get(Run, run_id), "openai", ProviderFailure("timeout"))
    with app.state.session_factory.begin() as session:
        if opens:
            with pytest.raises(ProviderFailure, match="circuit_open"):
                circuits.before_attempt(session, session.get(Run, run_id), "openai")
        else:
            circuits.before_attempt(session, session.get(Run, run_id), "openai")


def test_cooldown_and_probe_lease_expire_at_their_deadlines(circuit_context):
    app, run_id, clock = circuit_context
    start = failures(app, run_id, clock)
    clock[0] = start + timedelta(seconds=50)
    with app.state.session_factory.begin() as session:
        circuits.before_attempt(session, session.get(Run, run_id), "openai")
    clock[0] = start + timedelta(seconds=199.999)
    with app.state.session_factory.begin() as session:
        with pytest.raises(ProviderFailure, match="circuit_open"):
            circuits.before_attempt(session, session.get(Run, run_id), "openai")
    clock[0] = start + timedelta(seconds=200)
    with app.state.session_factory.begin() as session:
        circuits.before_attempt(session, session.get(Run, run_id), "openai")


def test_permanent_failure_of_probe_reopens_for_thirty_seconds(circuit_context):
    app, run_id, clock = circuit_context
    start = failures(app, run_id, clock)
    clock[0] = start + timedelta(seconds=50)
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        circuits.before_attempt(session, run, "openai")
        circuits.record_outcome(session, run, "openai", ProviderFailure("authentication"))
    clock[0] = start + timedelta(seconds=79.999)
    with app.state.session_factory.begin() as session:
        with pytest.raises(ProviderFailure, match="circuit_open"):
            circuits.before_attempt(session, session.get(Run, run_id), "openai")
    clock[0] = start + timedelta(seconds=80)
    with app.state.session_factory.begin() as session:
        circuits.before_attempt(session, session.get(Run, run_id), "openai")


def test_transient_failure_releases_probe_and_reopens_circuit(circuit_context):
    app, run_id, clock = circuit_context
    start = failures(app, run_id, clock)
    clock[0] = start + timedelta(seconds=50)
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        circuits.before_attempt(session, run, "openai")
        circuits.record_outcome(session, run, "openai", ProviderFailure("timeout"))
        bucket = session.query(PolicyBucket).filter_by(key="deployment").one()
        state = bucket.metadata_json["circuits"][circuits._scope(run, "openai")]
        assert state["probe_in_flight"] is False
        with pytest.raises(ProviderFailure, match="circuit_open"):
            circuits.before_attempt(session, run, "openai")


def test_failure_history_is_bounded_to_three_entries(circuit_context):
    app, run_id, _ = circuit_context
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        for _ in range(5):
            circuits.record_outcome(session, run, "openai", ProviderFailure("timeout"))
        bucket = session.query(PolicyBucket).filter_by(key="deployment").one()
        records = list(bucket.metadata_json["circuits"].values())
        assert len(records) == 1
        assert len(records[0]["failures"]) == 3
