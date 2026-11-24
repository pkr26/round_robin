from datetime import timedelta
from decimal import Decimal
from dataclasses import replace
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, Base, BudgetOperation, EvidenceRecord, Meeting, Outbox, Owner, PolicyBucket, PreflightRecord, ReportVersion, Run, Step, utcnow
from round_robin.database.services import enqueue, lock_policies
from round_robin.dispatcher.service import dispatch_once, reconcile_once
from round_robin.fixtures import sample_brief
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import BudgetDenied, authorize_operation, operations_exposure, reconcile_operation
from round_robin.orchestration.controller import advance_run, finalize_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'orchestration.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def seed(factory, *, brief=None, suffix="one", config=None):
    brief = brief or sample_brief()
    with factory() as session, session.begin():
        if session.get(Owner, "owner_test") is None:
            session.add(Owner(id="owner_test"))
            session.flush()
        meeting_id, run_id, preflight_id = f"meeting_{suffix}", f"run_{suffix}", f"preflight_{suffix}"
        session.add(Meeting(id=meeting_id, owner_id="owner_test", title=brief.title))
        session.flush()
        session.add(PreflightRecord(id=preflight_id, meeting_id=meeting_id, owner_id="owner_test", draft_revision=brief.revision, draft_hash="a" * 64, configuration_hash="b" * 64, payload={}, expires_at=utcnow() + timedelta(minutes=10)))
        session.flush()
        run = Run(id=run_id, meeting_id=meeting_id, owner_id="owner_test", preflight_id=preflight_id, input_snapshot=brief.model_dump(mode="json"), configuration=config or run_configuration(Settings(), "simulated"), initial_admission_expires_at=utcnow() + timedelta(minutes=30))
        session.add(run)
        session.flush()
        enqueue(session, run)
    return run_id


def drain(factory, registry=None, ceiling=30):
    for _ in range(ceiling):
        with factory() as session:
            pending = list(session.scalars(select(Step.id).where(Step.status == "pending")))
            run_ids = list(session.scalars(select(Run.id).where(Run.status.in_(["queued", "running"])) ))
        for run_id in run_ids:
            with factory() as session, session.begin():
                run = lock_work_run(session, run_id)
                advance_run(session, run)
        for step_id in pending:
            elapse_retry_wait(factory, step_id)
            execute_step(step_id, session_factory=factory, adapter_registry=registry)
        if not pending and not run_ids:
            return
    raise AssertionError("workflow did not settle within bounded drain")


def elapse_retry_wait(factory, step_id):
    """The synthetic drain advances a saved retry timer; execution never bypasses it."""
    with factory.begin() as session:
        step = session.get(Step, step_id)
        run = session.get(Run, step.run_id)
        controls = dict(run.configuration.get("step_dispatch", {}))
        if step_id in controls and controls[step_id].get("not_before"):
            controls[step_id] = {**controls[step_id], "not_before": (utcnow() - timedelta(seconds=1)).isoformat()}
            run.configuration = {**run.configuration, "step_dispatch": controls}


def test_complete_arbitrary_simulated_brief_uses_own_evidence_and_distinct_audit(factory):
    brief = sample_brief().model_copy(update={"title": "My independent custom workshop decision"})
    run_id = seed(factory, brief=brief)
    drain(factory)
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == "completed"
        steps = list(session.scalars(select(Step).where(Step.run_id == run_id)))
        assert len(steps) == 6
        synthesis = next(step for step in steps if step.stage == "synthesis")
        audit = next(step for step in steps if step.stage == "audit")
        assert synthesis.provider != audit.provider
        assert all(step.calls == 1 for step in steps)
        attempts = list(session.scalars(select(Attempt).where(Attempt.run_id == run_id)))
        assert len(attempts) == 6
        assert all(not attempt.request_json["peer_outputs"] for attempt in attempts if attempt.request_json["stage"] == "proposal")
        evidence = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id)))
        assert all(item.id.startswith("evidence_") for item in evidence)
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content
        assert report["simulated"] and report["readiness"] != "ready_to_choose"
        assert "My independent custom workshop decision" in synthesis.result_json["output"]["summary"]
        assert report["usage"]["actual_cost"] == "0"
        assert report["comparison"][0]["label"] == brief.options[0].label


