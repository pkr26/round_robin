"""Synthetic authorization records and MockHTTP exercise the funded runner.

These fixtures are not actual participant observations, funding or model-quality
evidence. They verify fail-closed controls; no real-provider request is sent.
"""
from collections import Counter
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import os
import stat
import threading

import httpx
import pytest

from backend.tests.test_provider_configuration import synthetic_configuration
from experiments.evaluation import load_dataset
from experiments.journal import Journal
from experiments.runner import OfflineRunner, PolicyError
from experiments.study_runner import FrozenStudyManifest, FundedStudyRunner, StudyBlocked, load_study_approval, main, private_write, set_spending_lock, trial_plan
from round_robin.database.models import utcnow
from round_robin.fixtures import sample_brief, sample_evidence
from round_robin.providers.base import ProviderOutput
from round_robin.providers.http import HTTPProvider


def evidence_file(path, value):
    private_write(path, value)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def funded_study(tmp_path):
    dataset = load_dataset()
    # Expanded typed feedback schemas require enough synthetic context for the
    # complete healthy transcript; this does not relax production request caps.
    provider_config = synthetic_configuration()
    for capability in provider_config["capabilities"].values():
        capability["context_window_tokens"] = 128000
    snapshots = [{"id": passage.id, "text": passage.text, "sha256": passage.sha256, "source_title": passage.source_title, "locator": passage.locator, "origin": "fictional", "checked_at": (utcnow() - timedelta(minutes=3)).isoformat(), "checked_by": "synthetic-test-author", "check_basis": "Synthetic arithmetic and control fixture, not real supplier evidence."} for passage in sample_evidence()]
    manifest = {"dataset_id": "synthetic-typed-study", "version": 1, "status": "frozen_human_reviewed", "frozen_at": (utcnow() - timedelta(minutes=2)).isoformat(), "frozen_by": "synthetic-test-author", "label": "Synthetic control fixture only; not actual discovery or funded results.", "repeated_task_ids": dataset["repeated_task_ids"], "double_review_task_ids": dataset["double_review_task_ids"], "tasks": [{"task_id": task["task_id"], "version": 1, "stratum": task["stratum"], "brief": sample_brief().model_dump(mode="json"), "acceptable_outcomes": ["Expose uncertain feasibility in this synthetic fixture."], "critical_dependencies": ["Synthetic source support is not real-world verification."], "checked_snapshots": snapshots} for task in dataset["tasks"]]}
    approval = {"approval_id": "synthetic-approval", "authorized_by": "synthetic-test-operator", "authorized_at": (utcnow() - timedelta(minutes=1)).isoformat(), "expires_at": (utcnow() + timedelta(hours=1)).isoformat(), "batch_id": "synthetic-batch", "owner_id": "synthetic-owner", "deployment_id": "synthetic-deployment", "journal_directory": str(tmp_path / "authority"), "spending_policy_file": str(tmp_path / "spending-policy.json"), "manifest": evidence_file(tmp_path / "manifest.json", manifest), "provider_configuration": evidence_file(tmp_path / "providers.json", provider_config), "discovery_record": evidence_file(tmp_path / "discovery.json", {"status": "completed_actual_participants", "fictional": False, "testing_notice": "Synthetic authorization-validator fixture; no actual people participated.", "interviews": [{"participant_id": f"synthetic-participant-{index}", "consent_reference": "synthetic-consent", "recent_decision": "Synthetic choice fixture", "target_audience": True, "completed_unaided": True, "located_recommendation_or_no_choice": True, "located_caveat": True, "located_exact_support": True, "located_next_action": True} for index in range(5)]}), "baseline_selection_record": evidence_file(tmp_path / "baseline.json", {"status": "selected_on_separate_development_cases", "selected_provider": "openai", "self_check_enabled": True, "development_task_ids": ["DEV-01", "DEV-02", "DEV-03"], "selection_reason": "Synthetic fixture selection record; not a real baseline-quality finding.", "evidence_output_hashes": [hashlib.sha256(f"synthetic-output-{index}".encode()).hexdigest() for index in range(3)]}), "reviewer_plan_record": evidence_file(tmp_path / "reviewers.json", {"status": "preregistered", "reviewer_ids": ["synthetic-rater-1", "synthetic-rater-2"], "double_review_task_ids": manifest["double_review_task_ids"], "all_suspected_critical_defects_double_reviewed": True}), "baseline_provider": "openai", "synthesizer": "openai", "auditor": "google", "execution_order_seed": "synthetic-order-v1", "blinding_seed": "synthetic-blind-v1", "batch_requests": 1872, "batch_usd": "156", "trial_usd": "2", "finish_role_usd": "0.20", "owner_daily_usd": "10", "owner_monthly_usd": "50", "deployment_daily_usd": "10", "deployment_monthly_usd": "50", "fixed_evidence_only": True, "all_three_provider_recipients_approved": True, "other_paid_execution_disabled": True}
    for case, original in zip(manifest["tasks"], dataset["tasks"]):
        case["brief"]["question"] = original["brief"]
    # Update the source file hash after constructing distinct substantive cases.
    manifest_path = Path(approval["manifest"]["path"])
    manifest_path.write_text(json.dumps(manifest))
    approval["manifest"]["sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    approval_path = tmp_path / "approval.json"
    private_write(approval_path, approval)
    parsed = load_study_approval(approval_path)
    set_spending_lock(parsed[0], False)
    return approval_path, parsed


def mock_registry(configuration, *, on_request=None, missing_usage=False, cite_peers=False):
    records, lock = [], threading.Lock()
    raw_configuration = {**configuration.model_dump(mode="json"), "mode": "live"}
    def adapter(provider):
        def send(request):
            body = json.loads(request.content)
            text = body["input"][0]["content"] if provider == "openai" else body["messages"][0]["content"] if provider == "anthropic" else body["contents"][0]["parts"][0]["text"]
            packet = json.loads(text)
            with lock:
                records.append({"provider": provider, "packet": packet})
            if on_request:
                on_request(provider, packet)
            brief = packet["brief"]
            output = ProviderOutput(summary="Synthetic MockHTTP result identifying the supplied decision and its uncertainty.", recommendation_option_id=brief["options"][0]["id"], reference_ids=packet["allowed_reference_ids"][:1], questions=[], assumptions=["Synthetic test only."], constraints=[], audit_passed=True if packet["stage"] == "audit" else None, critical_defects=[], dissent=["Synthetic material objection requiring author revision."] if packet["stage"] == "critique" else [])
            if cite_peers and packet["peer_outputs"]:
                identity = packet["peer_outputs"][0]["id"]
                output = output.model_copy(update={"reference_ids": [identity], "summary": f"See {identity} for the linked adviser assertion and its evidence."})
            result_text = output.model_dump_json()
            usage = {} if missing_usage else {"input_tokens": 100, "output_tokens": 50}
            if provider == "openai":
                raw = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": result_text}]}], "usage": usage}
            elif provider == "anthropic":
                raw = {"stop_reason": "end_turn", "content": [{"type": "text", "text": result_text}], "usage": usage}
            else:
                raw = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": result_text}]}}], "usageMetadata": {} if missing_usage else {"promptTokenCount": 100, "totalTokenCount": 150}}
            return httpx.Response(200, headers={"x-request-id": f"synthetic-{provider}-request"}, json=raw)
        return HTTPProvider(provider, raw_configuration, transport=httpx.MockTransport(send), api_key="synthetic-transport-only-key")
    return {provider: adapter(provider) for provider in ("openai", "anthropic", "google")}, records


def test_frozen_study_has_78_stable_trials_primary_before_repeats_and_exact_strata(funded_study):
    _, (approval, manifest, _) = funded_study
    plan = trial_plan(manifest, approval.execution_order_seed)
    assert len(plan) == len({trial.trial_id for trial in plan}) == 78
    assert Counter(trial.workflow for trial in plan) == dict.fromkeys(("single_provider", "independent_review", "bounded_debate"), 26)
    assert all(trial.primary for trial in plan[:48]) and not any(trial.primary for trial in plan[48:])
    assert trial_plan(manifest, approval.execution_order_seed) == plan


def test_all_three_funded_workflows_execute_with_mock_http_and_keep_raw_outputs(funded_study):
    _, (approval, manifest, configuration) = funded_study
    registry, requests = mock_registry(configuration)
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry, sleeper=lambda _: None)
        trials = [next(trial for trial in runner.plan if trial.workflow == workflow and trial.primary) for workflow in ("single_provider", "independent_review", "bounded_debate")]
        outputs = [runner.execute_trial(trial) for trial in trials]
        # The fixture's unclassified dissent is minor; only material open
        # obligations justify the bounded revision round.
        assert [output["request_count"] for output in outputs] == [3, 5, 9]
        assert all(output["status"] == "completed" and output["phase_passed"] is False for output in outputs)
        count = len(requests)
        assert runner.execute_trial(trials[0]) == outputs[0]
        assert len(requests) == count
        assert all(not request["packet"]["peer_outputs"] for request in requests if request["packet"]["stage"] == "proposal")
        assert any(request["packet"]["stage"] == "critique" for request in requests)
        assert not any(request["packet"]["stage"] == "revision" for request in requests)
        assert outputs[-1]["discussion_issues"] and all(issue["severity"] == "minor" for issue in outputs[-1]["discussion_issues"])
        response = next(runner.artifacts.rglob("*.response.json"))
        assert json.loads(response.read_text())["raw_text"]
        assert response.stat().st_mode & 0o077 == 0


