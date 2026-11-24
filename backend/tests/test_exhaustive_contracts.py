"""Independent hostile-input corpus for authoritative serialized contracts."""
from copy import deepcopy
from datetime import timedelta
import json

from hypothesis import given, settings, strategies as st
from pydantic import ValidationError
import pytest

from backend.tests.test_provider_configuration import synthetic_configuration
from round_robin.fixtures import SampleBundle, fingerprint, sample_brief, sample_bundle, sample_preflight, sample_report
from round_robin.schemas.briefs import CostComponent, DraftBrief, PublicTerm
from round_robin.schemas.common import Contract, total_money
from round_robin.schemas.contributions import Contribution
from round_robin.schemas.preflight import Preflight
from round_robin.schemas.provider_configuration import ProviderConfiguration, ResearchConfiguration
from round_robin.schemas.reports import DecisionReport, OptionComparison


@pytest.mark.parametrize("change,match", [
    (lambda b: b["options"][0]["cost_components"][0].update(unknown_reason="unclear"), "cannot also be unknown"),
    (lambda b: b["hard_requirements"][0].update(threshold=None), "needs a threshold"),
    (lambda b: b.update(timezone="NoSuch/Zone"), "IANA timezone"),
    (lambda b: b["options"].extend([{**deepcopy(b["options"][0]), "id": "option_four"}, {**deepcopy(b["options"][0]), "id": "option_five"}]) or b["options"][2].update(role="candidate"), "At most four"),
    (lambda b: b["options"][0].update(role="status_quo"), "At most one"),
    (lambda b: b["preferences"].append({**b["preferences"][0], "id": "preference_other"}), "ranks must be unique"),
    (lambda b: b["options"][0]["cost_components"].append(deepcopy(b["options"][0]["cost_components"][0])), "component IDs must be unique"),
    (lambda b: b.update(selected_attachment_ids=["upload_one", "upload_one"]), "attachment IDs must be unique"),
    (lambda b: b["hard_requirements"].append(deepcopy(b["hard_requirements"][0])), "IDs within a collection"),
])
def test_brief_ambiguous_or_inconsistent_inputs_fail(change, match):
    brief = sample_brief().model_dump(mode="json")
    change(brief)
    with pytest.raises(ValidationError, match=match):
        DraftBrief.model_validate(brief)


@pytest.mark.parametrize("text", ["", "  ", "approved\nprivate", "\t", "allowed\x00hidden"])
def test_public_scope_cannot_contain_controls_or_blanks(text):
    with pytest.raises(ValidationError):
        PublicTerm(id="public_term", text=text)


def test_valid_public_scope_and_explicit_unknown_are_preserved():
    assert PublicTerm(id="public_term", text="published café price").text == "published café price"
    component = sample_brief().options[0].cost_components[0].model_dump(mode="json")
    component.update(amount=None, unknown_reason="Supplier has not quoted this item.")
    assert CostComponent.model_validate(component).amount is None


@pytest.mark.parametrize("value", [1, True, "-1", "1e10", "1." , "0." + "0" * 64, "1000000000000000000000000000001"])
def test_calculated_money_rejects_numeric_coercion_and_overflow(value):
    with pytest.raises(ValueError):
        total_money(value)


def test_nested_contract_dictionaries_reject_invalid_unicode_without_echoing():
    class Nested(Contract):
        payload: dict
    for bad in ("\ud800", "\udfff", "private\x00payload"):
        with pytest.raises(ValidationError) as caught:
            Nested(payload={"nested": [{bad: "secret-sentinel"}]})
        assert "secret-sentinel" not in str(caught.value)


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=100))
@settings(max_examples=80, deadline=None, derandomize=True)
def test_valid_unicode_survives_contract_json_roundtrip(value):
    class Nested(Contract):
        payload: dict
    record = Nested(payload={"text": [value]})
    assert Nested.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize("change,match", [
    (lambda c: c["claims"].append(deepcopy(c["claims"][0])), "aliases must be unique"),
    (lambda c: c.update(reference_ids=[f"source_{i}" for i in range(40)]), "forty existing references"),
])
def test_contribution_identity_budget_is_enforced(change, match):
    payload = sample_bundle().contributions[0].model_dump(mode="json")
    change(payload)
    with pytest.raises(ValidationError, match=match):
        Contribution.model_validate(payload)


def test_preflight_window_and_mandatory_calls_cannot_be_laundered():
    original = sample_preflight(sample_brief()).model_dump()
    invalid = {**original, "expires_at": original["expires_at"] + timedelta(seconds=1)}
    with pytest.raises(ValidationError, match="exactly ten minutes"):
        Preflight.model_validate(invalid)
    invalid = deepcopy(original)
    invalid["limits"]["max_requests"] = 1
    with pytest.raises(ValidationError, match="Finish calls exceed"):
        Preflight.model_validate(invalid)


