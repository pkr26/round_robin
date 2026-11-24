"""Scheduler row locking under immediate successful PostgreSQL delivery."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import importlib
import json

from fastapi.testclient import TestClient

from sqlalchemy import func, select

from backend.tests.integration.test_postgres_api import postgres_app
from backend.tests.test_audit_runtime_fixes import exercise_waiting_advances
from backend.tests.test_orchestration import seed
from backend.tests.test_api import command, create, start
from backend.tests.test_provider_configuration import synthetic_configuration
from round_robin.database.models import BudgetOperation, Outbox, ReportVersion, Run, Step, utcnow
from round_robin.database.services import enqueue, lock_policies
from round_robin.dispatcher.service import reconcile_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import operations_exposure
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


def test_immediate_broker_delivery_deduplicates_waiting_advances(postgres_app, monkeypatch):
    exercise_waiting_advances(postgres_app.state.session_factory, monkeypatch, synchronous_sender=True)


def test_competing_advance_producers_retain_one_durable_intention(postgres_app):
    factory = postgres_app.state.session_factory
    run_id = seed(factory)
    def produce(_):
        with factory.begin() as session:
            enqueue(session, lock_work_run(session, run_id))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(produce, range(12)))
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "advance")) == 1


def test_expired_retry_funds_do_not_block_a_fresh_accepted_run(postgres_app, tmp_path, monkeypatch):
    """The original API -> worker-loss -> period-rollover budget failure."""
    now = [utcnow()]
    for name in ("api.preflight", "api.events", "orchestration.admission", "orchestration.budget",
            "orchestration.configuration", "orchestration.controller", "orchestration.circuits",
            "orchestration.policies", "worker.tasks", "dispatcher.service", "database.services"):
        module = importlib.import_module("round_robin." + name)
        if hasattr(module, "utcnow"):
            monkeypatch.setattr(module, "utcnow", lambda: now[0])
    config = synthetic_configuration()
    config["aggregate_funding"].update(owner_daily_usd="0.60", owner_monthly_usd="0.60")
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(config))
    settings = replace(postgres_app.state.settings, provider_config_file=path, paid_access_enabled=True)
    postgres_app.state.settings = settings
    monkeypatch.setattr("round_robin.orchestration.configuration.settings", settings)
    factory = postgres_app.state.session_factory

    class LostSynthesis(SimulatedProvider):
        def conservative_bound(self, request):
            return Decimal("0.02")
        def generate(self, request):
            if request.stage == "synthesis":
                raise SystemExit("Synthetic worker loss after durable authorization")
            return replace(super().generate(request), cost_usd=Decimal("0.01"))

    registry = {provider: LostSynthesis(provider) for provider in ("openai", "anthropic", "google")}
    with TestClient(postgres_app) as client:
        meeting = create(client)
        with factory.begin() as session:
            for policy in lock_policies(session, "owner_local"):
                policy.spending_locked, policy.limit_usd = False, "10"
                policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1, mode="live"))
        assert quote.status_code == 200 and not quote.json()["blockers"], quote.text
        accepted = start(client, meeting, quote.json())
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["id"]
        crashed = False
        for _ in range(20):
            with factory.begin() as session:
                advance_run(session, lock_work_run(session, run_id))
            with factory() as session:
                pending = list(session.scalars(select(Step.id).where(Step.run_id == run_id, Step.status == "pending")))
            for step_id in pending:
                try:
                    execute_step(step_id, session_factory=factory, adapter_registry=registry)
                except SystemExit:
                    crashed = True
                    break
            if crashed:
                break
        assert crashed
        now[0] += timedelta(seconds=151)
        reconcile_once(session_factory=factory)
        with factory() as session:
            assert session.get(Run, run_id).status == "completed_with_limitations"
            assert session.get(BudgetOperation, f"finish_{run_id}").reserved_usd == "0.38"
            original_report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content
        now[0] += timedelta(days=2)
        reconcile_once(session_factory=factory)
        with factory() as session:
            assert session.get(BudgetOperation, f"finish_{run_id}").status == "released"
            assert operations_exposure(list(session.scalars(select(BudgetOperation))), now=now[0], period="daily") == Decimal("0.02")
            assert session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content == original_report
        config["expires_at"] = (now[0] + timedelta(hours=1)).isoformat()
        path.write_text(json.dumps(config))
        fresh_meeting = create(client)
        quote = client.post(f"/api/meetings/{fresh_meeting['id']}/preflight", json=command(expected_draft_revision=1, mode="live"))
        assert quote.status_code == 200 and not quote.json()["blockers"], quote.text
        fresh = start(client, fresh_meeting, quote.json())
        assert fresh.status_code == 202, fresh.text
        with factory.begin() as session:
            advance_run(session, lock_work_run(session, fresh.json()["id"]))
        with factory() as session:
            assert session.get(Run, fresh.json()["id"]).status == "running"
            assert operations_exposure(list(session.scalars(select(BudgetOperation))), now=now[0], period="daily") == Decimal("0.42")
