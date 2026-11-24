"""Algorithm regressions using immutable synthetic inputs and injected transports."""
from copy import deepcopy
from dataclasses import replace
import base64
import hashlib
import json

import httpx
import pytest

from backend.tests.test_image_provider import authorization, generation_packet, image_configuration, pixels
from backend.tests.test_study_runner import funded_study, mock_registry
from backend.tests.test_release_evaluation import manifest as release_manifest
from backend.tests.test_evaluation import study
from backend.tests.test_exhaustive_visual_service import saved_version
from backend.tests.test_image_worker import image_factory
from experiments.cases import substantive_fingerprint
from experiments.evaluation import build_blinded_packets, fingerprint, summarize_workflow
from experiments.journal import Journal
from experiments.paired import compare_workflows, exact_paired_pvalue, zero_failure_upper_bound
from experiments.release import validate_manifest
from experiments.runner import PolicyError
from experiments.study_runner import FundedStudyRunner
from round_robin.fixtures import sample_brief
from round_robin.providers.base import ProviderOutput
from round_robin.providers.images import HTTPImageProvider
from round_robin.visual.feedback import build_edit_bundle, digest, regression_summary, regression_targets, verify_edit_bundle
from round_robin.visual.schemas import Critique, Finding, RegressionCheck, VisualBrief
from round_robin.visual.reviews import PixelView, issue_review_receipt
from round_robin.visual.assets import validate_raster
from round_robin.database.visual_models import DesignSession, DesignVersion, DesignReview
from round_robin.visual import service as visual_service
from round_robin.visual.schemas import ImageCapabilities


def parent_finding():
    return Finding(id="finding_critical", description="Required title is missing", severity="critical", category="observed_defect").model_dump(mode="json")


def test_exact_selected_finding_and_decline_reach_actual_mocked_edit_prompt(image_configuration):
    packet = generation_packet("edit")
    packet["edit_intent"] = "Address the selected issue while preserving the declined change"
    first = parent_finding()
    second = {**first, "id": "finding_color", "description": "Change the background color", "category": "preference", "severity": "minor"}
    bundle, bundle_hash = build_edit_bundle(brief=packet["brief"], parent_version_id="version_parent",
        parent_hash=hashlib.sha256(packet["images"][0]).hexdigest(), findings=[first, second],
        dispositions={first["id"]: "address", second["id"]: "decline"}, intent=packet["edit_intent"])
    packet.update(edit_bundle=bundle, edit_bundle_hash=bundle_hash)
    sent = []
    def send(request):
        prompt = json.loads(json.loads(request.content)["prompt"])
        sent.append(prompt)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(pixels()).decode()}]})
    adapter = HTTPImageProvider("openai", image_configuration, transport=httpx.MockTransport(send), api_key="synthetic-only")
    adapter.submit(packet, authorization("edit"))
    assert sent[0]["approved_edit_bundle"] == bundle
    assert {row["finding"]["description"]: row["decision"] for row in bundle["selected_findings"]} == {first["description"]: "address", second["description"]: "decline"}
    changed = deepcopy(bundle)
    changed["selected_findings"][0]["decision"] = "address" if changed["selected_findings"][0]["decision"] == "decline" else "decline"
    with pytest.raises(ValueError, match="hash"):
        verify_edit_bundle(changed, bundle_hash, brief=packet["brief"], parent_version_id="version_parent", parent_hash=bundle["parent_hash"])
    with pytest.raises(ValueError, match="parent"):
        verify_edit_bundle(bundle, bundle_hash, brief=packet["brief"], parent_version_id="version_other", parent_hash=bundle["parent_hash"])


def test_two_empty_critics_cannot_erase_prior_blocker_or_locked_requirement():
    brief = VisualBrief(purpose="Synthetic", audience="Test", locked_requirements=["Keep brand name"]).model_dump(mode="json")
    targets = regression_targets(brief, [parent_finding()])
    critics = [Critique(provider=provider, version_id="version_child", canonical_hash="a"*64, receipt_id=f"receipt_{provider}") for provider in ("anthropic", "google")]
    result = regression_summary(targets, critics, ["anthropic", "google"])
    assert not result["complete"] and len(result["pending_target_ids"]) == 2
    assert "finding_critical" in result["unresolved_blockers"]
    for critic in critics:
        critic.regression_checks = [RegressionCheck(target_id=row["id"], result="resolved", reason="Visible in both supplied views") for row in targets]
    result = regression_summary(targets, critics, ["anthropic", "google"])
    assert result["complete"] and not result["unresolved_blockers"]
    critics[1].regression_checks[0].result = "persistent"
    result = regression_summary(targets, critics, ["anthropic", "google"])
    assert result["complete"] and "finding_critical" in result["unresolved_blockers"]
    assert result["dispositions"][0]["status"] == "persistent"


