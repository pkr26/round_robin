from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from round_robin.api.auth import require_owner
from round_robin.api.preflight import run_configuration
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import (
    Attempt,
    BudgetOperation,
    Event,
    EvidenceRecord,
    Run,
    Step,
)
from round_robin.database.services import lock_policies
from round_robin.dispatcher.service import reconcile_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.contributions import DiscussionObjection
from round_robin.worker.tasks import execute_step
from sqlalchemy import select, text

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("usage", ["reported", "unknown"])
@pytest.mark.parametrize("cancel_during_call", [False, True])
def test_referenced_evidence_deep_corruption_after_synthesis_preserves_receipt(
    factory, tmp_path, monkeypatch, usage, cancel_during_call
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
    app = create_app(
        str(factory.kw["bind"].url),
        settings=replace(Settings(), artifact_directory=tmp_path / "private"),
    )
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    attacked = []
    with TestClient(app) as client:

        class Provider(SimulatedProvider):
            def conservative_bound(self, request):
                return Decimal("0.20") if request.stage == "synthesis" else Decimal(0)

            def generate(self, request):
                result = super().generate(request)
                if request.stage == "proposal" and self.provider == "openai":
                    with factory() as session:
                        allowed = set(request.allowed_reference_ids)
                        ref = next(
                            x
                            for x in session.scalars(
                                select(EvidenceRecord.id).where(
                                    EvidenceRecord.run_id == rid
                                )
                            )
                            if x in allowed
                        )
                    output = result.output.model_copy(
                        update={
                            "objections": [
                                DiscussionObjection(
                                    alias="new_objection_1",
                                    concern="Installation inclusion needs independent review.",
                                    severity="material",
                                    reference_ids=[ref],
                                )
                            ]
                        }
                    )
                    return replace(result, output=output)
                if request.stage == "synthesis":
                    issue = next(
                        i
                        for i in request.open_objections
                        if i["severity"] == "material"
                    )
                    ref = issue["reference_ids"][0]
                    with factory.begin() as session:
                        row = session.get(EvidenceRecord, ref)
                        assert row and row.run_id == rid
                        session.execute(
                            text("UPDATE evidence SET content=:raw WHERE id=:id"),
                            {"raw": "[" * 10000 + "0" + "]" * 10000, "id": ref},
                        )
                        version = session.get(Run, rid).version
                    attacked.append(ref)
                    if cancel_during_call:
                        response = client.post(
                            f"/api/runs/{rid}/cancel",
                            json={
                                "command_id": str(uuid4()),
                                "expected_run_version": version,
                            },
                        )
                        assert response.status_code == 200, response.text
                    raise ProviderFailure(
                        "refusal",
                        may_have_sent=True,
                        cost_usd=Decimal("0.25") if usage == "reported" else None,
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
                assert run.status == "running" and pending, (run.status, run.stage)
            for sid, provider, stage in pending:
                execute_step(sid, session_factory=factory, adapter_registry=registry)
                if stage == "synthesis":
                    break
            if attacked:
                break
        assert len(attacked) == 1
        if cancel_during_call:
            reconcile_once(session_factory=factory)
        response = client.get(f"/api/runs/{rid}/snapshot")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["run"]["status"] == (
            "cancelled" if cancel_during_call else "failed"
        )
        if cancel_during_call:
            assert body["run"]["stop_reason"] == "user_cancellation"
        assert body["evidence"] is None and body["report"] is not None
        assert body["report"]["evidence_ids"] == []
    with factory() as session:
        assert list(
            session.scalars(
                select(Event.id).where(
                    Event.run_id == rid,
                    Event.event_type == "discussion.obligation_recorded",
                )
            )
        )
        attempt = session.scalar(
            select(Attempt).where(Attempt.run_id == rid, Attempt.step_id == sid)
        )
        ledger = session.get(BudgetOperation, attempt.id)
        assert attempt.completed_at is not None and attempt.usage_certainty == usage
        assert ledger.status == ("settled" if usage == "reported" else "unknown")
        assert (
            Decimal(ledger.settled_usd) if ledger.settled_usd is not None else None
        ) == (Decimal("0.25") if usage == "reported" else None)
