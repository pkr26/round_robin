"""Independent runtime audit regressions: no live providers or network I/O."""
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import threading

import httpcore
import httpx
import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory, seed
from backend.tests.test_providers import configuration, output, packet, response_for
from round_robin.database.models import Attempt, BudgetOperation, EvidenceRecord, Outbox, Run, Step, Upload, utcnow
from round_robin.dispatcher.service import dispatch_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run, prepare_evidence
from round_robin.providers.base import ProviderFailure
from round_robin.providers.http import HTTPProvider
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


def adapter_for(provider, raw, *, status=200, headers=None):
    calls = []
    def send(request):
        calls.append(request)
        return httpx.Response(status, headers=headers, json=raw)
    return HTTPProvider(provider, configuration(provider), transport=httpx.MockTransport(send), api_key="synthetic"), calls


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
@pytest.mark.parametrize("failure", ["refusal", "truncated", "empty"])
def test_billable_failure_preserves_valid_usage_and_request_identity(provider, failure):
    raw = response_for(provider)
    if provider == "openai":
        if failure == "refusal":
            raw["output"][0]["content"] = [{"type": "refusal", "refusal": "Unavailable"}]
        elif failure == "truncated":
            raw["status"] = "incomplete"
        else:
            raw["output"] = []
    elif provider == "anthropic":
        if failure != "empty":
            raw["stop_reason"] = "refusal" if failure == "refusal" else "max_tokens"
        else:
            raw["content"] = []
    else:
        if failure != "empty":
            raw["candidates"][0]["finishReason"] = "SAFETY" if failure == "refusal" else "MAX_TOKENS"
        else:
            raw["candidates"] = []
    adapter, calls = adapter_for(provider, raw, headers={"x-request-id": "request_billed_failure"})
    with pytest.raises(ProviderFailure) as error:
        adapter.generate(packet())
    assert error.value.code == failure
    assert error.value.cost_usd == Decimal("0.0007")
    assert error.value.provider_request_id == "request_billed_failure"
    assert error.value.retryable is (failure != "refusal")
    assert error.value.may_have_sent is True
    assert len(calls) == 1


@pytest.mark.parametrize("raw", [[1], None, {"output": [None]}, {"status": "completed", "output": []}])
def test_malformed_envelope_keeps_http_request_identity_for_final_receipt(raw):
    adapter, calls = adapter_for("openai", raw, headers={"x-request-id": "request_invalid_envelope"})
    with pytest.raises(ProviderFailure) as error:
        adapter.generate(packet())
    assert error.value.provider_request_id == "request_invalid_envelope"
    assert error.value.cost_usd is None and error.value.may_have_sent
    assert len(calls) == 1


def test_read_timeout_after_response_headers_keeps_provider_receipt_identity():
    class TimeoutStream(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadTimeout("private upstream error")
            yield b""
    adapter = HTTPProvider("openai", configuration("openai"), transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"x-request-id": "request_timeout"}, stream=TimeoutStream())), api_key="synthetic")
    with pytest.raises(ProviderFailure) as error:
        adapter.generate(packet())
    assert error.value.code == "timeout" and error.value.provider_request_id == "request_timeout"
    assert "private upstream" not in str(error.value)


def test_structured_quote_alias_cannot_overwrite_a_selected_upload_passage(factory):
    brief = packet().brief
    upload_id = "upload_selected"
    # The source alias and upload locator previously shared one hash namespace.
    brief["options"][0]["cost_components"][0]["source_ref"] = upload_id + ":0"
    brief["selected_attachment_ids"] = [upload_id]
    from round_robin.schemas.briefs import DraftBrief
    run_id = seed(factory, brief=DraftBrief.model_validate(brief))
    text = "Supplier attachment: installation needs a permit."
    digest = hashlib.sha256(text.encode()).hexdigest()
    with factory.begin() as session:
        run = session.get(Run, run_id)
        session.add(Upload(id=upload_id, meeting_id=run.meeting_id, owner_id=run.owner_id, filename="supplier.txt", status="ready", canonical_text=text, sha256=digest, byte_size=len(text), media_type="text/plain", recipients=["openai", "anthropic", "google"]))
        run.configuration = {**run.configuration, "input_manifest": [{"id": upload_id, "sha256": digest}]}
        prepare_evidence(session, run)
    with factory() as session:
        records = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id)))
        assert len(records) == len({component["source_ref"] for option in brief["options"] for component in option["cost_components"]}) + 1
        assert sum(record.content["text"] == text for record in records) == 1
        run = session.get(Run, run_id)
        assert run.configuration["source_aliases"][upload_id + ":0"] != next(record.id for record in records if record.content["text"] == text)


