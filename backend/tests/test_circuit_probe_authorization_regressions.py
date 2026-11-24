from copy import deepcopy
from datetime import timedelta

from round_robin.database.models import (
    Attempt,
    BudgetOperation,
    PolicyBucket,
    Run,
    Step,
    utcnow,
)
from round_robin.orchestration import circuits
from round_robin.orchestration.budget import BudgetDenied
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker import tasks
from sqlalchemy import select

from backend.tests.test_exhaustive_runtime_workers import prepared
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_budget_denial_does_not_consume_half_open_probe_without_authorization(factory, monkeypatch):
    run_id, step_id = prepared(factory)
    with factory.begin() as session:
        run, step = session.get(Run, run_id), session.get(Step, step_id)
        provider = step.provider
        scope = circuits._scope(run, provider)
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        original = {"open_until": (utcnow() - timedelta(seconds=1)).isoformat(), "failures": [], "probe_in_flight": False}
        bucket.metadata_json = {**bucket.metadata_json, "circuits": {scope: original, "sibling_scope": {"failures": ["2026-01-01T00:00:00+00:00"]}}}
    def no_authorization(*args, **kwargs):
        raise BudgetDenied("finish_call_reserve")
    monkeypatch.setattr(tasks, "authorize_operation", no_authorization)
    adapter = SimulatedProvider(provider)
    result = tasks.execute_step(step_id, session_factory=factory, adapter_registry={provider: adapter})
    assert result["status"] in {"policy_blocked", "policy_skipped"} and adapter.calls == 0
    with factory() as session:
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert bucket.metadata_json["circuits"][scope] == original
        assert bucket.metadata_json["circuits"]["sibling_scope"] == {"failures": ["2026-01-01T00:00:00+00:00"]}
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None
        assert not [op for op in session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run_id)) if op.metadata_json.get("model_call") is True]


def test_real_exhausted_call_limit_does_not_consume_probe(factory):
    run_id, step_id = prepared(factory)
    with factory.begin() as session:
        run, step = session.get(Run, run_id), session.get(Step, step_id)
        provider = step.provider
        scope = circuits._scope(run, provider)
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        original = {"open_until": (utcnow() - timedelta(seconds=1)).isoformat(), "failures": [], "probe_in_flight": False}
        bucket.metadata_json = {**bucket.metadata_json, "circuits": {scope: original}}
        hold = session.get(BudgetOperation, f"finish_{run_id}")
        limit = min(24, int(run.configuration["limits"]["max_requests"]))
        for index in range(limit - len(hold.metadata_json["roles_remaining"])):
            session.add(BudgetOperation(id=f"synthetic_prior_call_{index}", run_id=run_id, owner_id=run.owner_id,
                                        kind="model", status="settled", reserved_usd="0", settled_usd="0",
                                        metadata_json={"model_call": True}))
    adapter = SimulatedProvider(provider)
    result = tasks.execute_step(step_id, session_factory=factory, adapter_registry={provider: adapter})
    assert result == {"status": "policy_blocked", "code": "finish_call_reserve"} and adapter.calls == 0
    with factory() as session:
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert bucket.metadata_json["circuits"][scope] == original
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None


def test_successful_half_open_probe_is_durable_and_counted_before_provider_call(factory):
    run_id, step_id = prepared(factory)
    with factory.begin() as session:
        run, step = session.get(Run, run_id), session.get(Step, step_id)
        provider = step.provider
        scope = circuits._scope(run, provider)
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        bucket.metadata_json = {**bucket.metadata_json, "circuits": {scope: {"open_until": (utcnow() - timedelta(seconds=1)).isoformat(), "probe_in_flight": False}, "sibling_scope": {"failures": []}}}
    observed = []
    class InspectingProvider(SimulatedProvider):
        def generate(self, request):
            with factory() as session:
                bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
                circuit = bucket.metadata_json["circuits"][scope]
                attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
                ledger = session.get(BudgetOperation, attempt.id)
                observed.append((circuit["probe_in_flight"], attempt.status, ledger.status, ledger.metadata_json["model_call"]))
            return super().generate(request)
    adapter = InspectingProvider(provider)
    result = tasks.execute_step(step_id, session_factory=factory, adapter_registry={provider: adapter})
    assert result["status"] == "persisted" and adapter.calls == 1
    assert observed == [(True, "authorized", "reserved", True)]
    with factory() as session:
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert scope not in bucket.metadata_json["circuits"]
        assert bucket.metadata_json["circuits"]["sibling_scope"] == {"failures": []}
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        assert attempt.status == "succeeded" and session.get(BudgetOperation, attempt.id).status == "settled"
        assert session.get(Step, step_id).calls == 1


def test_open_circuit_blocks_before_creating_any_model_authorization(factory):
    run_id, step_id = prepared(factory)
    with factory.begin() as session:
        run, step = session.get(Run, run_id), session.get(Step, step_id)
        provider = step.provider
        scope = circuits._scope(run, provider)
        original = {"open_until": (utcnow() + timedelta(seconds=30)).isoformat(), "probe_in_flight": False}
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        bucket.metadata_json = {**bucket.metadata_json, "circuits": {scope: deepcopy(original)}}
    adapter = SimulatedProvider(provider)
    result = tasks.execute_step(step_id, session_factory=factory, adapter_registry={provider: adapter})
    assert result == {"status": "provider_blocked", "code": "circuit_open"} and adapter.calls == 0
    with factory() as session:
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert bucket.metadata_json["circuits"][scope] == original
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None
        assert session.get(Step, step_id).calls == 0


def test_denial_after_partial_authorization_rolls_back_probe_ledger_and_counter(factory, monkeypatch):
    run_id, step_id = prepared(factory)
    with factory.begin() as session:
        run, step = session.get(Run, run_id), session.get(Step, step_id)
        provider = step.provider
        scope = circuits._scope(run, provider)
        original = {"open_until": (utcnow() - timedelta(seconds=1)).isoformat(), "probe_in_flight": False}
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        bucket.metadata_json = {**bucket.metadata_json, "circuits": {scope: deepcopy(original)}}
    authorize = tasks.authorize_operation
    def partial_then_deny(*args, **kwargs):
        authorize(*args, **kwargs)
        raise BudgetDenied("finish_call_reserve")
    monkeypatch.setattr(tasks, "authorize_operation", partial_then_deny)
    adapter = SimulatedProvider(provider)
    result = tasks.execute_step(step_id, session_factory=factory, adapter_registry={provider: adapter})
    assert result["status"] in {"policy_blocked", "policy_skipped"} and adapter.calls == 0
    with factory() as session:
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert bucket.metadata_json["circuits"][scope] == original
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None
        assert session.get(Step, step_id).calls == 0
        assert not [op for op in session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run_id)) if op.metadata_json.get("model_call") is True]
