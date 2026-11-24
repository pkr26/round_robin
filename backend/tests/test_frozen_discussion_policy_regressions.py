import hashlib
import json
from copy import deepcopy
from decimal import Decimal

import pytest
from round_robin.database.models import Attempt, BudgetOperation, Run, Step
from round_robin.orchestration import protocol
from round_robin.orchestration.budget import BudgetDenied, _policy_checks
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_exhaustive_runtime_workers import prepared

pytest_plugins = ("backend.tests.test_orchestration",)


def test_policy_identity_binds_exact_canonical_graph_and_profiles():
    descriptors = {profile: protocol.policy_descriptor(profile) for profile in ("independent_review", "single_provider", "bounded_debate")}
    for profile, descriptor in descriptors.items():
        expected = hashlib.sha256(json.dumps({"policy": protocol.POLICY, "profile": profile}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert descriptor == {"version": protocol.POLICY["version"], "profile": profile, "sha256": expected}
    assert len({item["sha256"] for item in descriptors.values()}) == 3
    with pytest.raises(ValueError):
        protocol.policy_descriptor("unrecognized")


def test_frozen_identity_detects_changed_graph_and_accepts_legacy(monkeypatch):
    frozen = protocol.policy_descriptor()
    assert protocol.verify_policy({"discussion_policy": deepcopy(frozen)})
    assert protocol.verify_policy({}) and protocol.verify_policy({"discussion_policy": None})
    monkeypatch.setitem(protocol.POLICY, "maximum_logical_calls", protocol.POLICY["maximum_logical_calls"] + 1)
    assert not protocol.verify_policy({"discussion_policy": frozen})


@pytest.mark.parametrize("value", [
    {}, [], False, "unknown", {"version": "unknown", "profile": "independent_review", "sha256": "0" * 64},
    {"version": "bounded-independent-review-v2", "profile": "independent_review", "sha256": "0" * 64},
    {"version": "bounded-independent-review-v2", "profile": "single_provider", "sha256": "0" * 64},
])
def test_invalid_policy_blocks_actual_worker_before_authorization(factory, value):
    run_id, step_id = prepared(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "discussion_policy": value}
    adapter = SimulatedProvider("openai")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": adapter})
    assert result == {"status": "policy_blocked", "code": "discussion_policy_incompatible"}
    assert adapter.calls == 0
    with factory() as session:
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None
        assert session.get(Step, step_id).calls == 0
        assert not [row for row in session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run_id)) if row.metadata_json.get("model_call") is True]
        with pytest.raises(BudgetDenied, match="discussion_policy_incompatible"):
            _policy_checks(session, session.get(Run, run_id), Decimal(0))


@pytest.mark.parametrize("profile", ["single_provider", "bounded_debate"])
def test_nonproduction_policy_profile_blocks_actual_worker(factory, profile):
    run_id, step_id = prepared(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "discussion_policy": protocol.policy_descriptor(profile)}
    adapter = SimulatedProvider("openai")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": adapter})
    assert result == {"status": "policy_blocked", "code": "discussion_policy_incompatible"}
    assert adapter.calls == 0
    with factory() as session:
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None


@pytest.mark.parametrize("legacy", ["missing", "null"])
def test_legacy_policy_absence_remains_executable(factory, legacy):
    run_id, step_id = prepared(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        configuration = dict(run.configuration)
        configuration.pop("discussion_policy", None)
        if legacy == "null":
            configuration["discussion_policy"] = None
        run.configuration = configuration
    adapter = SimulatedProvider("openai")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": adapter})
    assert result["status"] == "persisted" and adapter.calls == 1
    with factory() as session:
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        assert attempt.status == "succeeded"
