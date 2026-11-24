from decimal import Decimal

import pytest
import round_robin.worker.tasks as worker
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, BudgetOperation, Run, Step
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from sqlalchemy import select, text

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("known", [True, False])
def test_step_rebound_after_scalar_check_rolls_back_receipt_and_fence(
    factory, tmp_path, monkeypatch, known
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
    run_id = seed(factory, config=config)
    foreign_id = seed(factory, suffix="other")
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(
            select(Step.id).where(Step.run_id == run_id, Step.stage == "intake")
        )
    sent = []
    changed = []
    original = worker.read_saved_step

    def rebind_before_hydration(session, query):
        if sent and not changed:
            changed.append(True)
            session.execute(
                text("UPDATE steps SET run_id=:foreign WHERE id=:id"),
                {"foreign": foreign_id, "id": step_id},
            )
        return original(session, query)

    monkeypatch.setattr(worker, "read_saved_step", rebind_before_hydration)

    class Provider(SimulatedProvider):
        def conservative_bound(self, request):
            return Decimal("0.40")

        def generate(self, request):
            super().generate(request)
            sent.append(True)
            raise ProviderFailure(
                "refusal",
                may_have_sent=True,
                cost_usd=Decimal("0.25") if known else None,
            )

    provider = Provider("openai")
    result = worker.execute_step(
        step_id, session_factory=factory, adapter_registry={"openai": provider}
    )
    assert (
        changed
        and provider.calls == 1
        and result["status"] == "persistence_unconfirmed"
    )
    with factory() as session:
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        assert attempt.status == "authorized" and attempt.completed_at is None
        assert ledger.status == "reserved" and ledger.settled_usd is None
        assert session.get(Step, step_id).run_id == run_id
        assert (
            session.get(Run, run_id).configuration.get("saved_state_unavailable")
            is not True
        )