def test_funded_batch_completes_78_mock_trials_and_blinds_provider_metadata(funded_study):
    _, (approval, manifest, configuration) = funded_study
    registry, requests = mock_registry(configuration)
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry, sleeper=lambda _: None)
        summary = runner.run()
        assert summary["retained_outputs"] == 78 and summary["authorized_requests_in_batch"] == len(requests) == 442
        assert summary["phase_passed"] is False and summary["repeat_denominator_contribution"] == 0
        assert summary["smoke_requests"] == 0 and not summary["not_executed_trial_ids"]
        packets_path = next(runner.artifacts.rglob("reviewer_packets.json"))
        packets = json.loads(packets_path.read_text())
        assert len(packets) == 78
        assert "configuration_hash" not in json.dumps(packets)


def test_crash_after_authorization_preserves_unknown_and_restart_never_replays(funded_study):
    _, (approval, manifest, configuration) = funded_study
    def crash(provider, packet):
        raise KeyboardInterrupt
    registry, sent = mock_registry(configuration, on_request=crash)
    trial = next(trial for trial in trial_plan(manifest, approval.execution_order_seed) if trial.workflow == "single_provider")
    with pytest.raises(KeyboardInterrupt):
        with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
            FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry).execute_trial(trial)
    assert len(sent) == 1
    healthy, later = mock_registry(configuration)
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=healthy)
        output = runner.execute_trial(trial)
        assert output["status"] == "failed" and output["unknown_attempt_ids"]
        assert not later and len(runner.ledger._state()["attempts"]) == 1