def test_clarification_wait_releases_admission_and_successful_intake_is_not_repeated(factory):
    brief = sample_brief().model_copy(update={"horizon_months": None})
    run_id = seed(factory, brief=brief)
    drain(factory)
    with factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        assert run.status == "awaiting_input" and run.active_started_at is None
        assert len(run.configuration["questions"]) <= 3
        corrected = dict(run.input_snapshot, horizon_months=24)
        run.input_snapshot = corrected
        run.status, run.stage = "queued", "proposals"
        run.resumption_expires_at = utcnow() + timedelta(minutes=30)
    drain(factory)
    with factory() as session:
        intake = list(session.scalars(select(Step).where(Step.run_id == run_id, Step.stage == "intake")))
        assert len(intake) == 1 and intake[0].calls == 1
        assert session.get(Run, run_id).status == "completed"


def test_duplicate_delivery_is_idempotent_and_cancellation_fences_late_content(factory):
    run_id = seed(factory)
    with factory() as session, session.begin():
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
    class CancellingProvider(SimulatedProvider):
        def generate(self, request):
            with factory() as session, session.begin():
                run = lock_work_run(session, request.run_id)
                run.status = "cancel_requested"
                run.execution_fence += 1
                finalize_run(session, run)
            return super().generate(request)
    provider = CancellingProvider("openai")
    registry = {"openai": provider}
    assert execute_step(step_id, session_factory=factory, adapter_registry=registry)["status"] == "late_result_fenced"
    execute_step(step_id, session_factory=factory, adapter_registry=registry)
    assert provider.calls == 1
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == "cancelled"
        assert session.get(Step, step_id).result_json is None
        assert len(list(session.scalars(select(Attempt)))) == 1


def test_refusal_and_two_schema_failures_settle_without_fabricated_report(factory):
    seed(factory)
    registry = {provider: SimulatedProvider(provider, "refusal") for provider in ("openai", "anthropic", "google")}
    drain(factory, registry)
    assert registry["openai"].calls == 1
    with factory() as session:
        run = session.get(Run, "run_one")
        assert run.status == "failed"
        report = session.scalar(select(ReportVersion)).content
        assert report["recommendation"] is None and report["readiness"] is None


def test_moderator_fallback_keeps_logical_calls_and_audit_identity_distinct(factory):
    seed(factory)
    registry = {provider: SimulatedProvider(provider, "authentication" if provider == "openai" else "success") for provider in ("openai", "anthropic", "google")}
    drain(factory, registry)
    with factory() as session:
        run = session.get(Run, "run_one")
        assert run.status == "completed_with_limitations"
        steps = list(session.scalars(select(Step)))
        synthesis = next(step for step in steps if step.stage == "synthesis")
        audit = next(step for step in steps if step.stage == "audit")
        assert synthesis.provider == "anthropic" and audit.provider == "google"
        assert synthesis.calls == audit.calls == 2
        assert next(step for step in steps if step.stage == "proposal" and step.role_slot_id == "openai").status == "failed"
        assert all(step.calls <= 2 for step in steps)


def test_pending_schema_repair_keeps_barrier_open_then_stops_at_second_call(factory):
    run_id = seed(factory)
    with factory() as session, session.begin():
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id))
    provider = SimulatedProvider("openai", "malformed")
    execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
    with factory() as session:
        assert session.get(Step, step_id).status == "pending"
        assert session.get(Run, run_id).status == "running"
        assert session.scalar(select(ReportVersion)) is None
    elapse_retry_wait(factory, step_id)
    execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
    with factory() as session:
        assert session.get(Step, step_id).calls == 2
        assert session.get(Run, run_id).status == "failed"
    assert provider.calls == 2


