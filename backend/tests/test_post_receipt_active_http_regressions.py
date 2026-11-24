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
    ReportVersion,
    Run,
    Step,
)
from round_robin.database.services import lock_policies
from round_robin.dispatcher.service import reconcile_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.orchestration.reporting import build_invalid_brief_report
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step
from sqlalchemy import select, text

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("corrupt_evidence", [False, True])
@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize(
    "raw",
    ['{"PRIVATE_STEP_SENTINEL":', "[" * 10000 + "0" + "]" * 10000],
    ids=["malformed", "deep"],
)
def test_post_send_corrupt_step_keeps_receipt_cancel_and_other_work(
    factory, monkeypatch, tmp_path, known, cancelled, raw, corrupt_evidence
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
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked, policy.limit_usd = False, "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        advance_run(session, lock_work_run(session, run_id))
        intake_id = session.scalar(
            select(Step.id).where(Step.run_id == run_id, Step.stage == "intake")
        )

    class Normal(SimulatedProvider):
        def conservative_bound(self, request):
            return Decimal("0.10")

    assert (
        execute_step(
            intake_id,
            session_factory=factory,
            adapter_registry={"openai": Normal("openai")},
        )["status"]
        == "persisted"
    )
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        proposal = session.scalar(
            select(Step)
            .where(
                Step.run_id == run_id,
                Step.stage == "proposal",
                Step.status == "pending",
            )
            .order_by(Step.id)
        )
        assert proposal is not None
        step_id, provider_name = proposal.id, proposal.provider
        previous_id = "report_previous"
        usage = {
            "simulated": False,
            "status": "reported",
            "actual_cost": "0",
            "conservative_exposure_usd": "0",
            "unknown_operation_ids": [],
            "request_count": 0,
            "currency": "USD",
        }
        previous = build_invalid_brief_report(
            run,
            [],
            status="failed",
            stop_reason="previous_partial",
            report_id=previous_id,
            version=1,
            usage=usage,
            evidence=[],
        )
        session.add(
            ReportVersion(
                id=previous_id,
                run_id=run_id,
                version=1,
                event_cutoff=previous["event_cutoff"],
                content=previous,
            )
        )
        run.report_version = 1
    healthy_id = seed(factory, suffix="healthy")
    app = create_app(
        str(factory.kw["bind"].url),
        settings=replace(Settings(), artifact_directory=tmp_path / "private"),
    )
    app.state.session_factory = factory
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with TestClient(app) as client:
        assert client.get(f"/api/runs/{run_id}/reports/1").status_code == 200

        class Corrupting(SimulatedProvider):
            def conservative_bound(self, request):
                return Decimal("0.40")

            def generate(self, request):
                super().generate(request)
                if cancelled:
                    with factory() as session:
                        version = session.get(Run, run_id).version
                    response = client.post(
                        f"/api/runs/{run_id}/cancel",
                        json={
                            "command_id": str(uuid4()),
                            "expected_run_version": version,
                        },
                    )
                    assert response.status_code == 200, response.text
                with factory.begin() as session:
                    session.execute(
                        text("UPDATE steps SET result_json=:raw WHERE id=:id"),
                        {"raw": raw, "id": step_id},
                    )
                    if corrupt_evidence:
                        evidence_id = session.scalar(
                            select(EvidenceRecord.id)
                            .where(EvidenceRecord.run_id == run_id)
                            .limit(1)
                        )
                        assert evidence_id
                        session.execute(
                            text("UPDATE evidence SET content=:raw WHERE id=:id"),
                            {"raw": '{"PRIVATE_EVIDENCE_SENTINEL":', "id": evidence_id},
                        )
                raise ProviderFailure(
                    "refusal",
                    may_have_sent=True,
                    cost_usd=Decimal("0.25") if known else None,
                )

        provider = Corrupting(provider_name)
        try:
            result = execute_step(
                step_id,
                session_factory=factory,
                adapter_registry={provider_name: provider},
            )
        except (ValueError, RecursionError):
            with factory() as session:
                attempt = session.scalar(
                    select(Attempt).where(Attempt.step_id == step_id)
                )
                op = session.get(BudgetOperation, attempt.id)
                assert attempt.completed_at is None and attempt.status == "authorized"
                assert op.status == "reserved" and op.settled_usd is None
            raise
        assert result["status"] in (
            "persisted",
            "late_result_fenced",
            "receipt_persisted_state_unavailable",
        ), result
        assert provider.calls == 1
        with factory() as session:
            attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
            op = session.get(BudgetOperation, attempt.id)
            assert attempt.completed_at is not None and op.status == (
                "settled" if known else "unknown"
            )
            assert op.settled_usd == ("0.25" if known else None)
        reconcile_once(session_factory=factory)
        prior = client.get(f"/api/runs/{run_id}/reports/1")
        assert prior.status_code == 200, prior.text
        healthy = client.get(f"/api/runs/{healthy_id}")
        assert healthy.status_code == 200 and healthy.json()["status"] == "running"
        run = client.get(f"/api/runs/{run_id}")
        assert run.status_code == 200 and "PRIVATE_STEP_SENTINEL" not in run.text
        assert run.json()["status"] == ("cancelled" if cancelled else "failed")
        assert run.json()["report_unavailable_reason"] == "saved_state_integrity_failed"
        if cancelled:
            assert run.json()["stop_reason"] == "user_cancellation"
        snapshot = client.get(f"/api/runs/{run_id}/snapshot")
        assert snapshot.status_code == 200, snapshot.text
        assert (
            snapshot.json()["report"] is None
            and snapshot.json()["retry_eligible"] is False
        )
        if corrupt_evidence:
            assert (
                snapshot.json()["evidence"] is None
                and "PRIVATE_EVIDENCE_SENTINEL" not in snapshot.text
            )
        assert (
            snapshot.json()["run"]["report_unavailable_reason"]
            == "saved_state_integrity_failed"
        )
        current = client.get(f"/api/runs/{run_id}/report")
        assert (
            current.status_code == 503
            and current.json()["error"]["code"] == "saved_state_integrity_failed"
        )
        calculation = client.get(f"/api/runs/{run_id}/calculations/calculation_fake")
        assert (
            calculation.status_code == 503
            and calculation.json()["error"]["code"] == "saved_state_integrity_failed"
        )
        with factory() as session:
            assert session.get(BudgetOperation, f"finish_{run_id}").status == "held"
        repeat_stop = client.post(
            f"/api/runs/{run_id}/cancel",
            json={
                "command_id": str(uuid4()),
                "expected_run_version": run.json()["version"],
            },
        )
        assert repeat_stop.status_code == 200, repeat_stop.text
        with factory() as session:
            assert session.get(BudgetOperation, f"finish_{run_id}").status == "held"
        execute_step(
            step_id, session_factory=factory, adapter_registry={provider_name: provider}
        )
        assert provider.calls == 1
