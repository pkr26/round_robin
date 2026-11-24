"""Adversarial regression cases discovered during the implementation audit."""

from datetime import timedelta
import threading
import time

import httpcore
import httpx
import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import drain, factory, seed
from backend.tests.test_providers import configuration, packet, response_for
from round_robin.database.models import Attempt, BudgetOperation, Run, Step, utcnow
from round_robin.dispatcher.service import reconcile_once
from round_robin.orchestration.admission import lock_work_run, provider_capacity_available
from round_robin.orchestration.budget import authorize_operation
from round_robin.orchestration.controller import advance_run, finalize_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.http import HTTPProvider
from round_robin.providers.transport import _addresses
from round_robin.fixtures import sample_brief
from round_robin.orchestration.reporting import build_report
from round_robin.orchestration.policies import retryable_steps
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.briefs import DraftBrief


def test_stalled_dns_does_not_outlive_the_callers_deadline(monkeypatch):
    release, finished = threading.Event(), threading.Event()
    def stalled_dns(*args, **kwargs):
        try:
            release.wait(2)
            return []
        finally:
            finished.set()
    monkeypatch.setattr("round_robin.providers.transport.socket.getaddrinfo", stalled_dns)
    began = time.monotonic()
    try:
        with pytest.raises(httpcore.ConnectTimeout):
            _addresses("synthetic", 443, began + .05)
        assert time.monotonic() - began < .5
    finally:
        release.set()
        assert finished.wait(1)


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
@pytest.mark.parametrize("usage", [[1], "unexpected", 1, True])
def test_malformed_usage_keeps_valid_content_and_unknown_exposure(provider, usage):
    raw = response_for(provider)
    raw["usageMetadata" if provider == "google" else "usage"] = usage
    adapter = HTTPProvider(provider, configuration(provider), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw)), api_key="synthetic")
    result = adapter.generate(packet())
    assert result.output.recommendation_option_id == "option_a"
    assert result.cost_usd is None
    assert result.usage == {}


@pytest.mark.parametrize("field,value", [("cache_creation_input_tokens", -10), ("cache_read_input_tokens", -10), ("cache_creation_input_tokens", True), ("cache_read_input_tokens", "10")])
def test_malformed_cache_usage_cannot_underbook_a_settled_charge(field, value):
    raw = response_for("anthropic")
    raw["usage"][field] = value
    adapter = HTTPProvider("anthropic", configuration("anthropic"), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw)), api_key="synthetic")
    result = adapter.generate(packet())
    assert result.cost_usd is None


@pytest.mark.parametrize("raw", [[], "unexpected", 1, None, {"output": [None]}])
def test_malformed_envelope_has_a_bounded_classified_recovery(raw):
    adapter = HTTPProvider("openai", configuration("openai"), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw)), api_key="synthetic")
    with pytest.raises(ProviderFailure) as error:
        adapter.generate(packet())
    assert error.value.code == "malformed_response" and error.value.retryable


@pytest.mark.parametrize("mutation", ["nul", "surrogate", "long_dissent", "duplicate_constraint"])
def test_unpersistable_provider_outputs_are_controlled_schema_failures_with_usage(mutation):
    raw = response_for("openai")
    import json
    content = json.loads(raw["output"][0]["content"][0]["text"])
    if mutation == "nul":
        content["summary"] = "Unpersistable\x00response"
    elif mutation == "surrogate":
        content["summary"] = "Unpersistable\ud800response"
    elif mutation == "long_dissent":
        content["dissent"] = ["x" * 501]
    else:
        constraint = {"option_id": "option_a", "requirement_id": packet().brief["hard_requirements"][0]["id"], "result": "unknown", "reason": "Unknown", "basis_refs": []}
        content["constraints"] = [constraint, constraint]
    raw["output"][0]["content"][0]["text"] = json.dumps(content)
    adapter = HTTPProvider("openai", configuration("openai"), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw)), api_key="synthetic")
    with pytest.raises(ProviderFailure) as error:
        adapter.generate(packet())
    assert error.value.code == "schema_invalid" and error.value.retryable
    assert error.value.cost_usd is not None and error.value.cost_usd > 0


