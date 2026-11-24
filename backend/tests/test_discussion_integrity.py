from dataclasses import replace

import pytest
from round_robin.database.models import (
    Attempt,
    BudgetOperation,
    EvidenceRecord,
    ReportVersion,
    Run,
    Step,
)
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.orchestration.policies import retryable_steps
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.research import service
from round_robin.research.discussion import discussion_issues, input_hash
from round_robin.research.service import execute_research, schedule_research
from round_robin.schemas.contributions import DiscussionObjection, ObjectionResponse
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import elapse_retry_wait, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_research_service import PageFixture, SearchFixture
from backend.tests.test_research_service import research_run as research_run  # noqa: PLC0414

pytest_plugins = ("backend.tests.test_artifacts_api",)


def corrupt(run):
    issue = {"id": "issue_duplicate", "concern": "Material obligation", "severity": "material", "origin": "structured", "source_step_id": "step_issue", "source_provider": "openai", "source_stage": "proposal", "events": []}
    run.configuration = {**run.configuration, "discussion_issues": [{**issue, "status": "open"}, {**issue, "status": "resolved"}]}


@pytest.mark.parametrize("cancelled", [False, True])
def test_duplicate_ledger_publishes_disclosed_partial_and_keeps_exposure(factory, cancelled):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.status = "cancel_requested" if cancelled else "running"
        run.stage = "proposals"
        corrupt(run)
        session.add(BudgetOperation(id="op_uncertain", run_id=run.id, owner_id=run.owner_id, kind="public_search", status="unknown", reserved_usd="0.42", metadata_json={"model_call": False}))
        advance_run(session, run)
        assert run.status == ("cancelled" if cancelled else "failed")
        assert run.stop_reason == ("user_cancellation" if cancelled else "discussion_integrity_failed")
        assert retryable_steps(session, run) == []
        assert len(run.configuration["discussion_issues"]) == 2
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        report = read_runtime_report(session, row)
        assert report["readiness"] is None and report["comparison"] == []
        assert "discussion ledger cannot be validated" in report["readiness_unavailable_reason"]
        assert report["usage"]["unknown_operation_ids"] == ["op_uncertain"]
        assert report["usage"]["conservative_exposure_usd"] == "0.42"
        assert report["assessment"]["unresolved_critical_defect"] is True


def test_invalid_ledger_blocks_worker_before_authorization(factory):
    class ProviderSpy(SimulatedProvider):
        def conservative_bound(self, request):
            raise AssertionError("must not authorize provider")
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
        corrupt(run)
    registry = {name: ProviderSpy(name) for name in ("openai", "anthropic", "google")}
    assert execute_step(step_id, session_factory=factory, adapter_registry=registry) == {"status": "policy_blocked", "code": "discussion_integrity_failed"}
    with factory() as session:
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None
        assert session.get(Run, run_id).status == "failed"


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("may_have_sent", [False, True])
def test_inflight_corrupt_ledger_fences_content_and_reconciles_usage(factory, cancelled, may_have_sent):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
    class CorruptingProvider(SimulatedProvider):
        def generate(self, request):
            with factory.begin() as session:
                run = lock_work_run(session, run_id)
                corrupt(run)
                if cancelled:
                    run.status = "cancel_requested"
            raise ProviderFailure("timeout", may_have_sent=may_have_sent, cost_usd=None)
    registry = {name: CorruptingProvider(name) for name in ("openai", "anthropic", "google")}
    assert execute_step(step_id, session_factory=factory, adapter_registry=registry)["status"] == "late_result_fenced"
    with factory() as session:
        run = session.get(Run, run_id)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        report = read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)))
        assert run.status == ("cancelled" if cancelled else "failed")
        assert run.stop_reason == ("user_cancellation" if cancelled else "discussion_integrity_failed")
        assert attempt.response_json["content_discarded"] is True
        assert report["usage"]["status"] == ("unknown" if may_have_sent else "reported")
        assert report["usage"]["unknown_operation_ids"] == ([attempt.id] if may_have_sent else [])


