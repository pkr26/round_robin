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
from round_robin.worker.tasks import execute_step
from sqlalchemy import select, text

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize(
    "raw", ['{"PRIVATE_CHILD_SENTINEL":', "[" * 10000 + "0" + "]" * 10000]
)
@pytest.mark.parametrize("usage", ["reported", "unknown"])
@pytest.mark.parametrize("cancel_during_call", [False, True])
def test_unreadable_child_evidence_keeps_receipt_and_readable_partial(
    factory, tmp_path, monkeypatch, usage, cancel_during_call, raw
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
    app = create_app(
        str(factory.kw["bind"].url),
        settings=replace(Settings(), artifact_directory=tmp_path / "private"),
    )
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with TestClient(app) as client:

        class Provider(SimulatedProvider):
            def conservative_bound(self, request):
                return Decimal("0.40")

            def generate(self, request):
                super().generate(request)
                with factory.begin() as session:
                    run = session.get(Run, rid)
                    row = EvidenceRecord(
                        id="evidence_corrupt",
                        run_id=rid,
                        meeting_id=run.meeting_id,
                        owner_id=run.owner_id,
                        sha256="a" * 64,
                        content={},
                    )
                    session.add(row)
                    session.flush()
                    session.execute(
                        text("UPDATE evidence SET content=:raw WHERE id=:id"),
                        {"raw": raw, "id": row.id},
                    )
                    version = run.version
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

        provider = Provider("openai")
        outcome = execute_step(
            sid, session_factory=factory, adapter_registry={"openai": provider}
        )
        assert provider.calls == 1 and outcome["status"] in {
            "persisted",
            "late_result_fenced",
        }
        if cancel_during_call:
            reconcile_once(session_factory=factory)
        snapshot = client.get(f"/api/runs/{rid}/snapshot")
        assert snapshot.status_code == 200, snapshot.text
        body = snapshot.json()
        assert body["evidence"] is None
        assert body["run"]["status"] == (
            "cancelled" if cancel_during_call else "failed"
        )
        if cancel_during_call:
            assert body["run"]["stop_reason"] == "user_cancellation"
        assert body["report"] is not None
        assert "PRIVATE_CHILD_SENTINEL" not in snapshot.text
        report = client.get(f"/api/runs/{rid}/report")
        assert report.status_code == 200, report.text
        assert report.json()["evidence_ids"] == []
        assert "PRIVATE_CHILD_SENTINEL" not in report.text
    with factory() as session:
        attempt = session.scalar(select(Attempt).where(Attempt.run_id == rid))
        ledger = session.get(BudgetOperation, attempt.id)
        assert attempt.completed_at is not None and attempt.usage_certainty == usage
        assert ledger.status == ("settled" if usage == "reported" else "unknown")
        assert (
            Decimal(ledger.settled_usd) if ledger.settled_usd is not None else None
        ) == (Decimal("0.25") if usage == "reported" else None)
    execute_step(sid, session_factory=factory, adapter_registry={"openai": provider})
    assert provider.calls == 1
