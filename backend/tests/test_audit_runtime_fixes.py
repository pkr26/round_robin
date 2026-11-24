"""Regressions for bounded admission, exact input sizing and monetary semantics."""
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib

import pytest
from sqlalchemy import func, select

from backend.tests.test_api import application, client, command, create, preflight, start
from backend.tests.test_evidence_regressions import complete_brief
from backend.tests.test_orchestration import factory, seed
from backend.tests.test_research_service import PageFixture, SearchFixture, research_run
from round_robin.api.events import retry_eligibility
from round_robin.database.models import Attempt, BudgetOperation, Outbox, Run, Step, utcnow
from round_robin.database.services import enqueue, lock_policies
from round_robin.dispatcher.service import dispatch_once, reconcile_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import operations_exposure
from round_robin.orchestration.controller import advance_run
from round_robin.orchestration.policies import retryable_steps
from round_robin.orchestration.reporting import comparison_data
from round_robin.research.calculator import CalculationError, lifecycle_total
from round_robin.research.service import execute_research, reconcile_public_research, schedule_research


def test_research_uses_frozen_finish_reserve_instead_of_default(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        run.configuration = {**run.configuration,
            "limits": {**run.configuration["limits"], "active_seconds": 200},
            "finish_policy": {**run.configuration["finish_policy"], "reserved_active_seconds": 50}}
        assert schedule_research(session, run)
    search = SearchFixture()
    assert execute_research("run_local", session_factory=research_run, search=search,
        transport=PageFixture(), resolver=lambda _: ["93.184.216.34"])
    assert len(search.queries) == 3
    with research_run() as session:
        assert session.get(Run, "run_local").configuration["research_state"] == "done"


@pytest.mark.parametrize("via", ["worker", "reconciler"])
def test_unstarted_research_settles_when_only_finishing_time_remains(research_run, via):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert schedule_research(session, run)
        run.active_seconds, run.active_started_at = 400, None
    search = SearchFixture()
    if via == "worker":
        assert not execute_research("run_local", session_factory=research_run, search=search)
    else:
        with research_run.begin() as session:
            assert reconcile_public_research(session, session.get(Run, "run_local"))
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert run.configuration["research_state"] == "done"
        assert run.configuration["research_summary"]["reason"] == "finish_time_reserve"
        assert not schedule_research(session, run)
        assert session.scalar(select(Outbox.id).where(Outbox.kind == "advance"))
        assert session.scalar(select(func.count()).select_from(BudgetOperation)) == 0
    assert search.queries == []


def select_text(client, meeting, text):
    encoded = text.encode()
    uploaded = client.post(f"/api/meetings/{meeting['id']}/uploads", content=encoded, headers={
        "Content-Type": "text/plain", "X-Command-Id": command()["command_id"],
        "X-Expected-Revision": "1", "X-Filename": "bounded.txt",
        "X-Content-SHA256": hashlib.sha256(encoded).hexdigest()})
    assert uploaded.status_code == 201, uploaded.text
    brief = {**meeting["draft"], "selected_attachment_ids": [uploaded.json()["id"]]}
    response = client.patch(f"/api/meetings/{meeting['id']}/draft", json=command(expected_revision=1, brief=brief))
    assert response.status_code == 200, response.text
    return {**meeting, "draft": response.json()["brief"], "draft_revision": response.json()["revision"]}


@pytest.mark.parametrize("extra_character", [False, True])
def test_preflight_and_start_share_passage_and_downstream_byte_capacity(client, application, extra_character):
    meeting = create(client)
    aliases = {component["source_ref"] for option in meeting["draft"]["options"] for component in option["cost_components"]}
    characters = (24 - len(aliases)) * 2000 + int(extra_character)
    meeting = select_text(client, meeting, "x" * characters)
    quote = preflight(client, meeting)
    # A maximum-size 24-passage input leaves no admitted room for the bounded
    # synthesis/audit footprint. Count and encoded bytes are independent caps.
    assert quote["blockers"]
    response = start(client, meeting, quote)
    assert response.status_code == 409, response.text
    if extra_character:
        assert response.json()["error"]["code"] == "context_limit_selected_passages"
    else:
        assert response.json()["error"]["code"] == "context_limit_packet_bytes"
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0
        assert session.scalar(select(func.count()).select_from(Attempt)) == 0


def test_oversized_structured_source_is_rejected_before_intake(client, application):
    from round_robin.fixtures import sample_brief
    brief = sample_brief().model_dump(mode="json")
    for option in brief["options"]:
        for component in option["cost_components"]:
            component.update(source_ref="same_source", label="Long quote line " * 25)
    meeting = create(client, brief)
    quote = preflight(client, meeting)
    assert any("2,000" in message for message in quote["blockers"])
    response = start(client, meeting, quote)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "context_limit_structured_quote"
    with application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0


@pytest.mark.parametrize("currency,amount,expected", [
    ("KWD", "1.005", "1.005"), ("BHD", "1.0055", "1.006"),
    ("JPY", "1.5", "2"), ("CLF", "1.00015", "1.0002"), ("USD", "1.005", "1.00")])
def test_lifecycle_totals_use_the_currency_minor_unit(currency, amount, expected):
    brief = complete_brief()
    option = brief.options[0].model_copy(deep=True)
    option.cost_components = [option.cost_components[0].model_copy(update={
        "amount": amount, "currency": currency, "quantity": "1", "frequency": "once"})]
    result = lifecycle_total(option, 1, currency, calculation_id="calc_minor_units", as_of=date(2026, 11, 16))
    assert result.output == result.known_subtotal == expected


def test_three_decimal_currency_cannot_pass_a_lower_hard_budget():
    brief = complete_brief().model_dump(mode="json")
    brief["currency"], brief["budget"] = "KWD", "1.001"
    brief["hard_requirements"] = [{"id": "cost_limit", "description": "Within the accepted limit", "origin": "user_supplied", "metric": "total_cost", "operator": "lte", "threshold": "1.001"}]
    for option in brief["options"]:
        for component in option["cost_components"]:
            component.update(currency="KWD", amount="0", unknown_reason=None, status="not_applicable")
        option["cost_components"][0].update(amount="1.005", status="included", frequency="once", quantity="1")
    from round_robin.schemas.briefs import DraftBrief
    comparisons, _ = comparison_data(DraftBrief.model_validate(brief), as_of=date(2026, 11, 16))
    assert all(item["total"] == "1.005" and item["constraint_results"][0]["result"] == "fail" for item in comparisons)


def test_unsupported_currency_is_a_preflight_blocker_not_an_implicit_cent(client):
    from round_robin.fixtures import sample_brief
    brief = sample_brief().model_dump(mode="json")
    brief["currency"] = "XYZ"
    for option in brief["options"]:
        for component in option["cost_components"]:
            component["currency"] = "XYZ"
    meeting = create(client, brief)
    quote = preflight(client, meeting)
    assert quote["blockers"]
    assert start(client, meeting, quote).json()["error"]["code"] == "unsupported_currency"
    with pytest.raises(CalculationError, match="minor unit"):
        lifecycle_total(sample_brief().options[0], 1, "XYZ", calculation_id="calc_unsupported")


def exercise_waiting_advances(factory, monkeypatch, *, synchronous_sender=False):
    now = [utcnow()]
    for name in ("database.services", "orchestration.admission", "orchestration.controller", "orchestration.budget", "dispatcher.service"):
        module = importlib.import_module("round_robin." + name)
        monkeypatch.setattr(module, "utcnow", lambda: now[0])
    run_id = seed(factory)
    with factory.begin() as session:
        deployment, _ = lock_policies(session, "owner_test")
        deployment.metadata_json = {"draining": True}
    deliveries = []
    def deliver(kind, payload):
        deliveries.append((kind, payload))
        if synchronous_sender:
            with factory.begin() as session:
                advance_run(session, lock_work_run(session, payload))
    for _ in range(91):
        reconcile_once(session_factory=factory)
        before = len(deliveries)
        dispatch_once(session_factory=factory, sender=deliver)
        if not synchronous_sender:
            for kind, payload in deliveries[before:]:
                assert kind == "advance"
                with factory.begin() as session:
                    advance_run(session, lock_work_run(session, payload))
        now[0] += timedelta(seconds=1)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Outbox)) == 1
        assert session.get(Run, run_id).status == "queued"
    assert 1 <= len(deliveries) <= 7  # Lost-delivery recovery remains bounded.
    with factory.begin() as session:
        deployment, _ = lock_policies(session, "owner_test")
        deployment.metadata_json = {"draining": False}
    reconcile_once(session_factory=factory)
    with factory() as session:
        assert session.get(Run, run_id).status == "running"
        assert session.scalar(select(func.count()).select_from(Step)) == 1


