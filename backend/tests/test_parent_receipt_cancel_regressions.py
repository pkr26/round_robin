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
    ReportVersion,
    Run,
    Step,
)
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.worker.tasks import execute_step
from sqlalchemy import select, text

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


@pytest.mark.parametrize("case", ["raw_report", "forged_id", "raw_parent_brief"])
@pytest.mark.parametrize("usage", ["reported", "unknown"])
@pytest.mark.parametrize("cancel_during_call", [False, True])
def test_parent_damage_preserves_receipt_and_cancellation(
    factory, tmp_path, monkeypatch, case, usage, cancel_during_call
):
    factory.configure(
        info={
            "artifact_directory": tmp_path / "artifacts",
            "durability_control_directory": tmp_path / "control",
        }
    )
    parent = seed(factory)
    drain(factory)
    monkeypatch.setattr(
        "round_robin.orchestration.configuration.verify_frozen_configuration",
        lambda _: None,
    )
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    child = seed(factory, suffix="child", config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        parent_row, child_row = session.get(Run, parent), session.get(Run, child)
        child_row.parent_run_id = parent
        child_row.meeting_id = parent_row.meeting_id
        advance_run(session, lock_work_run(session, child))
        sid = session.scalar(
            select(Step.id).where(Step.run_id == child, Step.stage == "intake")
        )
        assert sid
        parent_report = session.scalar(
            select(ReportVersion).where(ReportVersion.run_id == parent)
        )
        parent_id = parent_report.id
        if case == "raw_report":
            session.execute(
                text("UPDATE report_versions SET content=:raw WHERE id=:id"),
                {"raw": '{"PRIVATE_PARENT_SENTINEL":', "id": parent_id},
            )
        elif case == "raw_parent_brief":
            session.execute(
                text("UPDATE runs SET input_snapshot=:raw WHERE id=:id"),
                {"raw": '{"PRIVATE_PARENT_SENTINEL":', "id": parent},
            )
        else:
            parent_report.content = {
                **parent_report.content,
                "id": "report_FORGED_PRIVATE_SENTINEL",
                "readiness": "ready_to_choose",
            }
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
                if cancel_during_call:
                    with factory() as s:
                        version = s.get(Run, child).version
                    response = client.post(
                        f"/api/runs/{child}/cancel",
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
        assert provider.calls == 1
        assert outcome["status"] in {"persisted", "late_result_fenced"}
        if cancel_during_call:
            from round_robin.dispatcher.service import reconcile_once

            reconcile_once(session_factory=factory)
    with factory() as session:
        run = session.get(Run, child)
        attempt = session.scalar(select(Attempt).where(Attempt.run_id == child))
        ledger = session.get(BudgetOperation, attempt.id)
        assert attempt.completed_at is not None
        assert ledger.status == ("settled" if usage == "reported" else "unknown")
        assert (
            Decimal(ledger.settled_usd) if ledger.settled_usd is not None else None
        ) == (Decimal("0.25") if usage == "reported" else None)
        assert attempt.usage_certainty == usage
        assert run.status == ("cancelled" if cancel_during_call else "failed")
        if cancel_during_call:
            assert run.stop_reason == "user_cancellation"
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == child))
        assert row
        report = read_runtime_report(session, row)
        assert report["parent_report_id"] == parent_id
        assert "PRIVATE_PARENT_SENTINEL" not in str(report)
        texts = (
            report["change_summary"]
            + report["main_reasons"]
            + report["readiness_reasons"]
        )
        assert any(
            ("parent" in x.lower() or "prior" in x.lower())
            and (
                "unavailable" in x.lower()
                or "not available" in x.lower()
                or "could not" in x.lower()
            )
            for x in texts
        )
        if case in {"raw_report", "forged_id"}:
            assert not any(
                "preferred-option result changed" in x
                or "Decision readiness changed" in x
                for x in report["change_summary"]
            )
    execute_step(sid, session_factory=factory, adapter_registry={"openai": provider})
    assert provider.calls == 1
