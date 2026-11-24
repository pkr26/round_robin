import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from round_robin.export_contracts import export
from round_robin.fixture_api import app
from round_robin.fixtures import SampleBundle, fingerprint, sample_bundle
from round_robin.schemas import DraftBrief, Preflight


def test_sample_roundtrip_and_changed_inputs_preserve_history():
    bundle = sample_bundle()
    restored = SampleBundle.model_validate_json(bundle.model_dump_json())
    assert restored == bundle
    assert [scenario.id for scenario in bundle.scenarios] == ["base12", "horizon24", "missing_installation"]
    first, changed, missing = bundle.scenarios
    assert first.report.recommendation.option_id == "option_a"
    assert changed.report.recommendation.option_id == "option_b"
    assert changed.report.parent_report_id == first.report.id
    assert missing.report.readiness == "conditional"
    assert first.report.comparison[0].total == "1600.00"
    assert bundle.cancelled_report.execution_status == "cancelled"
    assert bundle.cancelled_report.recommendation is None
    assert bundle.cancelled_report.readiness is None
    assert bundle.cancelled_report.usage.request_count == 0


def test_partial_and_cancelled_reports_preserve_each_scenarios_inputs():
    for scenario in sample_bundle().scenarios:
        for report in [scenario.partial_report, scenario.cancelled_report]:
            assert report.run_id == scenario.report.run_id
            assert report.comparison == scenario.report.comparison
            assert all(item.horizon_months == scenario.brief.horizon_months for item in report.comparison)
            assert report.readiness is None
            assert report.recommendation is None
            assert report.usage.request_count == 0
        assert scenario.cancelled_report.execution_status == "cancelled"


def test_missing_installation_uses_changed_quote_provenance_consistently():
    scenario = next(item for item in sample_bundle().scenarios if item.id == "missing_installation")
    option_b = next(item for item in scenario.brief.options if item.id == "option_b")
    assert {item.source_ref for item in option_b.cost_components} == {"passage_installation_gap"}
    for report in [scenario.report, scenario.partial_report, scenario.cancelled_report]:
        input_refs = {item.source_ref for calc in report.calculations for item in calc.inputs}
        dependency_refs = {ref for item in report.essential_dependencies for ref in item.reference_ids}
        assert "passage_offer_b" not in input_refs | dependency_refs
        assert "passage_installation_gap" in input_refs & dependency_refs


def test_all_passages_have_exact_hashes_and_fictional_origin():
    for passage in sample_bundle().evidence:
        assert passage.sha256 == hashlib.sha256(passage.text.encode()).hexdigest()
        assert passage.origin == "fictional"
        assert passage.verification_status == "fictional"


def test_preflight_binds_the_exact_brief_scope_and_expiry():
    for scenario in sample_bundle().scenarios:
        preflight = scenario.preflight
        assert preflight.draft_hash == fingerprint(scenario.brief.model_dump(mode="json"))
        assert (preflight.expires_at - preflight.created_at).total_seconds() == 600
        assert preflight.initial_admission_window_minutes == 30
        assert set(preflight.recipients) == {"openai", "anthropic", "google"}
        assert preflight.paid_access_enabled is False
        assert preflight.consumed_by_run_id is None


def test_unknown_fields_and_unapproved_recipients_fail_closed():
    data = sample_bundle().brief.model_dump(mode="json")
    data["provider_key"] = "not-a-real-key"
    with pytest.raises(ValidationError, match="Extra inputs"):
        DraftBrief.model_validate(data)
    preflight = sample_bundle().scenarios[0].preflight.model_dump(mode="json")
    preflight["recipients"] = ["openai", "openai", "google"]
    with pytest.raises(ValidationError, match="same three"):
        Preflight.model_validate(preflight)


def test_incomplete_drafts_save_but_do_not_claim_resolved_options():
    data = sample_bundle().brief.model_dump(mode="json")
    data.update(options=[], budget=None, horizon_months=None, deadline=None)
    assert DraftBrief.model_validate(data).options == []


def test_foreign_references_and_changed_passage_bytes_are_rejected():
    data = sample_bundle().model_dump(mode="json")
    data["contributions"][0]["claims"][0]["reference_ids"] = ["foreign_other_meeting"]
    with pytest.raises(ValidationError, match="resolve"):
        SampleBundle.model_validate(data)
    data = sample_bundle().model_dump(mode="json")
    data["evidence"][0]["text"] += " altered"
    with pytest.raises(ValidationError, match="hash"):
        SampleBundle.model_validate(data)


def test_contract_exports_are_reproducible_and_match_committed_files(tmp_path):
    export(tmp_path)
    committed = Path(__file__).resolve().parents[2] / "contracts"
    for name in ["sample.json", "sample.schema.json", "brief.schema.json", "events.schema.json", "openapi.json"]:
        assert json.loads((tmp_path / name).read_text()) == json.loads((committed / name).read_text())
    assert "SampleBundle" in json.loads((tmp_path / "openapi.json").read_text())["components"]["schemas"]


def test_read_only_fixture_api_does_not_expose_mutations_or_provider_access():
    with TestClient(app) as client:
        response = client.get("/api/sample")
        assert response.status_code == 200
        assert response.json()["fictional"] is True
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/api/sample", json={}).status_code == 405
        assert client.post("/api/meetings", json={}).status_code == 404
        assert client.get("/health").json()["paid_access_enabled"] is False
        assert client.get("/api/sample", headers={"host": "public.example"}).status_code == 400
        assert client.get("/api/sample", headers={"origin": "https://public.example"}).status_code == 403
        assert client.get("/api/sample", headers={"x-forwarded-for": "203.0.113.1"}).status_code == 403


def test_core_input_bounds_are_enforced():
    data = sample_bundle().brief.model_dump(mode="json")
    data["question"] = "x" * 4001
    with pytest.raises(ValidationError):
        DraftBrief.model_validate(data)
    data = sample_bundle().brief.model_dump(mode="json")
    data["options"] = [data["options"][0]] * 6
    with pytest.raises(ValidationError):
        DraftBrief.model_validate(data)
