"""Offline adversarial algorithm probes; synthetic inputs are not quality evidence.

Run from the repository root:
  PYTHONPATH=backend/src:. uv run python docs/evidence/algorithm-audit-2026-11-17/evaluation/probe.py
No product files are modified. All providers use injected in-memory HTTP mocks.
"""
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from backend.tests.test_study_runner import funded_study, mock_registry
from backend.tests.test_image_provider import pixels
from backend.tests.test_release_evaluation import manifest as release_manifest, result as release_result
from experiments.journal import Journal
from experiments.release import validate_manifest as validate_release, summarize as summarize_release
from experiments.study_runner import FundedStudyRunner
from round_robin.visual.reviews import PixelView, issue_review_receipt, validate_critique, coverage
from round_robin.visual.schemas import ImageCapabilities, Critique
from round_robin.worker.image_tasks import _packet
from round_robin.fixtures import sample_brief


def study_failed_audit():
    with tempfile.TemporaryDirectory() as temporary:
        _, (approval, manifest, configuration) = funded_study.__wrapped__(Path(temporary))
        registry, requests = mock_registry(configuration)
        for provider in registry.values():
            original = provider.generate
            def fail_audit(request, original=original):
                result = original(request)
                if request.stage == "audit":
                    output = result.output.model_copy(update={"audit_passed": False, "critical_defects": ["Synthetic decisive defect requiring correction."]})
                    return replace(result, output=output, raw_text=output.model_dump_json())
                return result
            provider.generate = fail_audit
        with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
            runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry, sleeper=lambda _: None)
            trial = next(trial for trial in runner.plan if trial.workflow == "bounded_debate" and trial.primary)
            outcome = runner.execute_trial(trial)
            stages = [row["stage"] for row in outcome["stage_records"]]
            counts = dict(Counter(stages))
            assert counts["synthesis"] == counts["audit"] == 1
            assert outcome["status"] == "limited" and outcome["request_count"] == 12
            return {"reproduced": True, "status": outcome["status"], "stage_counts": counts, "request_count": outcome["request_count"], "active_seconds": outcome["active_seconds"], "correction_or_reaudit_executed": False, "note": "Mocked failed final audit stops the study workflow, although the live product has a correction/re-audit transition."}


def edit_packet_loses_dispositions():
    raw = pixels()
    digest = hashlib.sha256(raw).hexdigest()
    version = SimpleNamespace(id="version_parent", session_id="design_probe", canonical_hash=digest)
    design = SimpleNamespace(id="design_probe", owner_id="owner_probe", frozen_brief={"purpose": "Synthetic diagram", "locked_requirements": ["Keep title"]})
    payload = {"kind": "edit", "step_id": "edit:version_2", "parent_version_id": version.id, "input_hash": digest, "edit_intent": "Address the selected finding; preserve the declined change.", "finding_dispositions": {"finding_fix": "address", "finding_keep": "decline"}}
    session = SimpleNamespace(get=lambda model, identity: version)
    with patch("round_robin.worker.image_tasks._request_allowance", return_value=60), patch("round_robin.worker.image_tasks.owned_asset_bytes", return_value=raw):
        packet = _packet(session, design, SimpleNamespace(payload=payload), None, Path("/unused"))
    public = {key: value for key, value in packet.items() if key != "images"}
    assert "finding_dispositions" not in packet and "findings" not in packet
    assert all(identity not in json.dumps(public) for identity in payload["finding_dispositions"])
    return {"reproduced": True, "saved_dispositions": payload["finding_dispositions"], "outgoing_packet_metadata": public, "missing_selected_findings": True}


def missing_regression_dispositions():
    raw = pixels()
    digest = hashlib.sha256(raw).hexdigest()
    view = PixelView(raw, digest)
    receipts, critiques = [], []
    prior = {"finding_previous_critical"}
    for provider in ("anthropic", "google"):
        caps = ImageCapabilities(provider=provider, endpoint_version="synthetic", understands_images=True, supported_media=["image/png"])
        receipt = issue_review_receipt(caps, "version_child", digest, [view])
        critique = Critique(provider=provider, version_id=receipt.version_id, canonical_hash=digest, receipt_id=receipt.id, findings=[], limitations=[])
        validate_critique(critique, receipt, prior_finding_ids=prior)
        receipts.append(receipt)
        critiques.append(critique)
    result = coverage(critiques, receipts, version_id="version_child", canonical_hash=digest, generator_provider="openai")
    assert result["complete"] is True
    return {"reproduced": True, "prior_findings": sorted(prior), "child_findings": [], "coverage": result, "note": "Coverage correctly counts reviewers but there is no separate mandatory pending/settled regression matrix."}


def aliased_release_cases():
    manifest = release_manifest()
    for index, task in enumerate(manifest["tasks"]):
        task["brief"] = sample_brief().model_dump(mode="json")
        task["brief"]["title"] = f"Same substantive decision, alias {index}"
    digest = validate_release(manifest)
    summary = summarize_release(manifest, [release_result(task, digest) for task in manifest["tasks"]])
    assert summary["independent_primary_tasks"] == 48 and summary["useful_primary_tasks"] == 48
    return {"reproduced": True, "distinct_substantive_cases": 1, "reported_independent_primary_tasks": summary["independent_primary_tasks"], "useful_primary_tasks": summary["useful_primary_tasks"], "numerical_usefulness_target": summary["numerical_usefulness_target"], "phase_5_passed": summary["phase_5_passed"]}


if __name__ == "__main__":
    results = {"synthetic_offline_probes": True, "provider_network_calls": 0, "study_failed_audit": study_failed_audit(), "edit_packet_loses_dispositions": edit_packet_loses_dispositions(), "missing_regression_dispositions": missing_regression_dispositions(), "aliased_release_cases": aliased_release_cases()}
    print(json.dumps(results, indent=2))