def test_waiting_advances_do_not_multiply_and_resume_after_drain(factory, monkeypatch):
    exercise_waiting_advances(factory, monkeypatch)


def test_reconciler_retires_legacy_duplicate_advance_records(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        deployment, _ = lock_policies(session, "owner_test")
        deployment.metadata_json = {"draining": True}
        for index in range(6):
            session.add(Outbox(id=f"work_legacy_{index}", run_id=run_id, kind="advance", status="published", payload={"run_id": run_id}, due_at=utcnow(), published_at=utcnow()))
    reconcile_once(session_factory=factory)
    with factory() as session:
        active = list(session.scalars(select(Outbox).where(Outbox.kind == "advance", Outbox.status.in_(["pending", "published"]))))
        assert len(active) == 1


def retained_terminal_run(factory, *, expires_at):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.stage, step.status, step.error_code = "synthesis", "failed", "worker_lost_after_authorization"
        step.calls, step.infrastructure_claims = 1, 1
        run.status, run.active_started_at = "completed_with_limitations", None
        run.configuration = {**run.configuration, "mode": "live", "expires_at": expires_at.isoformat()}
        hold = session.get(BudgetOperation, f"finish_{run_id}")
        hold.reserved_usd = "0.38"
        session.add(BudgetOperation(id="attempt_still_unknown", run_id=run_id, owner_id=run.owner_id, kind="model", status="unknown", reserved_usd="0.02", metadata_json={"model_call": True}))
    return run_id


def test_retry_eligibility_exposes_retryable_terminal_step(factory):
    run_id = retained_terminal_run(factory, expires_at=utcnow() + timedelta(hours=1))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        assert retry_eligibility(session, run) is True
        step.error_code = "authentication"
        assert retry_eligibility(session, run) is False


def test_expired_terminal_retry_releases_only_unused_hold(factory, monkeypatch):
    now = utcnow()
    run_id = retained_terminal_run(factory, expires_at=now + timedelta(hours=1))
    with factory() as session:
        assert retryable_steps(session, session.get(Run, run_id))
    monkeypatch.setattr("round_robin.orchestration.policies.utcnow", lambda: now + timedelta(days=2))
    for _ in range(3):
        reconcile_once(session_factory=factory)
    with factory() as session:
        assert not retryable_steps(session, session.get(Run, run_id))
        assert session.get(BudgetOperation, f"finish_{run_id}").status == "released"
        unknown = session.get(BudgetOperation, "attempt_still_unknown")
        assert unknown.status == "unknown" and unknown.reserved_usd == "0.02"
        assert operations_exposure(list(session.scalars(select(BudgetOperation))), period="daily", now=now + timedelta(days=2)) == Decimal("0.02")


def test_terminal_cancel_abandons_retry_without_releasing_unknown_exposure(client, application):
    meeting = create(client)
    accepted = start(client, meeting).json()
    with application.state.session_factory.begin() as session:
        run = session.get(Run, accepted["id"])
        run.status = "completed_with_limitations"
        run.configuration = {**run.configuration, "mode": "live"}
        session.add(BudgetOperation(id=f"finish_{run.id}", run_id=run.id, owner_id=run.owner_id, kind="finish_hold", status="held", reserved_usd="0.38", metadata_json={"roles_remaining": ["audit"], "initial_reserved_usd": "0.40"}))
        session.add(BudgetOperation(id="terminal_unknown", run_id=run.id, owner_id=run.owner_id, kind="model", status="unknown", reserved_usd="0.02", metadata_json={"model_call": True}))
    response = client.post(f"/api/runs/{accepted['id']}/cancel", json=command(expected_run_version=accepted["version"]))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed_with_limitations"
    with application.state.session_factory() as session:
        assert session.get(Run, accepted["id"]).configuration["retry_abandoned"]
        assert session.get(BudgetOperation, f"finish_{accepted['id']}").status == "released"
        assert session.get(BudgetOperation, "terminal_unknown").status == "unknown"
