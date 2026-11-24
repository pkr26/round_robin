from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.api.auth import require_owner
from round_robin.api.preflight import run_configuration
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, BudgetOperation, ReportVersion, Run, Step
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


@pytest.mark.parametrize("usage", ["known", "unknown"])
@pytest.mark.parametrize("cancel_during_call", [False, True])
def test_sent_text_usage_survives_malformed_sibling_and_optional_cancellation(factory, tmp_path, monkeypatch, usage, cancel_during_call):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
    app = create_app(str(factory.kw["bind"].url), settings=replace(Settings(), artifact_directory=tmp_path / "private"))
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    cancellations = []

    with TestClient(app) as client:
        class Provider(SimulatedProvider):
            def conservative_bound(self, request):
                return Decimal("0.40")

            def generate(self, request):
                result = super().generate(request)
                with factory.begin() as session:
                    current = session.get(Run, run_id)
                    version = current.version
                    session.add(BudgetOperation(id="malformed_sibling", run_id=run_id, owner_id=current.owner_id,
                        kind="model", status="unknown", reserved_usd="0.60", metadata_json=[]))
                if cancel_during_call:
                    payload = {"command_id": str(uuid4()), "expected_run_version": version}
                    response = client.post(f"/api/runs/{run_id}/cancel", json=payload)
                    cancellations.append((payload, response.status_code, response.json()))
                if usage == "unknown":
                    raise ProviderFailure("timeout", may_have_sent=True)
                return replace(result, cost_usd=Decimal("0.75"), simulated=False, provider_request_id="synthetic_request")

        adapter = Provider("openai")
        result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": adapter})
        assert adapter.calls == 1
        if cancel_during_call:
            payload, status, body = cancellations[0]
            assert status == 200 and body["status"] == "cancel_requested"
            assert client.post(f"/api/runs/{run_id}/cancel", json=payload).json() == body
        assert result["status"] in {"late_result_fenced", "persisted"}

    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == ("cancelled" if cancel_during_call else "failed")
        assert run.configuration.get("ledger_integrity_failed") is True
        assert run.report_version == 0 and session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)) is None
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        assert attempt.completed_at is not None
        assert ledger.status == ("settled" if usage == "known" else "unknown")
        assert (Decimal(ledger.settled_usd) if ledger.settled_usd is not None else None) == (Decimal("0.75") if usage == "known" else None)
        assert ledger.reserved_usd == "0.40"
        assert attempt.response_json is None or attempt.response_json.get("content_discarded") is True
        assert session.get(BudgetOperation, "malformed_sibling").metadata_json == []
        assert session.get(BudgetOperation, f"finish_{run_id}").status == "held"
    execute_step(step_id, session_factory=factory, adapter_registry={"openai": adapter})
    assert adapter.calls == 1