def test_invalid_ledger_blocks_public_research_before_authorization(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert schedule_research(session, run)
        corrupt(run)
    search = SearchFixture()
    assert not execute_research("run_local", session_factory=research_run, search=search, transport=PageFixture(), resolver=lambda _: ["93.184.216.34"])
    assert search.queries == []
    with research_run() as session:
        run = session.get(Run, "run_local")
        assert run.status == "failed" and run.stop_reason == "discussion_integrity_failed"
        assert not list(session.scalars(select(BudgetOperation).where(BudgetOperation.kind == "public_search")))


@pytest.mark.parametrize("after_send", [False, True])
def test_public_research_fences_corrupted_ledger_and_accounts_for_send(research_run, monkeypatch, after_send):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert schedule_research(session, run)
    if not after_send:
        original = service.append_event
        def corrupt_after_authorization(session, run, event_type, payload):
            result = original(session, run, event_type, payload)
            if event_type == "research.query_authorized":
                corrupt(run)
            return result
        monkeypatch.setattr(service, "append_event", corrupt_after_authorization)
    class CorruptingSearch(SearchFixture):
        def search(self, query, **kwargs):
            self.queries.append(query)
            with research_run.begin() as session:
                corrupt(session.get(Run, "run_local"))
            return []
    search = CorruptingSearch() if after_send else SearchFixture()
    assert not execute_research("run_local", session_factory=research_run, search=search, transport=PageFixture(), resolver=lambda _: ["93.184.216.34"])
    assert len(search.queries) == int(after_send)
    with research_run() as session:
        run = session.get(Run, "run_local")
        operation = session.scalar(select(BudgetOperation).where(BudgetOperation.kind == "public_search"))
        report = read_runtime_report(session, session.scalar(select(ReportVersion).where(ReportVersion.run_id == run.id)))
        assert run.stop_reason == "discussion_integrity_failed"
        assert operation.status == ("unknown" if after_send else "settled")
        assert report["usage"]["unknown_operation_ids"] == ([operation.id] if after_send else [])


def test_snapshot_shows_disclosed_partial_and_excludes_unreadable_evidence(artifacts):
    app, client, meeting_id, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.status = "cancel_requested"
        corrupt(run)
        session.add(EvidenceRecord(id="evidence_bad", run_id=run_id, meeting_id=meeting_id, owner_id=run.owner_id, content={"private": "DO_NOT_EXPOSE"}, sha256="0" * 64))
        session.flush()
        advance_run(session, run)
    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["brief"] is not None and body["evidence"] is None
    assert body["run"]["status"] == "cancelled" and body["run"]["stop_reason"] == "user_cancellation"
    assert body["report"]["comparison"] == [] and body["report"]["readiness"] is None
    assert body["retry_eligible"] is False
    assert "Stored evidence could not be validated" in str(body["report"]["main_reasons"])
    assert "DO_NOT_EXPOSE" not in response.text


def prepare_resolved_issue(factory, monkeypatch, *, severity="material", target_status="resolved"):
    class Scripted(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            if request.stage == "proposal" and self.provider == "openai":
                issue = DiscussionObjection(alias="new_objection_1", concern="Delivery exclusions need a response.", severity=severity, reference_ids=[request.evidence[0]["id"]])
                return replace(result, output=result.output.model_copy(update={"objections": [issue]}))
            if request.stage in {"synthesis", "audit"} and request.open_objections and target_status != "open" and not (request.stage == "audit" and target_status == "answered_pending_review"):
                responses = [ObjectionResponse(objection_id=issue["id"], disposition="answered" if request.stage == "synthesis" else "resolved", response="Reviewed the evidence", reference_ids=[request.evidence[0]["id"]]) for issue in request.open_objections if issue["concern"] == "Delivery exclusions need a response."]
                return replace(result, output=result.output.model_copy(update={"objection_responses": responses}))
            return result

    run_id = seed(factory)
    registry = {name: Scripted(name) for name in ("openai", "anthropic", "google")}
    for _ in range(30):
        with factory() as session:
            pending = list(session.scalars(select(Step.id).where(Step.run_id == run_id, Step.status == "pending")))
        with factory.begin() as session:
            advance_run(session, lock_work_run(session, run_id))
        for step_id in pending:
            elapse_retry_wait(factory, step_id)
            with factory() as session:
                audit = session.get(Step, step_id).stage == "audit"
            if audit:
                with monkeypatch.context() as patch:
                    patch.setattr("round_robin.worker.tasks.advance_run", lambda session, run: None)
                    execute_step(step_id, session_factory=factory, adapter_registry=registry)
                return run_id
            execute_step(step_id, session_factory=factory, adapter_registry=registry)
    raise AssertionError("audit did not become ready")


@pytest.mark.parametrize("history,expected", [("stale", "completed_with_limitations"), ("missing", "failed"), ("current", "completed")])
def test_only_current_evidence_backed_audit_can_clear_material_objection(factory, monkeypatch, history, expected):
    run_id = prepare_resolved_issue(factory, monkeypatch)
    monkeypatch.setattr("round_robin.orchestration.controller._reserve_correction", lambda session, run: False)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        issues = list(run.configuration["discussion_issues"])
        issue = next(item for item in issues if item["concern"] == "Delivery exclusions need a response.")
        assert issue["status"] == "resolved"
        if history == "stale":
            run.configuration = {**run.configuration, "resolved_brief": {**run.configuration["resolved_brief"], "goal": "An updated goal"}}
        if history == "missing":
            issues[issues.index(issue)] = {**issue, "events": []}
            run.configuration = {**run.configuration, "discussion_issues": issues}
        advance_run(session, run)
        assert run.status == expected
        saved = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        report = read_runtime_report(session, saved)
        if history == "missing":
            assert report["issues"] == [] and report["readiness"] is None
        else:
            saved_issue = next(item for item in report["issues"] if item["concern"] == "Delivery exclusions need a response.")
            assert saved_issue["status"] == ("resolved" if history == "current" else "open")


@pytest.mark.parametrize("case,expected", [
    ("current", "resolved"), ("no_prior_answer", "open"), ("same_provider", "open"),
    ("not_audit", "open"), ("no_references", "open"), ("mismatched_disposition", "open"),
    ("withdrawn_by_opener", "withdrawn"), ("withdrawn_by_other", "open"), ("withdrawn_no_refs", "open"),
])
def test_saved_resolution_requires_supported_history(factory, case, expected):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        digest = input_hash(run)
        answer = {"id": "answer_event", "step_id": "step_answer", "provider": "openai", "stage": "synthesis", "disposition": "answered", "response": "Evidence supports the response", "reference_ids": ["evidence_ref"], "input_hash": digest}
        review = {"id": "review_event", "step_id": "step_review", "provider": "anthropic", "stage": "audit", "disposition": "resolved", "response": "Independently reviewed", "reference_ids": ["evidence_ref"], "input_hash": digest}
        events = [answer, review]
        issue = {"id": "issue_one", "concern": "Delivery exclusion", "severity": "critical", "status": "resolved", "origin": "structured", "source_step_id": "step_open", "source_provider": "openai", "source_stage": "proposal", "events": events}
        if case == "no_prior_answer": issue["events"] = [review]
        if case == "same_provider": review["provider"] = answer["provider"]
        if case == "not_audit": review["stage"] = "synthesis"
        if case == "no_references": review["reference_ids"] = []
        if case == "mismatched_disposition": review["disposition"] = "answered"
        if case.startswith("withdrawn"):
            issue["status"] = "withdrawn"
            review["disposition"] = "withdrawn"
            review["provider"] = "openai" if case != "withdrawn_by_other" else "anthropic"
            if case == "withdrawn_no_refs": review["reference_ids"] = []
        run.configuration = {**run.configuration, "discussion_issues": [issue]}
        assert discussion_issues(run)[0]["status"] == expected