@pytest.mark.parametrize("regression_status", ["resolved", "persistent"])
def test_historical_missing_regression_checks_get_append_only_recovery_review(saved_version, regression_status):
    factory, design_id, parent_id, directory = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        parent = session.get(DesignVersion, parent_id)
        raw = visual_service.owned_asset_bytes(parent, design, directory)
        caps = ImageCapabilities(provider="anthropic", endpoint_version="synthetic", understands_images=True, supported_media=["image/png"])
        receipt = issue_review_receipt(caps, parent.id, parent.canonical_hash, [PixelView(raw, parent.canonical_hash)])
        original_parent = visual_service.commit_review(session, design, parent, Critique(provider="anthropic", version_id=parent.id,
            canonical_hash=parent.canonical_hash, receipt_id=receipt.id, findings=[Finding.model_validate(parent_finding())]), receipt, expected_fence=0)
        child = visual_service.commit_version(session, design, validate_raster(raw), directory / "visual", expected_fence=0, parent_version_id=parent.id, source_kind="native_diagram")
        originals = []
        for provider in ("anthropic", "google"):
            capability = caps.model_copy(update={"provider": provider})
            delivery = issue_review_receipt(capability, child.id, child.canonical_hash, [PixelView(raw, child.canonical_hash)])
            row = visual_service.commit_review(session, design, child, Critique(provider=provider, version_id=child.id, canonical_hash=child.canonical_hash, receipt_id=delivery.id), delivery, expected_fence=0)
            originals.append((row.id, deepcopy(row.critique)))
        session.flush()
        assert visual_service.version_view(session, child)["coverage"]["complete"]
        assert not visual_service.version_view(session, child)["regression_review"]["complete"]
        recovery = visual_service.queue_version_reviews(session, design, child)
        assert {row.provider for row in recovery} == {"anthropic", "google"}
        targets = visual_service.version_regression_targets(session, design, child)
        for provider in ("anthropic", "google"):
            capability = caps.model_copy(update={"provider": provider})
            delivery = issue_review_receipt(capability, child.id, child.canonical_hash, [PixelView(raw, child.canonical_hash), PixelView(raw, parent.canonical_hash, f"comparison_parent_version_{parent.id}")])
            visual_service.commit_review(session, design, child, Critique(provider=provider, version_id=child.id, canonical_hash=child.canonical_hash, receipt_id=delivery.id,
                regression_checks=[RegressionCheck(target_id=row["id"], result=regression_status, reason="Parent issue explicitly rechecked") for row in targets]), delivery, expected_fence=0)
        session.flush()
        view = visual_service.version_view(session, child)
        assert view["regression_review"]["complete"] and len(view["review_supplements"]) == 2
        assert bool(view["regression_review"]["unresolved_blockers"]) == (regression_status == "persistent")
        inherited = visual_service.version_feedback_findings(session, design, child)
        ancestor_id = original_parent.critique["findings"][0]["id"]
        assert (ancestor_id in {row["id"] for row in inherited}) == (regression_status == "persistent")
        grandchild = visual_service.commit_version(session, design, validate_raster(raw), directory / "visual", expected_fence=0, parent_version_id=child.id, source_kind="native_diagram")
        assert (ancestor_id in {row["id"] for row in visual_service.version_regression_targets(session, design, grandchild)}) == (regression_status == "persistent")
        for identity, critique in originals:
            assert session.get(DesignReview, identity).critique == critique


def test_substantive_identity_ignores_titles_and_option_order_but_keeps_cost_and_sequence():
    brief = sample_brief().model_dump(mode="json")
    renamed = deepcopy(brief)
    renamed["title"] = "Different display title"
    renamed["options"].reverse()
    assert substantive_fingerprint(renamed) == substantive_fingerprint(brief)
    renamed["options"][0]["cost_components"][0]["amount"] = "982"
    assert substantive_fingerprint(renamed) != substantive_fingerprint(brief)
    assert substantive_fingerprint({"steps": ["paint", "dry"]}) != substantive_fingerprint({"steps": ["dry", "paint"]})


