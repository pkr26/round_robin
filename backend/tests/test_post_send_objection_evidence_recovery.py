from dataclasses import replace
from decimal import Decimal

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
from round_robin.schemas.contributions import DiscussionObjection
from round_robin.worker.tasks import execute_step
from sqlalchemy import select, text

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize(
    "raw",
    ['{"PRIVATE_SENTINEL":', "[" * 10000 + "0" + "]" * 10000],
    ids=["malformed", "deep"],
)
def test_corrupt_evidence_referenced_by_committed_objection_after_send_preserves_receipt(
    factory, monkeypatch, tmp_path, known, raw
):
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

    class OpeningProvider(SimulatedProvider):
        def conservative_bound(self, request):
            return Decimal("0.10")

        def generate(self, request):
            result = super().generate(request)
            if request.stage == "proposal" and self.provider == "openai":
                objection = DiscussionObjection(
                    alias="new_objection_1",
                    concern="Confirm the source exclusions.",
                    severity="material",
                    reference_ids=[request.evidence[0]["id"]],
                )
                return replace(
                    result,
                    output=result.output.model_copy(update={"objections": [objection]}),
                )
            return result

    registry = {
        name: OpeningProvider(name) for name in ("openai", "anthropic", "google")
    }
    for _ in range(20):
        with factory.begin() as session:
            run = lock_work_run(session, run_id)
            issues = run.configuration.get("discussion_issues", [])
            if issues:
                evidence_id = issues[0]["reference_ids"][0]
                break
            advance_run(session, run)
            pending = list(
                session.scalars(
                    select(Step).where(Step.run_id == run_id, Step.status == "pending")
                )
            )
            pending.sort(
                key=lambda step: (step.stage != "proposal", step.provider != "openai")
            )
            step_id = pending[0].id if pending else None
        if step_id:
            assert (
                execute_step(
                    step_id, session_factory=factory, adapter_registry=registry
                )["status"]
                == "persisted"
            )
    else:
        raise AssertionError("source-backed objection never committed")

    with factory() as session:
        assert (
            session.scalar(
                select(Event.id).where(
                    Event.run_id == run_id,
                    Event.event_type == "discussion.obligation_recorded",
                )
            )
            is not None
        )
        pending = session.scalar(
            select(Step)
            .where(Step.run_id == run_id, Step.status == "pending")
            .order_by(Step.id)
        )
        assert pending is not None
        step_id, name = pending.id, pending.provider

    class CorruptingProvider(SimulatedProvider):
        def conservative_bound(self, request):
            return Decimal("0.10")

        def generate(self, request):
            super().generate(request)
            with factory.begin() as session:
                session.execute(
                    text("UPDATE evidence SET content=:raw WHERE id=:id"),
                    {"raw": raw, "id": evidence_id},
                )
            raise ProviderFailure(
                "refusal",
                may_have_sent=True,
                cost_usd=Decimal("0.07") if known else None,
            )

    corrupting = CorruptingProvider(name)
    result = execute_step(
        step_id, session_factory=factory, adapter_registry={name: corrupting}
    )
    assert result["status"] in ("persisted", "late_result_fenced"), result
    assert corrupting.calls == 1
    assert (
        execute_step(
            step_id, session_factory=factory, adapter_registry={name: corrupting}
        )["status"]
        == "settled"
    )
    assert corrupting.calls == 1
    with factory() as session:
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        op = session.get(BudgetOperation, attempt.id)
        assert op.status == ("settled" if known else "unknown")
        assert op.settled_usd == ("0.07" if known else None)
        run = session.get(Run, run_id)
        assert run.status == "failed" and run.stop_reason in (
            "evidence_integrity_failed",
            "discussion_integrity_failed",
        )
        report = read_runtime_report(
            session,
            session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)),
        )
        assert report["comparison"] == [] and report["evidence_ids"] == []
        assert report["usage"]["unknown_operation_ids"] == (
            [] if known else [attempt.id]
        )
        assert "PRIVATE_SENTINEL" not in str(report)
    app = create_app(
        str(factory.kw["bind"].url),
        settings=replace(Settings(), artifact_directory=tmp_path / "private"),
        initialize=False,
    )
    app.state.session_factory = factory
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/snapshot")
        assert response.status_code == 200, response.text
        assert (
            response.json()["evidence"] is None
            and response.json()["report"]["comparison"] == []
        )
        assert "PRIVATE_SENTINEL" not in response.text
