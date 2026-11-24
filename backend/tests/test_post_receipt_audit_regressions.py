from dataclasses import replace
from decimal import Decimal

import pytest
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.database.models import (
    Attempt,
    BudgetOperation,
    Event,
    ReportVersion,
    Run,
    Step,
)
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step
from sqlalchemy import select, text

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize(
    "corruption_target", ["failed_uncommitted", "committed_success"]
)
@pytest.mark.parametrize("usage", ["reported", "unknown"])
@pytest.mark.parametrize(
    "raw", ['{"PRIVATE_SENTINEL":', "[" * 10000 + "0" + "]" * 10000]
)
def test_audit_success_with_unreadable_earlier_failed_step_keeps_receipt_discards_content(
    factory, tmp_path, monkeypatch, raw, usage, corruption_target
):
    factory.configure(
        info={
            "artifact_directory": tmp_path / "artifacts",
            "durability_control_directory": tmp_path / "control",
        }
    )
    monkeypatch.setattr(
        "round_robin.orchestration.configuration.verify_frozen_configuration",
        lambda _: None,
    )
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    rid = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        advance_run(session, lock_work_run(session, rid))
    attacked = []

    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            if request.stage == "audit":
                with factory.begin() as session:
                    if corruption_target == "failed_uncommitted":
                        row = Step(
                            id="bad_previous_step",
                            run_id=rid,
                            stage="proposal",
                            role_slot_id="synthetic_other",
                            provider="google",
                            status="failed",
                            result_json={},
                        )
                        session.add(row)
                        session.flush()
                        bad_id = row.id
                    else:
                        bad_id = session.scalar(
                            select(Step.id)
                            .where(
                                Step.run_id == rid,
                                Step.stage == "proposal",
                                Step.status == "succeeded",
                            )
                            .limit(1)
                        )
                        assert bad_id
                    session.execute(
                        text("UPDATE steps SET result_json=:raw WHERE id=:id"),
                        {"raw": raw, "id": bad_id},
                    )
                attacked.append(True)
                return replace(
                    result,
                    cost_usd=Decimal("0.25") if usage == "reported" else None,
                    simulated=False,
                )
            return result

    registry = {p: Provider(p) for p in ("openai", "anthropic", "google")}
    for _ in range(20):
        with factory() as session:
            run = session.get(Run, rid)
            pending = list(
                session.execute(
                    select(Step.id, Step.provider, Step.stage)
                    .where(Step.run_id == rid, Step.status == "pending")
                    .order_by(Step.id)
                )
            )
            assert run.status == "running" and pending
        for sid, provider, stage in pending:
            outcome = execute_step(
                sid, session_factory=factory, adapter_registry=registry
            )
            if stage == "audit":
                break
        if attacked:
            break
    assert attacked and outcome["status"] == "receipt_persisted_state_unavailable"
    with factory() as session:
        run = session.get(Run, rid)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == sid))
        ledger = session.get(BudgetOperation, attempt.id)
        assert run.status == "failed" and run.configuration["saved_state_unavailable"]
        assert attempt.completed_at is not None and attempt.usage_certainty == usage
        assert ledger.status == ("settled" if usage == "reported" else "unknown")
        assert (
            Decimal(ledger.settled_usd) if ledger.settled_usd is not None else None
        ) == (Decimal("0.25") if usage == "reported" else None)
        assert session.scalar(select(Step.status).where(Step.id == sid)) == "authorized"
        events = list(
            session.scalars(
                select(Event).where(
                    Event.run_id == rid, Event.event_type == "content.committed"
                )
            )
        )
        assert not any(event.payload.get("stage") == "audit" for event in events)
        assert (
            session.scalar(select(ReportVersion.id).where(ReportVersion.run_id == rid))
            is None
        )