def test_followup_report_names_changed_inputs_parent_version_and_next_evidence(factory):
    parent_id = seed(factory)
    drain(factory)
    child_brief = sample_brief().model_copy(update={"horizon_months": 24, "budget": "2000"})
    child_id = seed(factory, brief=child_brief, suffix="child")
    with factory() as session, session.begin():
        child = session.get(Run, child_id)
        parent = session.get(Run, parent_id)
        child.parent_run_id = parent_id
        child.meeting_id = parent.meeting_id
    drain(factory)
    with factory() as session:
        parent = session.scalar(select(ReportVersion).where(ReportVersion.run_id == parent_id)).content
        child = session.scalar(select(ReportVersion).where(ReportVersion.run_id == child_id)).content
        assert child["parent_report_id"] == parent["id"]
        assert any("Budget changed" in change for change in child["change_summary"])
        assert any("horizon changed" in change for change in child["change_summary"])
        assert child["next_evidence"] and child["next_evidence"][0]["request_text"]
        assert all(item["affected_conclusion"] and item["verification_method"] for item in child["next_evidence"])


def test_persistent_dispatch_pause_and_drain_do_not_start_new_work(factory):
    run_id = seed(factory)
    with factory() as session, session.begin():
        deployment, _ = lock_policies(session, "owner_test")
        deployment.metadata_json = {"dispatch_locked": True}
        advance_run(session, lock_work_run(session, run_id))
    with factory() as session:
        assert session.get(Run, run_id).status == "queued"
        assert session.scalar(select(Step)) is None
    assert dispatch_once(session_factory=factory, sender=lambda *args: pytest.fail("paused publication")) == 0
    with factory() as session, session.begin():
        deployment, _ = lock_policies(session, "owner_test")
        deployment.metadata_json = {"draining": True}
        advance_run(session, lock_work_run(session, run_id))
        assert session.get(Run, run_id).status == "queued"


def test_live_dispatch_rechecks_operator_file_expiry_hash_and_funding(tmp_path):
    from backend.tests.test_provider_configuration import synthetic_configuration
    from round_robin.orchestration.configuration import verify_frozen_configuration
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(synthetic_configuration()))
    configured = replace(Settings(), provider_config_file=path, paid_access_enabled=True)
    frozen = run_configuration(configured, "live")
    run = Run(id="run_config_test", configuration=frozen, deployment_epoch=configured.deployment_epoch)
    verify_frozen_configuration(run, operator_settings=configured)
    changed = synthetic_configuration()
    changed["version"] = "changed-after-accepted-start"
    path.write_text(json.dumps(changed))
    with pytest.raises(BudgetDenied, match="configuration_changed"):
        verify_frozen_configuration(run, operator_settings=configured)
    run.configuration = {**frozen, "expires_at": (utcnow() - timedelta(seconds=1)).isoformat()}
    with pytest.raises(BudgetDenied, match="configuration_expired"):
        verify_frozen_configuration(run, operator_settings=configured)
    run.configuration = frozen
    with pytest.raises(BudgetDenied, match="funding_disabled"):
        verify_frozen_configuration(run, operator_settings=replace(configured, paid_access_enabled=False))


def test_lost_worker_after_authorization_preserves_unknown_and_never_repeats_attempt(factory):
    run_id = seed(factory)
    with factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        authorize_operation(session, run, "attempt_lost", "model", "0", step=step)
        step.status, step.lease_owner = "authorized", "old_executor"
        step.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(Attempt(id="attempt_lost", run_id=run_id, step_id=step.id, provider=step.provider, lease_owner="old_executor", reserved_usd="0", configuration={"execution_fence": 0}))
    reconcile_once(session_factory=factory)
    with factory() as session:
        attempt = session.get(Attempt, "attempt_lost")
        assert attempt.usage_certainty == "unknown"
        assert session.get(BudgetOperation, "attempt_lost").status == "unknown"
        assert session.get(Run, run_id).status == "failed"
        assert len(list(session.scalars(select(Attempt)))) == 1