def test_unpersistable_usage_metadata_keeps_content_and_conservative_uncertainty():
    raw = response_for("openai")
    raw["usage"]["untrusted_metadata"] = "contains\x00nul"
    adapter = HTTPProvider("openai", configuration("openai"), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw)), api_key="synthetic")
    result = adapter.generate(packet())
    assert result.output.recommendation_option_id == "option_a"
    assert result.cost_usd is None and result.usage == {}


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_impossible_usage_counts_cannot_overflow_report_accounting(provider):
    raw = response_for(provider)
    if provider == "google":
        raw["usageMetadata"] = {"promptTokenCount": 100, "totalTokenCount": 10 ** 100}
    else:
        raw["usage"]["output_tokens"] = 10 ** 100
    adapter = HTTPProvider(provider, configuration(provider), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw)), api_key="synthetic")
    result = adapter.generate(packet())
    assert result.cost_usd is None and result.output.recommendation_option_id == "option_a"


def maximum_brief(*, invalid_currency=False):
    body = sample_brief().model_dump(mode="json")
    for index, option in enumerate(body["options"]):
        option["id"] = "option_" + str(index) + "x" * 88
        option["label"] = "L" * 500
        template = option["cost_components"][0]
        option["cost_components"] = [{**template, "id": f"component_{component}", "label": "C" * 500, "amount": None, "unknown_reason": "R" * 500, "kind": "other", "source_ref": "source_maximum", "currency": "EUR" if invalid_currency else body["currency"]} for component in range(30)]
    return DraftBrief.model_validate(body)


@pytest.mark.parametrize("invalid_currency", [False, True])
def test_maximum_valid_brief_still_publishes_an_honest_report(invalid_currency):
    brief = maximum_brief(invalid_currency=invalid_currency)
    run = Run(id="run_maximum", input_snapshot=brief.model_dump(mode="json"), configuration={"mode": "simulated", "evidence_prepared": True}, event_sequence=0)
    report = build_report(run, [], status="failed", stop_reason="test_no_review", report_id="report_maximum", version=1, usage={"simulated": True, "status": "reported", "actual_cost": "0", "conservative_exposure_usd": "0", "unknown_operation_ids": [], "request_count": 0, "currency": "USD"}, evidence=[{"id": "source_maximum"}])
    assert report["readiness"] is None and report["recommendation"] is None
    assert len(report["comparison"]) == len(brief.options)
    assert all(item["total"] is None for item in report["comparison"])
    if not invalid_currency:
        assert len(report["comparison"][0]["missing_components"]) == 35
        assert len(report["calculations"][0]["missing_components"]) == 35
    assert len({item["id"] for item in report["calculations"]}) == len(brief.options)
    assert all(len(item["id"]) <= 96 for item in report["calculations"] + report["essential_dependencies"])


def test_maximum_length_model_disagreement_is_retained_in_a_publishable_report():
    brief = sample_brief()
    run = Run(id="run_disagreement", input_snapshot=brief.model_dump(mode="json"), configuration={"mode": "live", "evidence_prepared": True}, event_sequence=0)
    scoped = {component.source_ref for option in brief.options for component in option.cost_components}
    reference = sorted(scoped)[0]
    base = {"summary": "Conditional recommendation", "recommendation_option_id": brief.options[0].id, "constraints": [], "critical_defects": [], "dissent": [], "audit_passed": None}
    proposed = {"option_id": brief.options[0].id, "requirement_id": "requirement_goal", "result": "pass", "reason": "S" * 500, "basis_refs": [reference]}
    checked = {**proposed, "result": "unknown", "reason": "A" * 500}
    steps = [Step(id="step_synthesis", provider="openai", stage="synthesis", status="succeeded", result_json={"output": {**base, "constraints": [proposed]}}), Step(id="step_audit", provider="google", stage="audit", status="succeeded", result_json={"output": {**base, "audit_passed": True, "constraints": [checked]}})]
    report = build_report(run, steps, status="completed_with_limitations", stop_reason="test_disagreement", report_id="report_disagreement", version=1, usage={"simulated": False, "status": "reported", "actual_cost": "0", "conservative_exposure_usd": "0", "unknown_operation_ids": [], "request_count": 0, "currency": "USD"}, evidence=[{"id": identity} for identity in scoped])
    result = next(result for result in report["comparison"][0]["constraint_results"] if result["requirement_id"] == "requirement_goal")
    assert result["result"] == "unknown" and "disagree" in result["reason"]
    assert len(result["reason"]) <= 500


