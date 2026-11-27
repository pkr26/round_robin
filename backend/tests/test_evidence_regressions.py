"""Regression coverage for stale prices, claim support and provider contracts.

All provider traffic uses MockTransport. Funded records are synthetic control
fixtures; they authorize no external model requests or real study evidence.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select

from backend.tests.test_orchestration import drain, factory, seed
from backend.tests.test_provider_configuration import synthetic_configuration
from backend.tests.test_providers import configuration, output, packet, response_for
from backend.tests.test_study_runner import funded_study, mock_registry
from experiments.journal import Journal
from experiments.study_runner import FundedStudyRunner
from round_robin.api.auth import require_owner
from round_robin.api.preflight import run_configuration
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import ReportVersion, Run, Step, utcnow
from round_robin.database.services import canonical_hash, lock_policies
from round_robin.fixtures import sample_brief
from round_robin.orchestration.reporting import comparison_as_of, comparison_data
from round_robin.providers.base import ProviderFailure, ProviderOutput, validate_output
from round_robin.providers.http import HTTPProvider, _output_schema, _wire_output_schema
from round_robin.providers.simulated import SimulatedProvider
from round_robin.research.calculator import CalculationError, break_even, lifecycle_total
from round_robin.research.claims import ClaimRejected, assign_claim_ids
from round_robin.research.reuse import claim_packet, persist_claims
from round_robin.schemas.provider_configuration import ProviderConfiguration


def complete_brief(*, valid_until=None):
    body = sample_brief().model_dump(mode="json")
    for option in body["options"]:
        for category in ("upfront", "installation", "recurring", "maintenance", "tax_delivery"):
            if not any(component["kind"] == category for component in option["cost_components"]):
                option["cost_components"].append({"id": f"extra_{category}", "label": category, "kind": category, "amount": "0", "currency": "USD", "frequency": "once", "status": "not_applicable", "source_ref": "quote_extra"})
        for component in option["cost_components"]:
            component["quote_valid_until"] = valid_until.isoformat() if valid_until else None
            component["source_ref"] = f"quote_{option['id']}_{component['id']}"
    return type(sample_brief()).model_validate(body)


def claim(alias="new_claim_1", refs=(), *, kind="fact"):
    return {"alias": alias, "proposition": "A supplied proposition.", "kind": kind, "evidence_status": "supported", "reference_ids": list(refs), "predecessor_id": None}


def test_quote_expiry_changes_feasibility_but_preserves_known_arithmetic():
    expiry = date(2026, 11, 16)
    brief = complete_brief(valid_until=expiry)
    current, _ = comparison_data(brief, as_of=expiry)
    expired, calculations = comparison_data(brief, as_of=expiry + timedelta(days=1))
    assert current[0]["total"] == "1600.00"
    assert current[0]["constraint_results"][0]["result"] == "pass"
    assert expired[0]["total"] is None
    assert expired[0]["known_subtotal"] == "1600.00"
    assert expired[0]["constraint_results"][0]["result"] == "unknown"
    assert any("quote expired 2026-11-16" in reason for reason in expired[0]["missing_components"])
    assert calculations[0]["output"] is None
    first, second = brief.options[:2]
    assert break_even(first, second, "USD", as_of=expiry).output == "20"
    with pytest.raises(CalculationError, match="current quotes"):
        break_even(first, second, "USD", as_of=expiry + timedelta(days=1))


def test_unknown_expired_components_stay_within_the_per_component_gap_bound():
    option = sample_brief().options[0]
    component = option.cost_components[0].model_copy(update={"amount": None, "unknown_reason": "A replacement price is missing", "quote_valid_until": date(2000, 1, 1)})
    option = option.model_copy(update={"cost_components": [component.model_copy(update={"id": f"cost_{index}"}) for index in range(30)]})
    total = lifecycle_total(option, 12, "USD", calculation_id="calc_expired", as_of=date(2026, 11, 16))
    assert total.output is None and len(total.missing_components) == 30
    assert all("expired" in reason and "replacement price" in reason for reason in total.missing_components)


def test_simulator_uses_the_accepted_date_after_the_quote_expires_on_the_clock():
    request = packet()
    accepted_date = date(2000, 1, 1)
    brief = complete_brief(valid_until=accepted_date)
    brief.hard_requirements = [item for item in brief.hard_requirements if item.metric == "total_cost"]
    brief.deadline = None
    request = replace(request, brief=brief.model_dump(mode="json"), cost_as_of=accepted_date.isoformat())
    result = SimulatedProvider("openai").generate(request)
    assert result.output.recommendation_option_id is not None
    assert request.packet()["cost_as_of"] == accepted_date.isoformat()


def test_saved_run_keeps_one_comparison_date_when_the_clock_crosses_midnight(factory, monkeypatch):
    from round_robin.research import calculator

    accepted = utcnow()
    brief = complete_brief(valid_until=accepted.date())
    brief.hard_requirements = [item for item in brief.hard_requirements if item.metric == "total_cost"]
    brief.deadline = None
    run_id = seed(factory, brief=brief)
    with factory.begin() as session:
        session.get(Run, run_id).created_at = accepted

    class FollowingDay:
        @staticmethod
        def now(_timezone):
            return accepted + timedelta(days=1)

    monkeypatch.setattr(calculator, "datetime", FollowingDay)
    drain(factory)
    with factory() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content
        assert report["comparison"][0]["total"] == "1600.00"
        assert report["comparison"][0]["constraint_results"][0]["result"] == "pass"
        assert report["recommendation"] is not None


def test_comparison_date_uses_the_frozen_briefs_local_calendar():
    run = SimpleNamespace(created_at=datetime(2026, 11, 17, 1, tzinfo=timezone.utc), input_snapshot={"timezone": "America/Phoenix"}, configuration={})
    assert comparison_as_of(run) == date(2026, 11, 16)
    run.configuration = {"resolved_brief": {"timezone": "Pacific/Kiritimati"}}
    assert comparison_as_of(run) == date(2026, 11, 17)


@pytest.mark.parametrize("claims", [
    [claim(refs=["new_claim_1"])],
    [claim(refs=["new_claim_2"]), claim("new_claim_2", ["new_claim_1"])],
    [claim(refs=["evidence_owned", "new_claim_1"])],
])
def test_circular_claim_support_fails_both_provider_and_identity_boundaries(claims):
    content = {**output(), "claims": claims}
    with pytest.raises(ProviderFailure, match="invalid_reference"):
        validate_output(ProviderOutput.model_validate(content), packet())
    with pytest.raises(ClaimRejected, match="circular"):
        assign_claim_ids(content, allowed_reference_ids={"evidence_owned"})


def test_claim_support_requires_a_source_path_and_keeps_valid_alias_chains():
    request = replace(packet(), evidence=[{"id": "evidence_owned", "text": "A source passage.", "verification_status": "user_supplied"}])
    content = {**output(), "claims": [claim(refs=["evidence_owned"]), claim("new_claim_2", ["new_claim_1"]), claim("new_claim_3"), claim("new_claim_4", kind="preference")]}
    checked = validate_output(ProviderOutput.model_validate(content), request)
    assert [item.evidence_status for item in checked.claims] == ["supported", "supported", "not_checked", "supported"]
    prior = {"id": "claim_prior", "reference_ids": ["evidence_owned"]}
    request = replace(request, claims=(prior,), allowed_reference_ids=frozenset({"evidence_owned", "claim_prior"}))
    assert validate_output(ProviderOutput.model_validate({**output(), "claims": [claim(refs=["claim_prior"])]}), request).claims[0].evidence_status == "supported"
    request = replace(request, claims=({"id": "claim_prior", "reference_ids": ["claim_prior"]},))
    with pytest.raises(ProviderFailure, match="invalid_reference"):
        validate_output(ProviderOutput.model_validate({**output(), "claims": [claim(refs=["claim_prior"])]}), request)


def test_an_unavailable_calculation_is_not_a_supported_factual_basis():
    request = replace(packet(), calculations=({"id": "calc_unknown", "output": None},), allowed_reference_ids=frozenset({"evidence_owned", "calc_unknown"}))
    content = {**output(), "claims": [claim(refs=["calc_unknown"], kind="calculation")]}
    assert validate_output(ProviderOutput.model_validate(content), request).claims[0].evidence_status == "not_checked"
    request = replace(request, calculations=({"id": "calc_unknown", "output": "0"},))
    assert validate_output(ProviderOutput.model_validate(content), request).claims[0].evidence_status == "supported"


def test_persistence_cannot_bypass_claim_support_validation(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "mode": "live"}
        step = Step(id="step_support", run_id=run_id, stage="proposal", role_slot_id="proposal", provider="openai", status="succeeded")
        session.add(step)
        session.flush()
        with pytest.raises(ClaimRejected, match="circular"):
            persist_claims(session, run, step, {"claims": [claim(refs=["new_claim_1"])]}, set())
        assert session.scalar(select(func.count()).select_from(ClaimVersion)) == 0
        saved = persist_claims(session, run, step, {"claims": [claim()]}, set())
        assert saved["claims"][0]["evidence_status"] == "not_checked"
        assert claim_packet(session, run)[0]["evidence_status"] == "not_checked"


@pytest.mark.parametrize("support", ["self", "mutual", "empty"])
def test_historical_invalid_claim_support_fails_closed_without_rewriting_records(factory, tmp_path, support):
    run_id = seed(factory)
    originals = {}
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "mode": "live"}
        identities = ["claim_historical_a", "claim_historical_b"] if support == "mutual" else ["claim_historical_a"]
        for identity in identities:
            references = [identity] if support == "self" else [next(other for other in identities if other != identity)] if support == "mutual" else []
            content = {"id": identity, "version": 1, "predecessor_id": None, "proposition": "An old unsupported assertion.", "kind": "fact", "evidence_status": "supported", "reference_ids": references, "time_sensitive": True, "verified_at": utcnow().isoformat()}
            from round_robin.research.reuse import StoredClaim
            content = StoredClaim.model_validate(content).model_dump(mode="json")
            originals[identity] = content
            session.add(ClaimVersion(id=identity, run_id=run_id, meeting_id=run.meeting_id, owner_id=run.owner_id, version=1, content=content, sha256=canonical_hash(content)))
    app = create_app(str(factory.kw["bind"].url), settings=replace(Settings(), artifact_directory=tmp_path / "private"))
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/claims")
        assert response.status_code == 409 and response.json()["error"]["code"] == "claim_unreadable"
    with factory() as session:
        for identity, content in originals.items():
            row = session.get(ClaimVersion, identity)
            assert row.content == content and row.sha256 == canonical_hash(content)


@pytest.mark.parametrize("basis", ["known", "unknown", "invented"])
def test_persistence_checks_the_calculation_result_behind_a_support_reference(factory, basis):
    brief = complete_brief()
    if basis == "unknown":
        brief.options[0].cost_components[0].amount = None
        brief.options[0].cost_components[0].unknown_reason = "The price is missing"
    run_id = seed(factory, brief=brief)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "mode": "live"}
        step = Step(id="step_calculation_support", run_id=run_id, stage="proposal", role_slot_id="proposal", provider="openai", status="succeeded")
        session.add(step)
        session.flush()
        _, calculations = comparison_data(brief, as_of=run.created_at.date())
        identity = "calc_invented" if basis == "invented" else calculations[0]["id"]
        saved = persist_claims(session, run, step, {"claims": [claim(refs=[identity], kind="calculation")]}, {identity})
        assert saved["claims"][0]["evidence_status"] == ("supported" if basis == "known" else "not_checked")


def test_live_workers_publish_expired_costs_as_unknown_and_unrooted_claims_as_unchecked(factory, tmp_path, monkeypatch):
    config = synthetic_configuration()
    for capability in config["capabilities"].values():
        capability["context_window_tokens"] = 128000
    provider_path = tmp_path / "providers.json"
    provider_path.write_text(json.dumps(config))
    settings = replace(Settings(), provider_config_file=provider_path, paid_access_enabled=True, artifact_directory=tmp_path / "private")
    monkeypatch.setattr("round_robin.orchestration.configuration.settings", settings)
    from zoneinfo import ZoneInfo
    comparison_date = utcnow().astimezone(ZoneInfo(complete_brief().timezone)).date()
    brief = complete_brief(valid_until=comparison_date - timedelta(days=1))
    run_id = seed(factory, brief=brief, config=run_configuration(settings, "live"))
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
    registry, requests = mock_registry(ProviderConfiguration.model_validate(config))
    for provider, adapter in registry.items():
        original_transport = adapter.transport

        def add_unrooted_claim(request, provider=provider, original_transport=original_transport):
            raw = original_transport.handle_request(request).json()
            block = raw["output"][0]["content"][0] if provider == "openai" else raw["content"][0] if provider == "anthropic" else raw["candidates"][0]["content"]["parts"][0]
            content = json.loads(block["text"])
            content["claims"] = [claim()]
            request_body = json.loads(request.content)
            request_text = request_body["input"][0]["content"] if provider == "openai" else request_body["messages"][0]["content"] if provider == "anthropic" else request_body["contents"][0]["parts"][0]["text"]
            request_packet = json.loads(request_text)
            if request_packet["stage"] == "intake":
                content["claims"] = []  # unconfirmed intake may not issue claims
            if request_packet["evidence"]:
                # An adviser must not override missing current arithmetic by
                # calling the old structured quote a passing budget basis.
                content["constraints"] = [{"option_id": "option_a", "requirement_id": "requirement_budget", "result": "pass", "reason": "The adviser incorrectly treats an expired price as current.", "basis_refs": [request_packet["evidence"][0]["id"]]}]
            block["text"] = json.dumps(content)
            return httpx.Response(200, json=raw)

        adapter.transport = httpx.MockTransport(add_unrooted_claim)
    drain(factory, registry)
    app = create_app(str(factory.kw["bind"].url), settings=settings)
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with TestClient(app) as client:
        report_response = client.get(f"/api/runs/{run_id}/report")
        claim_response = client.get(f"/api/runs/{run_id}/claims")
        assert report_response.status_code == claim_response.status_code == 200
        report = report_response.json()
        assert report["simulated"] is False and report["readiness"] == "insufficient_evidence"
        assert report["recommendation"] is None
        assert report["comparison"][0]["total"] is None
        assert report["comparison"][0]["known_subtotal"] == "1600.00"
        assert report["comparison"][0]["constraint_results"][0]["result"] == "unknown"
        assert "quote expired" in " ".join(report["comparison"][0]["missing_components"])
        assert len(claim_response.json()["items"]) == 5  # confirmed stages only; intake cannot issue claims
        assert all(item["evidence_status"] == "not_checked" for item in claim_response.json()["items"])
    assert len(requests) == 6
    assert all(request["packet"]["cost_as_of"] for request in requests)


def test_funded_study_finalizes_without_a_distinct_auditor_and_continues(funded_study):
    _, (approval, manifest, configuration_model) = funded_study
    body = configuration_model.model_dump(mode="json")
    body["fallbacks"]["synthesis"] = ["openai", "google"]
    body["fallbacks"]["audit"] = ["google"]
    configuration_model = ProviderConfiguration.model_validate(body)
    path = approval.provider_configuration.path
    path.write_text(json.dumps(body))
    approval = approval.model_copy(update={"provider_configuration": approval.provider_configuration.model_copy(update={"sha256": hashlib.sha256(path.read_bytes()).hexdigest()})})
    registry, requests = mock_registry(configuration_model)
    original = registry["openai"]

    class UnavailableSynthesis:
        def conservative_bound(self, request):
            return original.conservative_bound(request)

        def generate(self, request):
            if request.stage == "synthesis":
                raise ProviderFailure("authentication")
            return original.generate(request)

    registry["openai"] = UnavailableSynthesis()
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration_model, provider_registry=registry, sleeper=lambda _: None)
        trials = [trial for trial in runner.plan if trial.workflow == "independent_review"][:2]
        first = runner.execute_trial(trials[0])
        assert first["status"] == "limited" and not first["mandatory_work_complete"]
        failed_audit = next(record for record in first["stage_records"] if record["stage"] == "audit")
        assert failed_audit["attempt_ids"] == []
        assert failed_audit["failures"] == [{"error_code": "independent_auditor_unavailable"}]
        assert runner.ledger._state()["runs"][runner._run_id(trials[0])]["terminal"]
        count = len(requests)
        assert runner.execute_trial(trials[0]) == first and len(requests) == count
        runner.plan = trials
        summary = runner.run()
        assert summary["blocked"] is None and summary["retained_outputs"] == 2
        assert not any(request["packet"]["stage"] == "audit" for request in requests)


@pytest.mark.parametrize("invalid_output", [False, True])
def test_claude_wire_schema_is_accepted_by_documented_subset_and_local_constraints_remain(invalid_output):
    received = []

    def reject_unsupported(value):
        if isinstance(value, dict):
            # Claude's raw API documents these constraints as unsupported.
            assert not {"minLength", "maxLength", "minimum", "maximum", "multipleOf", "maxItems"} & value.keys()
            assert "minItems" not in value or value["minItems"] in (0, 1)
            for child in value.values():
                reject_unsupported(child)
        elif isinstance(value, list):
            for child in value:
                reject_unsupported(child)

    def send(request):
        received.append(request)
        body = json.loads(request.content)
        reject_unsupported(body["output_config"]["format"]["schema"])
        content = output()
        if invalid_output:
            content["summary"] = "x" * 4001
        return httpx.Response(200, json=response_for("anthropic", content))

    adapter = HTTPProvider("anthropic", configuration("anthropic"), transport=httpx.MockTransport(send), api_key="synthetic")
    if invalid_output:
        with pytest.raises(ProviderFailure, match="schema_invalid"):
            adapter.generate(packet())
    else:
        assert adapter.generate(packet()).output.summary == output()["summary"]
    assert len(received) == 1
    assert "maxLength=4000" in _wire_output_schema("anthropic")["properties"]["summary"]["description"]
    assert _output_schema()["properties"]["summary"]["maxLength"] == 4000
    assert _wire_output_schema("openai") == _output_schema()