def test_dispatch_limit_cannot_be_starved_by_unowned_outbox_families(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        for index in range(3):
            session.add(Outbox(id=f"unowned_{index}", run_id=run_id, kind="image_submit", status="pending", due_at=utcnow() - timedelta(hours=1)))
    sent = []
    assert dispatch_once(session_factory=factory, sender=lambda *args: sent.append(args), limit=2) == 1
    assert sent == [("advance", run_id)]
    with factory() as session:
        assert all(item.status == "pending" for item in session.scalars(select(Outbox).where(Outbox.kind == "image_submit")))


def test_stale_iteration_delivery_cannot_authorize_or_finalize_current_work(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
        run.configuration = {**run.configuration, "stage_iteration": 1}
    provider = SimulatedProvider("openai")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
    assert result["status"] == "stale_iteration"
    with factory() as session:
        assert session.get(Run, run_id).status == "running"
        assert session.get(Step, step_id).calls == 0
        assert session.scalar(select(Attempt)) is None
    assert provider.calls == 0


def test_dns_thread_start_failure_releases_bounded_capacity(monkeypatch):
    from round_robin.providers import transport
    capacity = threading.BoundedSemaphore(1)
    monkeypatch.setattr(transport, "_DNS_CAPACITY", capacity)
    def unavailable(_):
        raise RuntimeError("private thread exhaustion")
    monkeypatch.setattr(threading.Thread, "start", unavailable)
    import time
    with pytest.raises(httpcore.ConnectError):
        transport._addresses("synthetic", 443, time.monotonic() + 1)
    assert capacity.acquire(blocking=False)
    capacity.release()


def test_fractional_token_prices_preserve_exact_ledger_cost_and_report_precision(factory, monkeypatch):
    from backend.tests.test_orchestration import drain
    from round_robin.api.preflight import run_configuration
    from round_robin.configuration import Settings
    from round_robin.database.models import ReportVersion
    from round_robin.database.services import lock_policies
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    for provider in ("openai", "anthropic", "google"):
        synthetic = configuration(provider)
        for field in ("models", "pricing", "capabilities"):
            config.setdefault(field, {}).update(synthetic[field])
        config["pricing"][provider].update(input_usd_per_million="1.25", output_usd_per_million="5.5")
    config["anthropic_api_version"] = "2026-10-21"
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
    registry = {}
    for provider in ("openai", "anthropic", "google"):
        def send(request, provider=provider):
            content = output()
            content.update(reference_ids=[], audit_passed=True)
            raw = response_for(provider, content)
            raw["usageMetadata" if provider == "google" else "usage"] = {"promptTokenCount": 1, "totalTokenCount": 2} if provider == "google" else {"input_tokens": 1, "output_tokens": 1}
            return httpx.Response(200, json=raw)
        registry[provider] = HTTPProvider(provider, config, transport=httpx.MockTransport(send), api_key="synthetic")
    drain(factory, registry)
    with factory() as session:
        assert session.get(Run, run_id).status == "completed"
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content
        ledger_total = sum((Decimal(operation.settled_usd or "0") for operation in session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run_id))), Decimal("0"))
        assert Decimal(report["usage"]["actual_cost"]) == ledger_total > 0