def test_cancelled_lost_worker_releases_capacity_after_lease_but_keeps_exposure(factory):
    run_id = seed(factory)
    with factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        steps = [step]
        for index in range(1, 3):
            additional = Step(id=f"step_capacity_{index}", run_id=run.id, stage=step.stage,
                              role_slot_id=f"capacity_{index}", provider="openai", status="pending")
            session.add(additional)
            steps.append(additional)
        session.flush()
        for index, owned_step in enumerate(steps):
            operation_id = f"cancelled_attempt_{index}"
            authorize_operation(session, run, operation_id, "model", "0", step=owned_step, model_call=True)
            session.add(Attempt(id=operation_id, run_id=run.id, step_id=owned_step.id, provider="openai", status="authorized", reserved_usd="0", configuration={}))
            owned_step.status = "authorized"
            owned_step.lease_owner = "lost_executor"
            owned_step.lease_expires_at = utcnow() + timedelta(seconds=150)
        run.status = "cancel_requested"
        finalize_run(session, run)
    with factory() as session:
        # Recently authorized physical calls still occupy capacity after Stop.
        assert not provider_capacity_available(session, "google")
    with factory() as session, session.begin():
        for attempt in session.scalars(select(Attempt)):
            attempt.authorized_at = utcnow() - timedelta(seconds=151)
    reconcile_once(session_factory=factory)
    with factory() as session:
        assert provider_capacity_available(session, "google")
        assert all(attempt.usage_certainty == "unknown" and attempt.completed_at is not None for attempt in session.scalars(select(Attempt)))
        operations = list(session.scalars(select(BudgetOperation).where(BudgetOperation.id.in_([f"cancelled_attempt_{index}" for index in range(3)]))))
        assert len(operations) == 3 and all(operation.status == "unknown" for operation in operations)
        assert session.get(Run, run_id).status == "cancelled"


def test_permanent_final_stage_failure_releases_the_unusable_finish_pool(factory):
    run_id = seed(factory)
    class RefusingSynthesis(SimulatedProvider):
        def generate(self, request):
            if request.stage == "synthesis":
                raise ProviderFailure("refusal", cost_usd=0)
            return super().generate(request)
    drain(factory, {provider: RefusingSynthesis(provider) for provider in ("openai", "anthropic", "google")})
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == "completed_with_limitations"
        assert session.scalar(select(Step).where(Step.run_id == run_id, Step.stage == "audit")) is None
        assert not retryable_steps(session, run)
        assert session.get(BudgetOperation, f"finish_{run_id}").status == "released"


def test_legacy_synthesis_retry_cannot_reuse_an_audit_of_different_content(factory):
    run_id = seed(factory)
    with factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        synthesis = session.scalar(select(Step).where(Step.run_id == run_id))
        synthesis.stage = "synthesis"
        synthesis.status, synthesis.error_code, synthesis.calls, synthesis.infrastructure_claims = "failed", "timeout", 1, 1
        run.status = "completed_with_limitations"
        assert retryable_steps(session, run) == [synthesis]
        session.add(Step(id="step_old_audit", run_id=run_id, stage="audit", role_slot_id="audit", provider="google", status="succeeded", calls=1, infrastructure_claims=1, generation=run.recovery_generation))
        session.flush()
        assert retryable_steps(session, run) == []


def test_retry_and_finish_retention_share_remaining_time_and_actual_fault_codes(factory):
    run_id = seed(factory)
    with factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.stage = "synthesis"
        step.status, step.error_code, step.calls, step.infrastructure_claims = "failed", "worker_lost_after_authorization", 1, 1
        run.status = "failed"
        assert retryable_steps(session, run) == [step]
        run.active_seconds, run.active_started_at = 520, None
        assert not retryable_steps(session, run)
        finalize_run(session, run, reason="synthesis_unavailable")
        assert session.get(BudgetOperation, f"finish_{run_id}").status == "released"