@pytest.mark.parametrize("change,match", [
    (lambda c: c["models"].update(google="models/escape"), "identifier is invalid"),
    (lambda c: c["models"].update(openai="invalid?secret"), "identifier is invalid"),
    (lambda c: c["fallbacks"].update(audit=[]), "bounded moderator fallback"),
    (lambda c: c["fallbacks"].update(audit=["google", "google"]), "bounded moderator fallback"),
    (lambda c: c["limits"].update(per_run_usd="0.1"), "finishing resources"),
    (lambda c: c["limits"].update(max_requests=1), "finishing resources"),
    (lambda c: c["limits"].update(active_seconds=1), "finishing resources"),
    (lambda c: c["aggregate_funding"].update(owner_daily_usd="0"), "funding limits must be positive"),
])
def test_operator_config_cannot_expand_or_unfund_authority(change, match):
    config = synthetic_configuration()
    change(config)
    with pytest.raises(ValidationError, match=match):
        ProviderConfiguration.model_validate(config)


@pytest.mark.parametrize("fields", [{}, {"pricing_version": "synthetic"}, {"pricing_version": "synthetic", "per_request_usd": "0"}])
def test_research_requires_positive_explicit_pricing(fields):
    with pytest.raises(ValidationError, match="conservative request pricing"):
        ResearchConfiguration(enabled=True, **fields)


@pytest.mark.parametrize("change,match", [
    (lambda r: r.update(readiness=None), "Unavailable assessment"),
    (lambda r: r.update(readiness=None, recommendation=None), "needs an explicit reason"),
    (lambda r: r.update(readiness_unavailable_reason="Cannot assess"), "cannot also be unavailable"),
    (lambda r: r["recommendation"].update(option_id="option_missing"), "refer to a compared option"),
    (lambda r: r["comparison"][0]["constraint_results"].pop(), "Missing hard-requirement"),
    (lambda r: r["comparison"][0]["constraint_results"][0].update(result="fail"), "pass all applicable"),
    (lambda r: r["comparison"][0]["constraint_results"][0].update(basis_refs=[]), "need supporting references"),
    (lambda r: r["essential_dependencies"][0].update(reference_ids=[]), "accepted scoped support"),
    (lambda r: r["assessment"].update(proposal_providers=["openai"]), "two distinct"),
    (lambda r: r["assessment"].update(auditor="openai"), "successful distinct audit"),
])
def test_ready_report_is_not_a_self_attested_label(change, match):
    report = sample_bundle().scenarios[0].report.model_dump(mode="json")
    change(report)
    with pytest.raises(ValidationError, match=match):
        DecisionReport.model_validate(report)


def test_missing_components_never_publish_an_all_in_total():
    row = sample_bundle().scenarios[0].report.comparison[0].model_dump(mode="json")
    row["missing_components"] = ["installation"]
    with pytest.raises(ValidationError, match="incomplete cost"):
        OptionComparison.model_validate(row)


@pytest.mark.parametrize("change,match", [
    (lambda b: b["evidence"].append(deepcopy(b["evidence"][0])), "Evidence IDs must be unique"),
    (lambda b: b["scenarios"][1].update(id="base12"), "three distinct"),
    (lambda b: b["scenarios"][0]["preflight"].update(draft_hash="f" * 64), "bind the exact scenario"),
    (lambda b: b["scenarios"][0]["report"]["essential_dependencies"][0].update(reference_ids=["passage_foreign"]), "Dependency references"),
    (lambda b: b["scenarios"][0]["report"]["comparison"][1]["constraint_results"].pop(), "Every compared option"),
    (lambda b: b["scenarios"][0]["report"]["comparison"][1]["constraint_results"][0].update(basis_refs=["passage_foreign"]), "Constraint basis"),
    (lambda b: b["scenarios"][0].update(report=None), "Base fixture report is required"),
])
def test_published_fixture_contract_rejects_corrupted_history(change, match):
    bundle = sample_bundle().model_dump(mode="json")
    change(bundle)
    with pytest.raises(ValidationError, match=match):
        SampleBundle.model_validate(bundle)


def test_fixture_cost_provenance_is_checked_after_rebinding_brief_hash():
    bundle = sample_bundle().model_dump(mode="json")
    scenario = bundle["scenarios"][0]
    scenario["brief"]["options"][0]["cost_components"][0]["source_ref"] = "source_foreign"
    scenario["preflight"]["draft_hash"] = fingerprint(scenario["brief"])
    with pytest.raises(ValidationError, match="Cost provenance"):
        SampleBundle.model_validate(bundle)
