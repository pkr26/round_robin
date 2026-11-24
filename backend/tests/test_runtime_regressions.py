"""Regression coverage for accepted briefs, protected finishing work and spending."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import hashlib
from threading import Event
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import drain, factory, seed
from backend.tests.test_provider_configuration import synthetic_configuration
from backend.tests.test_study_runner import mock_registry
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, BudgetOperation, ReportVersion, Run, Step, utcnow
from round_robin.database.services import lock_policies
from round_robin.fixtures import sample_brief
from round_robin.operations.recovery import purge_pending
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import BudgetDenied, _policy_checks, operations_exposure
from round_robin.orchestration.controller import advance_run, resolved_brief
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.briefs import DraftBrief
from round_robin.schemas.provider_configuration import ProviderConfiguration
from round_robin.worker.tasks import execute_step


def live_app(tmp_path, monkeypatch, *, aggregate="20", **limits):
    raw = synthetic_configuration()
    raw["limits"].update(owner_daily_usd=aggregate, owner_monthly_usd=aggregate, **limits)
    for key in raw["aggregate_funding"]:
        raw["aggregate_funding"][key] = aggregate
    for capability in raw["capabilities"].values():
        capability["context_window_tokens"] = 128000
    configuration = ProviderConfiguration.model_validate(raw)
    path = tmp_path / "providers.json"
    path.write_text(configuration.model_dump_json())
    settings = replace(Settings(), provider_config_file=path, paid_access_enabled=True,
                       execution_mode="live", artifact_directory=tmp_path / "artifacts")
    monkeypatch.setattr("round_robin.orchestration.configuration.settings", settings)
    app = create_app(f"sqlite:///{tmp_path / 'live.sqlite'}", settings=settings, initialize=True)
    with app.state.session_factory.begin() as session:
        for policy in lock_policies(session, "owner_local"):
            policy.spending_locked = False
            policy.limit_usd = aggregate
            policy.metadata_json = {"daily_usd": aggregate, "monthly_usd": aggregate}
    return app, configuration


def start_live(client):
    def command(**values):
        return {"command_id": str(uuid4()), **values}
    created = client.post("/api/meetings", json=command(brief=sample_brief().model_dump(mode="json")))
    assert created.status_code == 201, created.text
    meeting = created.json()
    quote = client.post(f"/api/meetings/{meeting['id']}/preflight",
                        json=command(expected_draft_revision=1, mode="live"))
    assert quote.status_code == 200, quote.text
    assert not quote.json()["blockers"]
    started = client.post(f"/api/meetings/{meeting['id']}/runs",
                          json=command(expected_draft_revision=1, preflight_id=quote.json()["id"]))
    assert started.status_code == 202, started.text
    return meeting["id"], started.json()["id"]


@pytest.mark.parametrize("in_flight", [False, True])
def test_meeting_deletion_releases_unused_finish_and_retains_unknown_usage(tmp_path, monkeypatch, in_flight):
    app, configuration = live_app(tmp_path, monkeypatch, aggregate="1")
    session_factory = app.state.session_factory
    with TestClient(app) as client:
        meeting_id, run_id = start_live(client)
        with session_factory.begin() as session:
            advance_run(session, lock_work_run(session, run_id))
            step_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
        def delete():
            response = client.request("DELETE", f"/api/meetings/{meeting_id}",
                                      json={"command_id": str(uuid4())})
            assert response.status_code == 200, response.text
        if in_flight:
            registry, _ = mock_registry(configuration)
            original = registry["openai"]
            class LostAfterDeletion:
                model = original.model
                def conservative_bound(self, request):
                    return original.conservative_bound(request)
                def generate(self, request):
                    delete()
                    raise ProviderFailure("timeout", may_have_sent=True)
            result = execute_step(step_id, session_factory=session_factory,
                                  adapter_registry={"openai": LostAfterDeletion()})
            assert result["status"] == "late_result_fenced"
        else:
            delete()
        purge_pending(session_factory, app.state.settings.artifact_directory,
                      tmp_path / "deletion-manifest.jsonl")
        with session_factory() as session:
            hold = session.get(BudgetOperation, f"finish_{run_id}")
            assert (hold.status, hold.reserved_usd) == ("released", "0")
            operations = list(session.scalars(select(BudgetOperation)))
            unknown = [operation for operation in operations if operation.status == "unknown"]
            assert len(unknown) == int(in_flight)
            if in_flight:
                assert operations_exposure(operations) == Decimal(unknown[0].reserved_usd) > 0
                attempt = session.scalar(select(Attempt))
                assert attempt.usage_certainty == "unknown"
        _, next_id = start_live(client)
        with session_factory.begin() as session:
            advance_run(session, lock_work_run(session, next_id))
            assert session.get(Run, next_id).status == "running"


def test_report_ids_are_unique_for_short_alias_equal_to_long_alias_digest(factory):
    body = sample_brief().model_dump(mode="json")
    long_id = "a" * 96
    body["options"][0]["id"] = long_id
    body["options"][1]["id"] = hashlib.sha256(long_id.encode()).hexdigest()
    run_id = seed(factory, brief=DraftBrief.model_validate(body))
    drain(factory)
    with factory() as session:
        run = session.get(Run, run_id)
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content
        assert run.status == "completed"
        calculations = {item["id"] for item in report["calculations"]}
        assert len(calculations) == len(body["options"])
        assert {item["calculation_id"] for item in report["comparison"]} == calculations
        assert {item["option_id"] for item in report["comparison"]} == {item["id"] for item in body["options"]}
        assert len({item["id"] for item in report["essential_dependencies"]}) == len(report["essential_dependencies"])


@pytest.mark.parametrize("identity", ["requirement_quote_validity", "requirement_server_deadline"])
def test_quote_validity_does_not_replace_explicit_completion_deadline(factory, identity):
    body = sample_brief().model_dump(mode="json")
    body["deadline"] = "2027-02-01"
    body["hard_requirements"] = [{"id": identity, "description": "Supplier quote must remain valid through 2027-02-01.",
                                  "origin": "user_supplied", "metric": "manual", "operator": "manual", "threshold": None}]
    resolved, _ = resolved_brief(body)
    repeated, added_again = resolved_brief(resolved)
    assert repeated == resolved and added_again == []
    completion = next(item for item in resolved["hard_requirements"] if item["description"].startswith("Complete by 2027-02-01;"))
    assert completion["id"] != identity
    run_id = seed(factory, brief=DraftBrief.model_validate(body))
    drain(factory)
    with factory() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content
        assert {identity, completion["id"]} <= set(report["hard_requirement_ids"])
        assert all(completion["id"] in {result["requirement_id"] for result in item["constraint_results"]}
                   for item in report["comparison"])


def test_one_call_profile_settles_failed_proposal_and_completes_independent_review(tmp_path, monkeypatch):
    app, configuration = live_app(tmp_path, monkeypatch, max_calls_per_step=1)
    registry, _ = mock_registry(configuration)
    original = registry["openai"]
    class FailedProposal:
        model = original.model
        def conservative_bound(self, request):
            return original.conservative_bound(request)
        def generate(self, request):
            if request.stage == "proposal":
                raise ProviderFailure("transient", retryable=True, may_have_sent=False)
            return original.generate(request)
    registry["openai"] = FailedProposal()
    with TestClient(app) as client:
        _, run_id = start_live(client)
        drain(app.state.session_factory, registry)
        report = client.get(f"/api/runs/{run_id}/report").json()
        assert report["assessment"]["synthesis_valid"] and report["assessment"]["audit_succeeded"]
        assert report["execution_status"] == "completed_with_limitations"
        with app.state.session_factory() as session:
            steps = list(session.scalars(select(Step).where(Step.run_id == run_id)))
            assert all(step.calls <= 1 for step in steps)
            failed = next(step for step in steps if step.stage == "proposal" and step.provider == "openai")
            assert (failed.status, failed.error_code, failed.infrastructure_claims) == ("failed", "transient", 1)


def test_correction_failure_cannot_renew_logical_role_call_limit(factory):
    class CorrectionFailure(SimulatedProvider):
        def __init__(self, provider):
            super().__init__(provider)
            self.syntheses = 0
        def generate(self, request):
            if request.stage == "synthesis":
                self.syntheses += 1
                if self.syntheses == 2:
                    raise ProviderFailure("timeout", retryable=True, may_have_sent=False)
            result = super().generate(request)
            if request.stage == "audit":
                return replace(result, output=result.output.model_copy(update={"audit_passed": False, "critical_defects": ["Correct the supplied schedule evidence."]}))
            return result
    run_id = seed(factory)
    drain(factory, {provider: CorrectionFailure(provider) for provider in ("openai", "anthropic", "google")})
    with factory() as session:
        corrections = list(session.scalars(select(Step).where(Step.run_id == run_id, Step.stage == "synthesis")))
        assert sum(step.calls for step in corrections) == 2
        assert sum(step.infrastructure_claims for step in corrections) == 2
        assert session.get(Run, run_id).stop_reason == "synthesis_unavailable"


def test_one_call_moderator_cannot_fallback_after_its_authorized_call(tmp_path, monkeypatch):
    app, configuration = live_app(tmp_path, monkeypatch, max_calls_per_step=1)
    registry, requests = mock_registry(configuration)
    original = registry["openai"]
    class UnavailableIntake:
        model = original.model
        def conservative_bound(self, request):
            return original.conservative_bound(request)
        def generate(self, request):
            raise ProviderFailure("authentication", may_have_sent=False)
    registry["openai"] = UnavailableIntake()
    with TestClient(app) as client:
        _, run_id = start_live(client)
        drain(app.state.session_factory, registry)
        with app.state.session_factory() as session:
            run = session.get(Run, run_id)
            steps = list(session.scalars(select(Step).where(Step.run_id == run_id)))
            assert run.stop_reason == "intake_unavailable" and run.status == "failed"
            assert len(steps) == 1
            assert (steps[0].provider, steps[0].calls, steps[0].infrastructure_claims) == ("openai", 1, 1)
        assert not requests


def test_five_request_profile_preserves_both_final_requests(tmp_path, monkeypatch):
    app, configuration = live_app(tmp_path, monkeypatch, max_requests=5)
    registry, requests = mock_registry(configuration)
    with TestClient(app) as client:
        _, run_id = start_live(client)
        drain(app.state.session_factory, registry)
        report = client.get(f"/api/runs/{run_id}/report").json()
        assert report["execution_status"] == "completed_with_limitations"
        assert report["assessment"]["synthesis_valid"] and report["assessment"]["audit_succeeded"]
        assert report["recommendation"] is not None
        assert len(requests) == report["usage"]["request_count"] == 5
        with app.state.session_factory() as session:
            steps = list(session.scalars(select(Step).where(Step.run_id == run_id)))
            assert sum(step.stage == "proposal" and step.status == "succeeded" for step in steps) == 2
            skipped = next(step for step in steps if step.stage == "proposal" and step.status == "skipped")
            assert (skipped.calls, skipped.error_code) == (0, "finish_call_reserve")
            assert session.get(BudgetOperation, f"finish_{run_id}").status == "released"


def test_proposal_reserve_denial_does_not_fence_other_in_flight_proposals(tmp_path, monkeypatch):
    app, configuration = live_app(tmp_path, monkeypatch, max_requests=5)
    session_factory = app.state.session_factory
    registry, requests = mock_registry(configuration)
    with TestClient(app) as client:
        _, run_id = start_live(client)
        with session_factory.begin() as session:
            advance_run(session, lock_work_run(session, run_id))
            intake_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
        execute_step(intake_id, session_factory=session_factory, adapter_registry=registry)
        with session_factory() as session:
            proposals = {step.provider: step.id for step in session.scalars(select(Step).where(Step.run_id == run_id, Step.stage == "proposal"))}
        started = {provider: Event() for provider in ("openai", "anthropic")}
        release = Event()
        def blocking(original, provider):
            class DelayedProposal:
                model = original.model
                def conservative_bound(self, request):
                    return original.conservative_bound(request)
                def generate(self, request):
                    if request.stage == "proposal":
                        started[provider].set()
                        if not release.wait(10):
                            raise AssertionError("The proposal test was not released")
                    return original.generate(request)
            return DelayedProposal()
        registry.update({provider: blocking(registry[provider], provider) for provider in started})
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(execute_step, proposals[provider], session_factory=session_factory, adapter_registry=registry) for provider in started]
            try:
                assert all(event.wait(5) for event in started.values())
                outcome = execute_step(proposals["google"], session_factory=session_factory, adapter_registry=registry)
                assert outcome == {"status": "policy_skipped", "code": "finish_call_reserve"}
                with session_factory() as session:
                    assert session.get(Run, run_id).status == "running"
                    assert all(session.get(Step, proposals[provider]).status == "authorized" for provider in started)
            finally:
                release.set()
            assert all(future.result(timeout=10)["status"] == "persisted" for future in futures)
        drain(session_factory, registry)
        report = client.get(f"/api/runs/{run_id}/report").json()
        assert report["assessment"]["synthesis_valid"] and report["assessment"]["audit_succeeded"]
        assert report["execution_status"] == "completed_with_limitations"
        assert report["usage"]["request_count"] == len(requests) == 5


@pytest.mark.parametrize("period,aggregate,spent", [("daily", "20", "10"), ("monthly", "100", "50")])
def test_explicit_higher_deployment_caps_are_honored_and_current_caps_can_lower_them(tmp_path, monkeypatch, period, aggregate, spent):
    app, _ = live_app(tmp_path, monkeypatch, aggregate=aggregate)
    session_factory = app.state.session_factory
    with session_factory.begin() as session:
        session.add(BudgetOperation(id="historical_settled_usage", owner_id="owner_local", run_id=None, kind="model",
                                    status="settled", reserved_usd=spent, settled_usd=spent, authorized_at=utcnow(), metadata_json={}))
    with TestClient(app) as client:
        _, run_id = start_live(client)
        with session_factory.begin() as session:
            advance_run(session, lock_work_run(session, run_id))
            run = session.get(Run, run_id)
            assert run.status == "running"
            assert session.get(BudgetOperation, f"finish_{run_id}").reserved_usd == "0.40"
            deployment, _ = lock_policies(session, run.owner_id)
            deployment.metadata_json = {**deployment.metadata_json, f"{period}_usd": spent}
            with pytest.raises(BudgetDenied, match=f"deployment_{period}_budget_exhausted"):
                _policy_checks(session, run, Decimal("0"))