def test_release_denominator_rejects_48_titles_of_one_valid_brief():
    manifest = release_manifest()
    for index, task in enumerate(manifest["tasks"]):
        task["brief"] = sample_brief().model_dump(mode="json")
        task["brief"]["title"] = f"Alias {index}"
    with pytest.raises(ValueError, match="duplicated"):
        validate_manifest(manifest)


def test_funded_failed_audit_receives_one_reserved_correction_and_independent_reaudit(funded_study):
    _, (approval, manifest, configuration) = funded_study
    registry, requests = mock_registry(configuration)
    for adapter in registry.values():
        original = adapter.generate
        def generate(request, original=original):
            result = original(request)
            if request.step_id == "audit":
                output = result.output.model_copy(update={"audit_passed": False, "critical_defects": ["Synthetic concrete cost error to correct"]})
                return replace(result, output=output, raw_text=output.model_dump_json())
            if request.step_id in {"synthesis_correction", "audit_correction"}:
                output = ProviderOutput.model_validate({**result.output.model_dump(mode="json"), "objection_responses": [
                    {"objection_id": item["id"], "disposition": "answered" if request.stage == "synthesis" else "resolved",
                     "response": "Synthetic correction checked against retained source", "reference_ids": [request.evidence[0]["id"]]}
                    for item in request.open_objections]})
                return replace(result, output=output, raw_text=output.model_dump_json())
            return result
        adapter.generate = generate
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry, sleeper=lambda _: None)
        trial = next(row for row in runner.plan if row.workflow == "independent_review" and row.primary)
        outcome = runner.execute_trial(trial)
        assert outcome["status"] == "completed" and outcome["request_count"] == 7
        assert [row["id"] for row in outcome["stage_records"]][-2:] == ["synthesis_correction", "audit_correction"]
        assert outcome["stage_records"][-1]["provider"] != outcome["stage_records"][-2]["provider"]
        assert runner.configuration["discussion_policy"]["sha256"]
        with pytest.raises(PolicyError):
            runner.ledger.reserve_correction(outcome["run_id"])
        assert runner.execute_trial(trial) == outcome
        assert len(requests) == 7


def test_study_claims_are_scoped_and_supplied_to_next_stage(funded_study):
    _, (approval, manifest, configuration) = funded_study
    registry, requests = mock_registry(configuration)
    for adapter in registry.values():
        original = adapter.generate
        def generate(request, original=original):
            result = original(request)
            if request.stage == "proposal":
                output = ProviderOutput.model_validate({**result.output.model_dump(mode="json"), "claims": [{"alias": "new_claim_1", "proposition": "Synthetic scoped assumption", "kind": "assumption", "evidence_status": "not_checked", "reference_ids": []}]})
                return replace(result, output=output, raw_text=output.model_dump_json())
            return result
        adapter.generate = generate
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry, sleeper=lambda _: None)
        trial = next(row for row in runner.plan if row.workflow == "independent_review" and row.primary)
        result = runner.execute_trial(trial)
    packet = next(row["packet"] for row in requests if row["packet"]["stage"] == "synthesis")
    assert len(packet["claims"]) == 3
    identities = {claim["id"] for claim in packet["claims"]}
    assert len(identities) == 3 and identities <= set(packet["allowed_reference_ids"])


def test_material_obligations_trigger_revision_and_audit_true_cannot_erase_them(funded_study):
    _, (approval, manifest, configuration) = funded_study
    registry, requests = mock_registry(configuration)
    for adapter in registry.values():
        original = adapter.generate
        def generate(request, original=original):
            result = original(request)
            if request.stage == "critique":
                output = ProviderOutput.model_validate({**result.output.model_dump(mode="json"), "dissent": [], "objections": [
                    {"alias": "new_objection_1", "concern": "Quoted maintenance cost is omitted", "severity": "material",
                     "reference_ids": [request.evidence[0]["id"]]}]})
                return replace(result, output=output, raw_text=output.model_dump_json())
            return result
        adapter.generate = generate
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry, sleeper=lambda _: None)
        trial = next(row for row in runner.plan if row.workflow == "bounded_debate" and row.primary)
        result = runner.execute_trial(trial)
        assert result["status"] == "limited" and len(result["open_material_objection_ids"]) == 3
        assert len([row for row in result["stage_records"] if row["stage"] == "revision"]) == 3
        assert result["request_count"] == 14 and not result["mandatory_work_complete"]
        assert any(event["type"] == "discussion_recorded" and len(event["issues"]) == 3 for event in journal.events)
    # A synchronous critique round cannot see same-round peer objections.
    assert all(not row["packet"]["open_objections"] for row in requests if row["packet"]["stage"] == "critique")
    assert all(len(row["packet"]["open_objections"]) == 3 for row in requests if row["packet"]["stage"] == "revision")