def test_operator_lock_commit_fences_every_later_authorization(funded_study):
    _, (approval, manifest, configuration) = funded_study
    def stop_after_first(provider, packet):
        set_spending_lock(approval, True)
    registry, sent = mock_registry(configuration, on_request=stop_after_first)
    trial = next(trial for trial in trial_plan(manifest, approval.execution_order_seed) if trial.workflow == "single_provider")
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry)
        output = runner.execute_trial(trial)
        assert output["status"] == "limited" and output["request_count"] == 1 and len(sent) == 1


def test_changed_human_record_or_cached_outcome_is_rejected_without_another_call(funded_study):
    approval_path, (approval, manifest, configuration) = funded_study
    registry, sent = mock_registry(configuration)
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry)
        trial = next(trial for trial in runner.plan if trial.workflow == "single_provider")
        runner.execute_trial(trial)
        path = runner._trial_directory(trial) / "outcome.json"
        original = json.loads(path.read_text())
        original["public_text"] = "A favorable replacement must never overwrite the primary."
        path.write_text(json.dumps(original))
        with pytest.raises(StudyBlocked, match="outcome_hash"):
            runner.execute_trial(trial)
    assert len(sent) == 3
    approval.discovery_record.path.write_text("{}")
    with pytest.raises(StudyBlocked, match="hash_mismatch"):
        load_study_approval(approval_path)


def test_default_simulator_still_rejects_credentials_and_study_cli_needs_explicit_live(funded_study, monkeypatch):
    approval_path, (approval, _, _) = funded_study
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key-that-must-not-print")
    with Journal(approval.journal_directory.parent / "offline") as journal:
        with pytest.raises(Exception, match="credentials"):
            OfflineRunner(journal)
    assert main(["--approval-file", str(approval_path)]) == 2


def test_renamed_duplicate_cases_cannot_inflate_independent_study_coverage(funded_study):
    _, (_, manifest, _) = funded_study
    raw = manifest.model_dump(mode="json")
    raw["tasks"][1]["brief"] = {**raw["tasks"][0]["brief"], "title": "A different title is still the same substantive task."}
    raw["tasks"][1]["checked_snapshots"] = raw["tasks"][0]["checked_snapshots"]
    with pytest.raises(ValueError, match="Aliased or renamed copies"):
        FrozenStudyManifest.model_validate(raw)


def test_adversarial_model_peer_references_are_opaque_in_every_public_output(funded_study):
    _, (approval, manifest, configuration) = funded_study
    registry, _ = mock_registry(configuration, cite_peers=True)
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry)
        for workflow in ("single_provider", "independent_review", "bounded_debate"):
            trial = next(trial for trial in runner.plan if trial.workflow == workflow)
            output = runner.execute_trial(trial)
            assert "proposal_openai" not in output["public_text"]
            assert "proposal_baseline" not in output["public_text"]
            assert "critique_" not in output["public_text"] and "revision_" not in output["public_text"]
            assert "ref_" in output["public_text"] and output["public_reference_map"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_funded_journal_is_private_even_with_permissive_umask(funded_study):
    _, (approval, manifest, configuration) = funded_study
    previous = os.umask(0)
    try:
        with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
            registry, _ = mock_registry(configuration)
            FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry)
            assert stat.S_IMODE(journal.path.stat().st_mode) == 0o700
            for filename in ("lock", "manifest.json", "events.jsonl", "head.json"):
                assert stat.S_IMODE((journal.path / filename).stat().st_mode) == 0o600
    finally:
        os.umask(previous)
