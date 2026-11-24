"""Clock and money boundaries used by spending authorization."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory, seed
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.database.models import BudgetOperation, Run, Step
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import (
    BudgetDenied,
    _policy_checks,
    active_elapsed,
    amount,
    authorize_operation,
    aware,
    freeze_active,
    operations_exposure,
    reconcile_operation,
    remaining_active,
    reserve_finish,
)
from round_robin.orchestration.controller import advance_run


def test_invalid_money_has_stable_denial_reason():
    with pytest.raises(BudgetDenied, match="^invalid_money$"):
        amount("NaN")


def test_ledger_helpers_accept_their_named_arguments(factory):
    assert operations_exposure(operations=[]) == Decimal("0")
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        _policy_checks(session=session, run=run, extra=Decimal("0"))
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        operation = authorize_operation(
            session=session, run=run, operation_id="keyword_request", kind="model", bound_usd="0", step=step,
        )
        assert operation.id == "keyword_request"


def test_clock_values_are_normalized_to_utc():
    local = datetime(2026, 11, 17, 12, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    normalized = aware(local)
    assert normalized.replace(tzinfo=None) == datetime(2026, 11, 17, 6, 30)
    assert normalized.tzinfo is timezone.utc
    assert aware(datetime(2026, 11, 17, 12)).tzinfo is timezone.utc


def test_active_clock_counts_forward_time_and_tolerates_one_second_of_skew():
    now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    run = SimpleNamespace(active_seconds=0, active_started_at=now)
    assert active_elapsed(run, now) == 0
    run.active_seconds = 5
    run.active_started_at = now - timedelta(seconds=3)
    assert active_elapsed(run, now) == 8
    run.active_started_at = now + timedelta(seconds=1)
    assert active_elapsed(run, now) == 5
    run.active_started_at = now + timedelta(seconds=1, microseconds=1)
    with pytest.raises(BudgetDenied, match="^clock_regression$"):
        active_elapsed(run, now)


def test_remaining_active_uses_frozen_limit_and_permanent_clock_fence():
    run = SimpleNamespace(active_seconds=10, active_started_at=None, configuration={})
    assert remaining_active(run) == 590
    run.configuration = {"limits": {"active_seconds": 120}}
    assert remaining_active(run) == 110
    run.configuration["active_clock_invalid"] = True
    with pytest.raises(BudgetDenied, match="^clock_regression$"):
        remaining_active(run)


def test_explicit_clock_is_used_to_measure_remaining_and_freeze_time():
    now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    run = SimpleNamespace(active_seconds=10, active_started_at=now - timedelta(seconds=5), configuration={})
    assert remaining_active(run, now) == 585
    freeze_active(run, now)
    assert run.active_seconds == 15
    assert run.active_started_at is None


def test_monthly_settled_exposure_excludes_prior_and_future_months():
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    rows = [
        BudgetOperation(status="settled", settled_usd="1", authorized_at=timestamp, metadata_json={})
        for timestamp in (datetime(2026, 10, 31, tzinfo=timezone.utc), now, datetime(2026, 12, 1, tzinfo=timezone.utc))
    ]
    rows.insert(0, BudgetOperation(status="released", reserved_usd="100", metadata_json={}))
    assert operations_exposure(rows, now=now, period=json.loads('"monthly"')) == Decimal("1")


def test_daily_exposure_uses_decoded_statuses_and_exact_utc_day():
    now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    rows = [BudgetOperation(status=json.loads('"released"'), reserved_usd="100", metadata_json={})]
    rows.extend(
        BudgetOperation(
            status=json.loads('"settled"'), reserved_usd="0", settled_usd="1",
            authorized_at=timestamp, metadata_json={},
        )
        for timestamp in (now - timedelta(days=1), now, now + timedelta(days=1))
    )
    assert operations_exposure(rows, now=now, period=json.loads('"daily"')) == Decimal("1")


def test_unknown_usage_from_decoded_input_preserves_prior_high_water(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        session.add(BudgetOperation(
            id="observed_usage", run_id=run_id, owner_id="owner_test", kind="model",
            status="unknown", reserved_usd="0", settled_usd="5",
            authorized_at=datetime.now(timezone.utc), metadata_json={},
        ))
        session.flush()
        certainty = json.loads('"unknown"')
        operation = reconcile_operation(session, "observed_usage", "1", certainty)
        assert operation.status == "unknown"
        assert Decimal(operation.settled_usd) == Decimal("5")


def test_decoded_receipt_certainty_preserves_unknown_or_proves_no_send(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        session.add(BudgetOperation(
            id="receipt_certainty", run_id=run_id, owner_id="owner_test", kind="model",
            status="unknown", reserved_usd="5", settled_usd="1",
            authorized_at=datetime.now(timezone.utc), metadata_json={},
        ))
        session.flush()
        operation = reconcile_operation(session, "receipt_certainty", None, json.loads('"reported"'))
        assert operation.status == "unknown"
        assert Decimal(operation.settled_usd) == Decimal("1")
        operation = reconcile_operation(session, "receipt_certainty", None, json.loads('"proven_not_sent"'))
        assert operation.status == "settled"
        assert Decimal(operation.settled_usd) == Decimal("0")


def test_reloaded_settled_receipt_rejects_a_lower_conflicting_amount(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        session.add(BudgetOperation(
            id="settled_receipt", run_id=run_id, owner_id="owner_test", kind="model",
            status="settled", reserved_usd="5", settled_usd="5", metadata_json={},
        ))
    with factory.begin() as session:
        with pytest.raises(BudgetDenied, match="^usage_reconciliation_conflict$"):
            reconcile_operation(session, "settled_receipt", "4", "reported")
        assert Decimal(session.get(BudgetOperation, "settled_receipt").settled_usd) == Decimal("5")


@pytest.mark.parametrize("bound,allowed", [("0.20", True), ("0.201", False)])
def test_one_finish_role_cannot_take_more_than_half_the_original_hold(factory, monkeypatch, bound, allowed):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.stage, step.role_slot_id = "synthesis", "synthesis"
        if allowed:
            authorize_operation(session, run, "finish_attempt", "model", bound, step=step, finish_role="synthesis")
            assert Decimal(session.get(BudgetOperation, f"finish_{run_id}").reserved_usd) == Decimal("0.20")
        else:
            with pytest.raises(BudgetDenied, match="^finish_money_reserve$"):
                authorize_operation(session, run, "finish_attempt", "model", bound, step=step, finish_role="synthesis")
            assert session.get(BudgetOperation, "finish_attempt") is None


def test_finish_role_rejects_an_underfunded_hold(factory, monkeypatch):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        hold = session.get(BudgetOperation, f"finish_{run_id}")
        hold.reserved_usd = "0.10"
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.stage, step.role_slot_id = "synthesis", "synthesis"
        with pytest.raises(BudgetDenied, match="^finish_money_reserve$"):
            authorize_operation(session, run, "underfunded_finish", "model", "0.20", step=step, finish_role="synthesis")
        assert session.get(BudgetOperation, "underfunded_finish") is None


@pytest.mark.parametrize("prior_model_call,allowed", [(True, False), (False, True)])
def test_request_cap_counts_model_calls_and_preserves_both_finish_roles(factory, monkeypatch, prior_model_call, allowed):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        run.configuration = {**run.configuration, "limits": {**run.configuration["limits"], "max_requests": 3}}
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        session.add(BudgetOperation(
            id="prior_request", run_id=run_id, owner_id=run.owner_id, kind="model",
            status="settled", reserved_usd="0", settled_usd="0",
            authorized_at=datetime.now(timezone.utc), metadata_json={"model_call": prior_model_call},
        ))
        session.flush()
        if allowed:
            authorize_operation(session, run, "next_request", "model", "0", step=step)
            assert session.get(BudgetOperation, "next_request") is not None
        else:
            with pytest.raises(BudgetDenied, match="^finish_call_reserve$"):
                authorize_operation(session, run, "next_request", "model", "0", step=step)
            assert session.get(BudgetOperation, "next_request") is None


@pytest.mark.parametrize("run_suffix,other_suffix", [("a", "z"), ("z", "a")])
def test_request_cap_does_not_count_a_different_runs_calls(factory, monkeypatch, run_suffix, other_suffix):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    run_id = seed(factory, suffix=run_suffix)
    other_id = seed(factory, suffix=other_suffix)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        run.configuration = {**run.configuration, "limits": {**run.configuration["limits"], "max_requests": 3}}
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        session.add(BudgetOperation(
            id="other_request", run_id=other_id, owner_id=run.owner_id, kind="model",
            status="settled", reserved_usd="0", settled_usd="0",
            authorized_at=datetime.now(timezone.utc), metadata_json={"model_call": True},
        ))
        session.flush()
        result = authorize_operation(session, run, "this_request", "model", "0", step=step)
        assert result.run_id == run_id


@pytest.mark.parametrize("configured,prior_calls,allowed", [(None, 21, True), (None, 22, False), (25, 21, True), (25, 22, False)])
def test_request_ceiling_preserves_two_final_calls_at_hard_maximum(factory, monkeypatch, configured, prior_calls, allowed):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        limits = dict(run.configuration["limits"])
        if configured is None:
            limits.pop("max_requests", None)
        else:
            limits["max_requests"] = configured
        run.configuration = {**run.configuration, "limits": limits}
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        session.add_all([
            BudgetOperation(
                id=f"prior_call_{index}", run_id=run_id, owner_id=run.owner_id, kind="model",
                status="settled", reserved_usd="0", settled_usd="0", metadata_json={"model_call": True},
            )
            for index in range(prior_calls)
        ])
        session.flush()
        if allowed:
            authorize_operation(session, run, "ceiling_request", "model", "0", step=step)
            assert session.get(BudgetOperation, "ceiling_request") is not None
        else:
            with pytest.raises(BudgetDenied, match="^finish_call_reserve$"):
                authorize_operation(session, run, "ceiling_request", "model", "0", step=step)
            assert session.get(BudgetOperation, "ceiling_request") is None


@pytest.mark.parametrize("run_suffix,other_suffix", [("a", "z"), ("z", "a")])
def test_step_attempt_limit_only_counts_calls_from_the_same_run(factory, run_suffix, other_suffix):
    run_id = seed(factory, suffix=run_suffix)
    other_id = seed(factory, suffix=other_suffix)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        session.add(Step(
            id="other_step", run_id=other_id, stage=step.stage, round=step.round,
            role_slot_id=step.role_slot_id, provider=step.provider, status="failed", calls=2,
        ))
        session.flush()
        result = authorize_operation(session, run, "isolated_call", "model", "0", step=step)
        assert result.run_id == run_id
        assert result.metadata_json["step_id"] == step.id


@pytest.mark.parametrize("field,value", [
    ("stage", "audit"), ("stage", "synthesis"),
    ("round", 0), ("round", 2),
    ("role_slot_id", "anthropic"), ("role_slot_id", "openai"),
])
def test_step_attempt_limit_does_not_include_other_step_families(factory, field, value):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.stage, step.round, step.role_slot_id = "proposal", 1, "google"
        family = {"stage": step.stage, "round": step.round, "role_slot_id": step.role_slot_id}
        family[field] = value
        session.add(Step(
            id="other_family", run_id=run_id, provider="openai", status="failed", calls=2, **family,
        ))
        session.flush()
        result = authorize_operation(session, run, "family_request", "model", "0", step=step)
        assert result.run_id == run_id


def test_step_family_refreshes_another_sessions_prior_attempt_count(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step_id = step.id
        session.add(Step(
            id="prior_retry", run_id=run_id, stage=step.stage, round=step.round,
            role_slot_id=step.role_slot_id, provider=step.provider, status="failed",
            calls=0, stage_iteration=1,
        ))
    with factory() as reader:
        run = reader.get(Run, run_id)
        step = reader.get(Step, step_id)
        cached = reader.get(Step, "prior_retry")
        assert cached.calls == 0
        with factory.begin() as writer:
            writer.get(Step, "prior_retry").calls = 2
        with pytest.raises(BudgetDenied, match="^step_call_limit$"):
            authorize_operation(reader, run, "stale_family", "model", "0", step=step)


def test_authorization_refreshes_selected_step_ownership(factory):
    run_id = seed(factory, suffix="a")
    other_id = seed(factory, suffix="z")
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
    with factory() as reader:
        run = reader.get(Run, run_id)
        step = reader.get(Step, step_id)
        assert step.run_id == run_id
        with factory.begin() as writer:
            writer.get(Step, step_id).run_id = other_id
        with pytest.raises(BudgetDenied, match="^foreign_step$"):
            authorize_operation(reader, run, "stale_owner", "model", "0", step=step)


def test_deleted_selected_step_is_not_replaced_by_another_step(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        for iteration, identity in enumerate(("a_selected", "z_other")):
            session.add(Step(
                id=identity, run_id=run_id, stage="proposal", round=1, stage_iteration=iteration,
                role_slot_id="openai", provider="openai", status="pending",
            ))
    with factory() as reader:
        run = reader.get(Run, run_id)
        step = reader.get(Step, "a_selected")
        with factory.begin() as writer:
            writer.delete(writer.get(Step, "a_selected"))
        with pytest.raises(BudgetDenied, match="^foreign_step$"):
            authorize_operation(reader, run, "deleted_step", "model", "0", step=step)


def test_non_model_authorization_does_not_increment_associated_step(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        assert step.calls == 0
        authorize_operation(session, run, "nonmodel", "public_search", "0", step=step, model_call=False)
        assert step.calls == 0


def test_authorization_refreshes_prior_request_count(factory, monkeypatch):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        run.configuration = {**run.configuration, "limits": {**run.configuration["limits"], "max_requests": 3}}
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
        session.add(BudgetOperation(
            id="prior_count", run_id=run_id, owner_id=run.owner_id, kind="model", status="settled",
            reserved_usd="0", settled_usd="0", metadata_json={"model_call": False},
        ))
    with factory() as reader:
        run = reader.get(Run, run_id)
        step = reader.get(Step, step_id)
        cached = reader.get(BudgetOperation, "prior_count")
        assert cached.metadata_json["model_call"] is False
        with factory.begin() as writer:
            writer.get(BudgetOperation, "prior_count").metadata_json = {"model_call": True}
        with pytest.raises(BudgetDenied, match="^finish_call_reserve$"):
            authorize_operation(reader, run, "stale_count", "model", "0", step=step)


def test_policy_refreshes_prior_exposure(factory, monkeypatch):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    config["limits"]["per_run_usd"] = "1"
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        session.add(BudgetOperation(
            id="prior_spend", run_id=run_id, owner_id="owner_test", kind="model", status="settled",
            reserved_usd="0", settled_usd="0", metadata_json={},
        ))
    with factory() as reader:
        run = reader.get(Run, run_id)
        cached = reader.get(BudgetOperation, "prior_spend")
        assert cached.settled_usd == "0"
        with factory.begin() as writer:
            writer.get(BudgetOperation, "prior_spend").settled_usd = "1"
        with pytest.raises(BudgetDenied, match="^run_budget_exhausted$"):
            _policy_checks(reader, run, Decimal("0.1"))


@pytest.mark.parametrize("run_suffix,other_suffix", [("a", "z"), ("z", "a")])
def test_authorization_rejects_foreign_steps_in_either_run_order(factory, run_suffix, other_suffix):
    run_id = seed(factory, suffix=run_suffix)
    other_id = seed(factory, suffix=other_suffix)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        foreign = Step(id="foreign_step", run_id=other_id, stage="proposal", role_slot_id="foreign", provider="openai", status="pending")
        session.add(foreign)
        session.flush()
        with pytest.raises(BudgetDenied, match="^foreign_step$"):
            authorize_operation(session, run, "foreign_request", "model", "0", step=foreign)
        assert session.get(BudgetOperation, "foreign_request") is None


def test_authorization_rejects_a_succeeded_run(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.status = "succeeded"
        with pytest.raises(BudgetDenied, match="^run_ineligible$"):
            authorize_operation(session, run, "terminal_request", "model", "0")
        assert session.get(BudgetOperation, "terminal_request") is None


@pytest.mark.parametrize("configured,calls,allowed", [(None, 1, True), (None, 2, False), (3, 2, False), (3, 3, False)])
def test_step_attempt_ceiling_enforces_default_and_hard_maximum(factory, monkeypatch, configured, calls, allowed):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        limits = dict(run.configuration["limits"])
        if configured is None:
            limits.pop("max_calls_per_step", None)
        else:
            limits["max_calls_per_step"] = configured
        run.configuration = {**run.configuration, "limits": limits}
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.calls = calls
        session.flush()
        if allowed:
            authorize_operation(session, run, "capped_attempt", "model", "0", step=step)
            assert session.get(BudgetOperation, "capped_attempt") is not None
        else:
            with pytest.raises(BudgetDenied, match="^step_call_limit$"):
                authorize_operation(session, run, "capped_attempt", "model", "0", step=step)
            assert session.get(BudgetOperation, "capped_attempt") is None


@pytest.mark.parametrize("first_suffix,second_suffix", [("a", "z"), ("z", "a")])
def test_finish_hold_is_unique_to_each_run(factory, first_suffix, second_suffix):
    first = seed(factory, suffix=first_suffix)
    second = seed(factory, suffix=second_suffix)
    with factory.begin() as session:
        first_hold = reserve_finish(session, session.get(Run, first))
        second_hold = reserve_finish(session, session.get(Run, second))
        assert first_hold.id == f"finish_{first}"
        assert second_hold.id == f"finish_{second}"
        assert second_hold.run_id == second


@pytest.mark.parametrize("other_suffix,current_suffix", [("a", "z"), ("z", "a")])
def test_other_run_spending_does_not_consume_the_per_run_cap(factory, monkeypatch, other_suffix, current_suffix):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    config["limits"]["per_run_usd"] = "1"
    other = seed(factory, suffix=other_suffix, config=config)
    current = seed(factory, suffix=current_suffix, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        session.add(BudgetOperation(
            id="unrelated_charge", run_id=other, owner_id="owner_test", kind="model",
            status="settled", reserved_usd="1", settled_usd="1",
            authorized_at=datetime.now(timezone.utc), metadata_json={},
        ))
        session.flush()
        _policy_checks(session, session.get(Run, current), Decimal("0.1"))


@pytest.mark.parametrize("other_owner", ["owner_a", "owner_z"])
def test_other_owner_spending_does_not_consume_the_owner_cap(factory, monkeypatch, other_owner):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        deployment, owner = lock_policies(session, "owner_test")
        for policy in (deployment, owner):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        owner.metadata_json = {"daily_usd": "1", "monthly_usd": "50"}
        session.add(BudgetOperation(
            id="other_owner_charge", run_id=None, owner_id=other_owner, kind="model",
            status="settled", reserved_usd="1", settled_usd="1",
            authorized_at=datetime.now(timezone.utc), metadata_json={},
        ))
        session.flush()
        _policy_checks(session, session.get(Run, run_id), Decimal("0.1"))


@pytest.mark.parametrize(
    "finish_role,remaining,allowed",
    [(None, 226, False), (None, 226.1, True), ("synthesis", 125.5, False), ("synthesis", 125.6, True)],
)
def test_finish_time_reserve_is_protected_at_the_exact_boundary(factory, monkeypatch, finish_role, remaining, allowed):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        if finish_role:
            step.stage, step.role_slot_id = finish_role, finish_role
        monkeypatch.setattr("round_robin.orchestration.budget.remaining_active", lambda _: remaining)
        monkeypatch.setattr("round_robin.orchestration.protocol.mandatory_tail_seconds", lambda *_: 201)
        if allowed:
            authorize_operation(session, run, "timed_request", "model", "0", step=step, finish_role=finish_role)
            assert session.get(BudgetOperation, "timed_request") is not None
        else:
            with pytest.raises(BudgetDenied, match="^finish_time_reserve$"):
                authorize_operation(session, run, "timed_request", "model", "0", step=step, finish_role=finish_role)
            assert session.get(BudgetOperation, "timed_request") is None


@pytest.mark.parametrize("legacy,tail,remaining,allowed", [
    (True, None, 215, False), (True, None, 215.1, True),
    (False, 100, 150, False), (True, 100, 150, True),
])
def test_finish_time_supports_legacy_and_current_policy_defaults(factory, monkeypatch, legacy, tail, remaining, allowed):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        config = dict(run.configuration)
        if legacy:
            config.pop("discussion_policy", None)
        finish_policy = dict(config["finish_policy"])
        if tail is None:
            finish_policy.pop("reserved_active_seconds", None)
        else:
            finish_policy["reserved_active_seconds"] = tail
        config["finish_policy"] = finish_policy
        run.configuration = config
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        monkeypatch.setattr("round_robin.orchestration.budget.remaining_active", lambda _: remaining)
        if allowed:
            authorize_operation(session, run, "legacy_timed_request", "model", "0", step=step)
            assert session.get(BudgetOperation, "legacy_timed_request") is not None
        else:
            with pytest.raises(BudgetDenied, match="^finish_time_reserve$"):
                authorize_operation(session, run, "legacy_timed_request", "model", "0", step=step)
            assert session.get(BudgetOperation, "legacy_timed_request") is None


@pytest.mark.parametrize("funded,deployment_locked,owner_locked", [
    (True, True, False), (True, False, True), (False, False, False),
    (1, False, False), (None, False, False),
])
def test_each_funding_or_spending_lock_independently_denies_authority(
    factory, monkeypatch, funded, deployment_locked, owner_locked,
):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=funded)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        deployment, owner = lock_policies(session, "owner_test")
        deployment.spending_locked = deployment_locked
        owner.spending_locked = owner_locked
        with pytest.raises(BudgetDenied, match="^spending_locked$"):
            _policy_checks(session, session.get(Run, run_id), Decimal("0"))


@pytest.mark.parametrize("scope", ["run", "deployment", "owner"])
def test_money_ceiling_allows_exact_cap_and_denies_above(factory, monkeypatch, scope):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    config["limits"]["per_run_usd"] = "1" if scope == "run" else "10"
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        deployment, owner = lock_policies(session, "owner_test")
        for policy, label in ((deployment, "deployment"), (owner, "owner")):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "1" if label == scope else "10", "monthly_usd": "50"}
        run = session.get(Run, run_id)
        _policy_checks(session, run, Decimal("1"))
        code = "run_budget_exhausted" if scope == "run" else f"{scope}_daily_budget_exhausted"
        with pytest.raises(BudgetDenied, match=f"^{code}$"):
            _policy_checks(session, run, Decimal("1.01"))


@pytest.mark.parametrize("scope", ["deployment", "owner"])
def test_missing_daily_limit_uses_policy_limit_and_missing_monthly_limit_fails_closed(factory, monkeypatch, scope):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        deployment, owner = lock_policies(session, "owner_test")
        for policy in (deployment, owner):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        target = deployment if scope == "deployment" else owner
        target.limit_usd = "1"
        target.metadata_json = {"monthly_usd": "50"}
        run = session.get(Run, run_id)
        _policy_checks(session, run, Decimal("1"))
        with pytest.raises(BudgetDenied, match=f"^{scope}_daily_budget_exhausted$"):
            _policy_checks(session, run, Decimal("1.01"))
        target.metadata_json = {"daily_usd": "10"}
        with pytest.raises(BudgetDenied, match=f"^{scope}_monthly_budget_exhausted$"):
            _policy_checks(session, run, Decimal("0.01"))


def test_direct_policy_checks_do_not_inherit_caller_decimal_precision(factory, monkeypatch):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    cap = "1000000000000"
    for key in ("per_run_usd", "owner_daily_usd", "owner_monthly_usd", "deployment_daily_usd", "deployment_monthly_usd"):
        config["limits"][key] = cap
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = cap
            policy.metadata_json = {"daily_usd": cap, "monthly_usd": cap}
        session.add(BudgetOperation(
            id="full_cap", run_id=run_id, owner_id="owner_test", kind="model",
            status="settled", reserved_usd=cap, settled_usd=cap,
            authorized_at=datetime.now(timezone.utc), metadata_json={},
        ))
        session.flush()
        with localcontext() as ambient:
            ambient.prec = 6
            ambient.traps[Inexact] = False
            with pytest.raises(BudgetDenied, match="^run_budget_exhausted$"):
                _policy_checks(session, session.get(Run, run_id), Decimal("0.000000000000000001"))


@pytest.mark.parametrize("mode", ["simulated", "live"])
def test_execution_epoch_requires_value_equality_in_both_modes(factory, monkeypatch, mode):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode=mode, funded=True)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        deployment, owner = lock_policies(session, "owner_test")
        for policy in (deployment, owner):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        run = session.get(Run, run_id)
        run.deployment_epoch = int("1000")
        deployment.metadata_json = {**deployment.metadata_json, "deployment_epoch": int("1000")}
        _policy_checks(session, run, Decimal("0"))
        deployment.metadata_json = {**deployment.metadata_json, "deployment_epoch": 999}
        with pytest.raises(BudgetDenied, match="^execution_epoch_changed$"):
            _policy_checks(session, run, Decimal("0"))


@pytest.mark.parametrize("first_role,second_role", [("synthesis", "audit"), ("audit", "synthesis")])
def test_final_role_transfer_keeps_the_other_role_reserved(factory, monkeypatch, first_role, second_role):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        run.configuration = {**run.configuration, "limits": {**run.configuration["limits"], "max_requests": 2}}
        first = session.scalar(select(Step).where(Step.run_id == run_id))
        first.stage, first.role_slot_id = first_role, first_role
        authorize_operation(session, run, "first_final_request", "model", "0", step=first, finish_role=first_role)
        hold = session.get(BudgetOperation, f"finish_{run_id}")
        assert hold.metadata_json["roles_remaining"] == [second_role]

        other = Step(id="other_work", run_id=run_id, stage="proposal", role_slot_id="other", provider="openai", status="pending")
        second = Step(id="second_final_work", run_id=run_id, stage=second_role, role_slot_id=second_role, provider="openai", status="pending")
        session.add_all([other, second])
        session.flush()
        with pytest.raises(BudgetDenied, match="^finish_call_reserve$"):
            authorize_operation(session, run, "other_request", "model", "0", step=other)
        authorize_operation(session, run, "second_final_request", "model", "0", step=second, finish_role=second_role)
        assert hold.metadata_json["roles_remaining"] == []