def test_paired_analysis_keeps_missing_primaries_and_exposes_zero_defect_uncertainty():
    data, baseline, base_grades, base_map = study("single_provider")
    _, candidate, candidate_grades, candidate_map = study("bounded_debate")
    # Retain unique output identities across both arms as the real bundle does.
    candidate = [replace(row, output_id="candidate-" + row.output_id) for row in candidate]
    packets, mapping = build_blinded_packets(data, baseline + candidate, seed="fixed-analysis")
    by_trial = {value["trial_id"]: token for token, value in mapping["identities"].items()}
    grades = []
    for originals, old in [(base_grades, base_map), (candidate_grades, candidate_map)]:
        for grade in originals:
            identity = old["identities"][grade.packet_token]
            token = by_trial[identity["trial_id"]]
            grades.append(replace(grade, grade_id=token + "-" + grade.reviewer_id, packet_token=token, output_hash=mapping["identities"][token]["public_output_hash"]))
    full = compare_workflows(data, baseline + candidate, grades, mapping, seed="preregistered", resamples=1000)
    assert full["primary_denominator"] == 16 and full["ties"] == 16
    assert full["zero_defect_bounds"]["bounded_debate"]["one_sided_95_upper_rate_if_zero"] > .17
    assert not full["phase_passed"]
    missing_id = next(row.trial_id for row in candidate if row.trial_id.endswith(".r0"))
    retained = [row for row in baseline + candidate if row.trial_id != missing_id]
    grades = [grade for grade in grades if mapping["identities"][grade.packet_token]["trial_id"] != missing_id]
    partial = compare_workflows(data, retained, grades, mapping, seed="preregistered", resamples=1000)
    assert partial["primary_denominator"] == 16 and partial["baseline_wins"] == 1
    assert partial["critical_defect_difference"] is None and partial["zero_defect_bounds"] is None
    assert exact_paired_pvalue(4, 0) == .125
    assert .060 < zero_failure_upper_bound(48) < .061


def test_paired_cost_and_latency_bind_outputs_and_keep_failed_admissions():
    data, baseline, _, _ = study("single_provider", failed={"BUD-01"})
    _, candidate, _, _ = study("bounded_debate")
    candidate = [replace(row, output_id="candidate-" + row.output_id) for row in candidate]
    outputs = baseline + candidate
    _, mapping = build_blinded_packets(data, outputs, seed="cost-binding")
    measurements = [{"trial_id": row.trial_id, "workflow": "single_provider" if row in baseline else "bounded_debate",
        "configuration_hash": row.configuration_fingerprint, "dataset_fingerprint": fingerprint(data), "status": row.status,
        "admitted": True, "mandatory_work_complete": row.status == "completed", "active_seconds": 180,
        "first_useful_active_seconds": 20, "reported_usd": "0.1", "unknown_exposure_usd": "0.02", "conservative_actual_exposure_usd": "0.12"} for row in outputs]
    result = compare_workflows(data, outputs, [], mapping, seed="preregistered", resamples=1000, measurements=measurements)
    performance = result["performance"]["arms"]["single_provider"]
    assert performance["all_retained_outputs_measured"] and performance["reported_usd"] == "2.6"
    assert performance["admitted_trial_timing"]["admitted_trials"] == 26
    assert performance["admitted_trial_timing"]["complete_within_300"]["count"] == 25
    assert performance["unknown_exposure_usd"] == "0.52"
    measurements[0]["unknown_exposure_usd"] = "0"
    with pytest.raises(ValueError, match="exposure"):
        compare_workflows(data, outputs, [], mapping, seed="preregistered", resamples=1000, measurements=measurements)