def test_audit_hard_requirement_failure_cannot_be_lost_when_synthesis_omits_it():
    from round_robin.fixtures import sample_brief
    from round_robin.orchestration.reporting import build_report
    brief = sample_brief()
    run = Run(id="run_audit_veto", input_snapshot=brief.model_dump(mode="json"), configuration={"mode": "live", "evidence_prepared": True}, event_sequence=0)
    reference = brief.options[0].cost_components[0].source_ref
    common = output()
    common.update(reference_ids=[], recommendation_option_id=brief.options[0].id)
    finding = {"option_id": brief.options[0].id, "requirement_id": "requirement_goal", "result": "fail", "reason": "The saved supplier passage explicitly rules out the hard requirement.", "basis_refs": [reference]}
    steps = [Step(id="step_synth_veto", provider="openai", stage="synthesis", status="succeeded", result_json={"output": common}), Step(id="step_audit_veto", provider="google", stage="audit", status="succeeded", result_json={"output": {**common, "audit_passed": True, "constraints": [finding]}})]
    report = build_report(run, steps, status="completed_with_limitations", stop_reason="test_audit_veto", report_id="report_audit_veto", version=1, usage={"simulated": False, "status": "reported", "actual_cost": "0", "conservative_exposure_usd": "0", "unknown_operation_ids": [], "request_count": 0, "currency": "USD"}, evidence=[{"id": component.source_ref} for option in brief.options for component in option.cost_components])
    constraint = next(item for item in report["comparison"][0]["constraint_results"] if item["requirement_id"] == "requirement_goal")
    assert constraint["result"] == "fail" and constraint["basis_refs"] == [reference]
    assert report["recommendation"] is None


def test_completed_attempt_with_late_not_sent_proof_can_clear_unknown_exposure(factory):
    # A reconciler can expire the bounded executor while a response is returning.
    # Definitive pre-send failure remains useful accounting evidence after that fence.
    from round_robin.orchestration.budget import reconcile_operation
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id))
    class LateNotSent(SimulatedProvider):
        def generate(self, request):
            with factory.begin() as session:
                attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
                attempt.completed_at, attempt.status, attempt.usage_certainty = utcnow(), "unknown", "unknown"
                reconcile_operation(session, attempt.id, None, "unknown")
            raise ProviderFailure("connection_failure", may_have_sent=False)
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": LateNotSent("openai")})
    assert result["status"] == "late_usage_reconciled"
    with factory() as session:
        attempt = session.scalar(select(Attempt))
        assert attempt.usage_certainty == "proven_not_sent" and attempt.settled_usd == "0"
        operation = session.get(BudgetOperation, attempt.id)
        assert operation.status == "settled" and operation.settled_usd == "0"
        assert session.get(Step, step_id).result_json is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_usage_metadata_cannot_break_postgres_persistence(value):
    raw = response_for("openai")
    raw["usage"]["untrusted_metadata"] = {"nested": [value]}
    adapter = HTTPProvider("openai", configuration("openai"), transport=httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(raw).encode())), api_key="synthetic")
    result = adapter.generate(packet())
    assert result.output.recommendation_option_id == "option_a"
    assert result.usage == {} and result.cost_usd is None


@pytest.mark.parametrize("state", ["old_generation", "awaiting_admission"])
def test_delayed_execute_message_cannot_finalize_a_retry_waiting_for_admission(factory, state):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step_id = session.scalar(select(Step.id))
        if state == "old_generation":
            run.recovery_generation += 1
        else:
            run.status = "queued"
            run.active_started_at = None
            run.resumption_expires_at = utcnow() + timedelta(minutes=30)
    provider = SimulatedProvider("openai")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
    assert result["status"] == ("stale_generation" if state == "old_generation" else "admission_wait")
    with factory() as session:
        assert session.get(Run, run_id).status == ("running" if state == "old_generation" else "queued")
        assert session.get(Step, step_id).status == "pending"
        assert session.scalar(select(Attempt)) is None
    assert provider.calls == 0


def test_iteration_changed_during_provider_io_discards_content_but_settles_usage(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id))
    class SupersededProvider(SimulatedProvider):
        def generate(self, request):
            with factory.begin() as session:
                run = session.get(Run, run_id)
                run.configuration = {**run.configuration, "stage_iteration": 1}
            return super().generate(request)
    provider = SupersededProvider("openai")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
    assert result["status"] == "late_result_fenced"
    with factory() as session:
        assert session.get(Step, step_id).result_json is None
        attempt = session.scalar(select(Attempt))
        assert attempt.response_json["content_discarded"] is True
        assert attempt.usage_certainty == "reported" and attempt.settled_usd == "0"
        assert session.get(BudgetOperation, attempt.id).status == "settled"
    assert provider.calls == 1
