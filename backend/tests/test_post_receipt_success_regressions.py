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
from sqlalchemy.orm import Session

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("usage", ["reported"])
@pytest.mark.parametrize(
    "raw", ['{"PRIVATE_SENTINEL":', "[" * 10000 + "0" + "]" * 10000]
)
def test_saved_step_decode_rollback_keeps_authenticated_receipt(
    factory, tmp_path, monkeypatch, usage, raw
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
        run = lock_work_run(session, rid)
        advance_run(session, run)
        sid = session.scalar(
            select(Step.id).where(Step.run_id == rid, Step.stage == "intake")
        )
        assert sid
    after_send = []
    dirty = []
    original = Session.begin_nested

    def inspect_nested(session):
        if after_send:
            dirty.append(
                (
                    {(type(obj).__name__, obj.id) for obj in session.dirty},
                    list(session.new),
                    list(session.deleted),
                )
            )
        return original(session)

    monkeypatch.setattr(Session, "begin_nested", inspect_nested)

    class Provider(SimulatedProvider):
        def conservative_bound(self, request):
            return Decimal("0.40")

        def generate(self, request):
            result = super().generate(request)
            with factory.begin() as session:
                bad = Step(
                    id="step_corrupt",
                    run_id=rid,
                    stage="proposal",
                    role_slot_id="other",
                    provider="google",
                    status="failed",
                    result_json={},
                )
                session.add(bad)
                session.flush()
                session.execute(
                    text("UPDATE steps SET result_json=:raw WHERE id=:id"),
                    {"raw": raw, "id": bad.id},
                )
            after_send.append(True)
            return replace(result, cost_usd=Decimal("0.25"), simulated=False)

    provider = Provider("openai")
    outcome = execute_step(
        sid, session_factory=factory, adapter_registry={"openai": provider}
    )
    assert (
        outcome["status"] == "receipt_persisted_state_unavailable"
        and provider.calls == 1
    )
    assert dirty[0] == (
        {
            ("Attempt", outcome["attempt_id"]),
            ("BudgetOperation", outcome["attempt_id"]),
        },
        [],
        [],
    )
    with factory() as session:
        attempt = session.get(Attempt, outcome["attempt_id"])
        ledger = session.get(BudgetOperation, attempt.id)
        run = session.get(Run, rid)
        assert attempt.completed_at and attempt.usage_certainty == usage
        assert attempt.response_json == {
            "content_discarded": True,
            "error_code": "saved_state_integrity_failed",
            "provider_error_code": None,
        }
        assert ledger.status == ("settled" if usage == "reported" else "unknown")
        assert (
            Decimal(ledger.settled_usd) if ledger.settled_usd is not None else None
        ) == (Decimal("0.25") if usage == "reported" else None)
        assert (
            run.status == "failed"
            and run.stop_reason == "saved_state_integrity_failed"
            and run.configuration["saved_state_unavailable"]
        )
        assert session.get(BudgetOperation, f"finish_{rid}").status == "held"
        assert (
            session.scalar(select(ReportVersion.id).where(ReportVersion.run_id == rid))
            is None
        )
        assert (
            session.scalar(
                select(Event.id).where(
                    Event.run_id == rid,
                    Event.event_type.in_(
                        ["content.committed", "report.published", "step.failed"]
                    ),
                )
            )
            is None
        )
        assert session.scalar(select(Step.status).where(Step.id == sid)) == "authorized"
    execute_step(sid, session_factory=factory, adapter_registry={"openai": provider})
    assert provider.calls == 1