def test_outbox_survives_publish_failure_and_duplicate_delivery(factory):
    seed(factory)
    def failed(*args):
        raise ConnectionError("synthetic broker unavailable")
    assert dispatch_once(session_factory=factory, sender=failed) == 0
    with factory() as session, session.begin():
        item = session.scalar(select(Outbox))
        assert item.status == "pending"
        item.due_at = utcnow() - timedelta(seconds=1)
    sent = []
    assert dispatch_once(session_factory=factory, sender=lambda kind, value: sent.append((kind, value))) == 1
    assert sent == [("advance", "run_one")]


def test_aggregate_unknown_carry_and_finish_pool_conversion(factory, monkeypatch):
    # Isolate ledger policy; live configuration file validation has its own tests.
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda run: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    run_id = seed(factory, config=config)
    with factory() as session, session.begin():
        deployment, owner = lock_policies(session, "owner_test")
        for policy in (deployment, owner):
            policy.spending_locked = False
            policy.limit_usd = "0.6"
            policy.metadata_json = {"daily_usd": "0.6", "monthly_usd": "0.6"}
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        hold = session.get(BudgetOperation, f"finish_{run_id}")
        assert hold.reserved_usd == "0.40"
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        authorize_operation(session, run, "cost_unknown", "model", "0.20", step=step)
        reconcile_operation(session, "cost_unknown", None, "unknown")
        session.get(BudgetOperation, "cost_unknown").authorized_at = utcnow() - timedelta(days=40)
        assert operations_exposure(list(session.scalars(select(BudgetOperation))), period="daily") == Decimal("0.60")
        with pytest.raises(BudgetDenied, match="budget_exhausted"):
            authorize_operation(session, run, "excess", "model", "0.01", step=step)


def test_new_stage_iteration_cannot_reset_same_logical_role_call_allowance(factory):
    run_id = seed(factory)
    with factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        authorize_operation(session, run, "attempt_first", "model", "0", step=step)
        authorize_operation(session, run, "attempt_second", "model", "0", step=step)
        invented_new_step = Step(id="step_new_iteration", run_id=run.id, stage=step.stage, round=step.round, role_slot_id=step.role_slot_id, provider=step.provider, stage_iteration=1, calls=0, infrastructure_claims=0, generation=run.recovery_generation)
        session.add(invented_new_step)
        session.flush()
        with pytest.raises(BudgetDenied, match="step_call_limit"):
            authorize_operation(session, run, "attempt_reset_bypass", "model", "0", step=invented_new_step)


def test_direct_brief_budget_and_deadline_cannot_omit_essential_requirements(factory):
    brief = sample_brief().model_copy(update={"hard_requirements": []})
    run_id = seed(factory, brief=brief)
    with factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        run.stage = "proposals"
        advance_run(session, run)
        resolved = run.configuration["resolved_brief"]
        assert any(item["metric"] == "total_cost" and item["operator"] == "lte" and item["threshold"] == brief.budget for item in resolved["hard_requirements"])
        assert any(item["metric"] == "manual" and brief.deadline.isoformat() in item["description"] for item in resolved["hard_requirements"])
        assert run.input_snapshot["hard_requirements"] == []


def test_corrupt_saved_passage_blocks_provider_dispatch_and_keeps_honest_partial(factory):
    run_id = seed(factory)
    with factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        intake_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
    execute_step(intake_id, session_factory=factory)
    with factory() as session, session.begin():
        source = session.scalar(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id))
        source.content = {**source.content, "text": "Corrupted bytes cannot replace the stored passage."}
        proposal_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "proposal"))
    provider = SimulatedProvider("openai")
    result = execute_step(proposal_id, session_factory=factory, adapter_registry={provider: SimulatedProvider(provider) for provider in ("openai", "anthropic", "google")})
    assert result["status"] == "policy_blocked" and result["code"] == "evidence_integrity_failed"
    with factory() as session:
        assert len(list(session.scalars(select(Attempt)))) == 1  # intake only
        report = session.scalar(select(ReportVersion)).content
        assert report["comparison"] == [] and report["recommendation"] is None and report["readiness"] is None
