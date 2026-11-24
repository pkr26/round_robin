"""Adversarial preregistered study authority checks using synthetic MockHTTP only."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from backend.tests.test_study_runner import funded_study, mock_registry
from experiments.journal import Journal
from experiments.runner import PolicyError
from experiments.study_runner import FundedStudyRunner, StudyBlocked, load_study_approval, set_spending_lock


def test_unregistered_trial_cannot_create_an_extra_paid_run(funded_study):
    _, (approval, manifest, configuration) = funded_study
    registry, requests = mock_registry(configuration)
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry)
        planned = next(trial for trial in runner.plan if trial.workflow == "single_provider")
        invented = replace(planned, trial_id=planned.trial_id + "_invented", repeat_index=99, primary=False)
        with pytest.raises(StudyBlocked, match="preregistered"):
            runner.execute_trial(invented)
        assert not requests and not runner.ledger._state()["runs"]


def test_in_memory_manifest_must_match_the_approved_file_before_sending(funded_study):
    _, (approval, manifest, configuration) = funded_study
    altered = manifest.model_copy(deep=True)
    altered.tasks[0].brief.question = "An unapproved replacement decision with the same signed source file."
    registry, requests = mock_registry(configuration)
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, altered, configuration, provider_registry=registry)
        with pytest.raises(StudyBlocked, match="manifest"):
            runner.execute_trial(next(trial for trial in runner.plan if trial.workflow == "single_provider"))
        assert not requests


def test_lowered_operator_request_cap_still_reserves_all_unfinished_final_roles(funded_study):
    _, (approval, manifest, configuration) = funded_study
    registry, requests = mock_registry(configuration)
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry)
        trial = next(trial for trial in runner.plan if trial.workflow == "single_provider")
        run_id = runner._run_id(trial)
        runner.ledger.open_run(approval.batch_id, run_id, approval.owner_id)
        set_spending_lock(approval.model_copy(update={"batch_requests": 3}), False)
        runner.ledger.authorize(run_id=run_id, step_id="optional_one", attempt_id="optional_one", bound_usd="0", timeout_seconds=1)
        with pytest.raises(PolicyError, match="current_operator_request_limit"):
            runner.ledger.authorize(run_id=run_id, step_id="optional_two", attempt_id="optional_two", bound_usd="0", timeout_seconds=1)
        assert len(runner.ledger._state()["attempts"]) == 1
        assert not requests


@pytest.mark.parametrize("record,mutation", [("discovery_record", []), ("discovery_record", {"interviews": [None] * 5, "status": "completed_actual_participants", "fictional": False}), ("baseline_selection_record", {"development_task_ids": [None, [], {}]}), ("reviewer_plan_record", {"reviewer_ids": [None, [], {}]})])
def test_malformed_human_record_shapes_fail_as_sanitized_study_rejections(funded_study, record, mutation):
    approval_path, _ = funded_study
    approval = json.loads(approval_path.read_text())
    record_path = Path(approval[record]["path"])
    if isinstance(mutation, dict):
        mutation = {**json.loads(record_path.read_text()), **mutation}
    record_path.write_text(json.dumps(mutation))
    approval[record]["sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
    approval_path.write_text(json.dumps(approval))
    with pytest.raises(StudyBlocked):
        load_study_approval(approval_path)


def test_negative_audit_is_visible_in_the_blinded_public_result(funded_study):
    _, (approval, manifest, configuration) = funded_study
    registry, _ = mock_registry(configuration)
    for adapter in registry.values():
        original = adapter.generate
        def reject_audit(request, original=original):
            result = original(request)
            return replace(result, output=result.output.model_copy(update={"audit_passed": False})) if request.stage == "audit" else result
        adapter.generate = reject_audit
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        runner = FundedStudyRunner(journal, approval, manifest, configuration, provider_registry=registry)
        outcome = runner.execute_trial(next(trial for trial in runner.plan if trial.workflow == "single_provider"))
        assert outcome["status"] == "limited" and not outcome["mandatory_work_complete"]
        public = json.loads(outcome["public_text"])
        assert public["audit_findings"]["audit_passed"] is False
        assert any("did not pass" in message for message in public["limitations"])


@pytest.mark.parametrize("trap_inexact", [False, True])
def test_current_study_policy_exact_boundary_ignores_low_precision_callers(funded_study, trap_inexact):
    from decimal import Decimal, Inexact, localcontext
    from experiments.study_runner import StudyLedger
    _, (approval, _, _) = funded_study
    fields = ("batch_usd", "trial_usd", "owner_daily_usd", "owner_monthly_usd", "deployment_daily_usd", "deployment_monthly_usd")
    frozen = approval.model_copy(update={**dict.fromkeys(fields, "0.20"), "finish_role_usd": "0.05"})
    set_spending_lock(frozen.model_copy(update=dict.fromkeys(fields, "0.15")), False)
    with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
        ledger = StudyLedger(journal, frozen)
        ledger.open_batch(frozen.batch_id)
        ledger.open_run(frozen.batch_id, "trial_exact_boundary", frozen.owner_id)
        ledger.authorize(run_id="trial_exact_boundary", step_id="proposal", attempt_id="proposal", bound_usd="0.049")
        with localcontext() as ambient:
            ambient.prec, ambient.traps[Inexact] = 1, trap_inexact
            ledger.authorize(run_id="trial_exact_boundary", step_id="optional", attempt_id="at_boundary", bound_usd="0.001")
            assert ledger._exposure(ledger._state(), ledger._now(), scope="deployment") == Decimal("0.15")
            with pytest.raises(PolicyError, match="current_operator_currency_limit"):
                ledger.authorize(run_id="trial_exact_boundary", step_id="optional_second", attempt_id="above_boundary", bound_usd="0.001")
