"""Adversarial evidence and discussion invariants, using only local synthetic data."""
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from backend.tests.test_evidence_regressions import complete_brief
from backend.tests.test_orchestration import drain, factory, seed
from backend.tests.test_research_service import research_run
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.database.models import EvidenceRecord, ReportVersion, Run, Step
from round_robin.orchestration.reporting import build_report
from round_robin.research.claims import grounded_claim_aliases
from round_robin.research.discussion import assessment_target, reduce_discussion
from round_robin.schemas.discussion import ScopedAssessment
from round_robin.research.fetcher import FetchResponse, RetrievedSource, exact_passages
from round_robin.research.search import SearchHit
from round_robin.research.service import execute_research, schedule_research


def live_report(*, audit_result="pass", audit_refs=None, synthesis_result="pass", synthesis_refs=None, verbose=False, proposal_critical=False, with_attestations=False, quote_valid_until=None, published_at=None):
    brief = complete_brief(valid_until=quote_valid_until).model_dump(mode="json")
    brief["options"] = brief["options"][:2]
    brief["hard_requirements"] = [{"id": "requirement_feature", "description": "Required feature", "metric": "manual", "operator": "manual", "origin": "user_supplied"}]
    for option in brief["options"]:
        for component in option["cost_components"]:
            component["source_ref"] = "evidence_cost"
    run = SimpleNamespace(id="run_probe", owner_id="owner_probe", input_snapshot=brief, created_at=datetime.now(timezone.utc),
        event_sequence=0, configuration={"mode": "live", "resolved_brief": brief, "evidence_prepared": True})
    def output(result="pass", refs=None, **fields):
        return {"summary": "Conditional choice using synthetic supplied inputs", "recommendation_option_id": brief["options"][0]["id"],
            "constraints": [{"option_id": item["id"], "requirement_id": "requirement_feature", "result": result,
                "reason": f"Synthetic {result} assessment", "basis_refs": ["evidence_positive"] if refs is None else refs} for item in brief["options"]],
            "audit_passed": True, "critical_defects": [], "dissent": [], **fields}
    def step(identity, stage, provider, value):
        return SimpleNamespace(id=identity, status="succeeded", stage=stage, provider=provider, error_code=None, result_json={"output": value})
    steps = [step("proposal_one", "proposal", "openai", output(dissent=[f"Earlier minor concern {i}" for i in range(12)] if verbose else [], critical_defects=["Unresolved proposal blocker"] if proposal_critical else [])),
        step("proposal_two", "proposal", "google", output(dissent=[f"Other minor concern {i}" for i in range(12)] if verbose else [])),
        step("synthesis_one", "synthesis", "openai", output(synthesis_result, synthesis_refs)),
        step("audit_one", "audit", "anthropic", output(audit_result, audit_refs, critical_defects=["Final auditor critical issue"] if verbose else []))]
    usage = {"simulated": False, "status": "reported", "actual_cost": "0", "conservative_exposure_usd": "0", "unknown_operation_ids": [], "request_count": 4}
    evidence = [{"id": "evidence_cost", "verification_status": "user_supplied", "sha256": "a" * 64}, {"id": "evidence_positive", "verification_status": "not_checked", "sha256": "b" * 64}]
    if with_attestations:
        receipts = []
        for option in brief["options"]:
            for target, requirement, refs, outcome in (("option_costs", None, ["evidence_cost"], "supported"), ("requirement", "requirement_feature", ["evidence_positive"], "pass")):
                receipts.append(ScopedAssessment(id=f"assessment_{len(receipts)}", target_type=target, option_id=option["id"], requirement_id=requirement,
                    target_hash=assessment_target(run, target, option["id"], requirement), reference_ids=refs,
                    evidence_hashes={ref: next(item["sha256"] for item in evidence if item["id"] == ref) for ref in refs}, result=outcome,
                    attestation="I attest the exact supplied scope and source passages in this synthetic fixture.", actor_id=run.owner_id,
                    run_id=run.id, recorded_at=datetime.now(timezone.utc)).model_dump(mode="json"))
        run.configuration["evidence_assessments"] = receipts
    report = build_report(run, steps, status="completed", stop_reason="test", report_id="report_probe", version=1, usage=usage, evidence=evidence, published_at=published_at)
    return report


def test_scoped_explicit_attestations_enable_qualified_readiness_without_source_promotion():
    assert live_report()["readiness"] == "conditional"
    accepted = live_report(with_attestations=True)
    assert accepted["readiness"] == "ready_to_choose"
    assert accepted["evidence_assessments"] and "not independent factual certification" in accepted["readiness_dimensions"]["basis_scope"]
    disputed = live_report(with_attestations=True, audit_result="fail", audit_refs=["evidence_positive"])
    assert disputed["readiness"] == "conditional"
    assert all(item["constraint_results"][0]["disputed"] for item in disputed["comparison"])


def test_new_publications_expire_cost_support_at_local_midnight():
    expiry = date(2026, 11, 27)
    def publish(hour, minute):
        return live_report(with_attestations=True, quote_valid_until=expiry,
                           published_at=datetime(2026, 11, 28, hour, minute, tzinfo=timezone.utc))

    fresh = publish(6, 59)
    stale = publish(7, 0)
    assert fresh["valuation_timezone"] == stale["valuation_timezone"] == "America/Phoenix"
    assert fresh["evidence_valid_through"] == stale["evidence_valid_through"] == expiry.isoformat()
    assert fresh["valuation_date"] == expiry.isoformat() and fresh["readiness"] == "ready_to_choose"
    assert stale["valuation_date"] == "2026-11-28" and stale["readiness"] != "ready_to_choose"
    assert fresh["comparison"][0]["total"] == stale["comparison"][0]["known_subtotal"] == "1600.00"
    assert stale["comparison"][0]["total"] is None
    assert stale["recommendation"] is None
    assert {item["target_type"] for item in stale["evidence_assessments"]} == {"requirement"}


def test_expired_model_citations_cannot_establish_or_dispute_manual_failures():
    fields = dict(quote_valid_until=date(2026, 11, 27), published_at=datetime(2026, 11, 28, 7, tzinfo=timezone.utc),
                  audit_result="fail", audit_refs=["evidence_cost"])
    expired = live_report(synthesis_result="fail", synthesis_refs=["evidence_cost"], **fields)
    assert expired["readiness"] == "insufficient_evidence"
    assert all(item["constraint_results"][0]["result"] == "unknown" for item in expired["comparison"])
    current = live_report(synthesis_result="pass", synthesis_refs=["evidence_positive"], **fields)
    assert all(item["constraint_results"][0]["result"] == "pass" and not item["constraint_results"][0]["disputed"]
               for item in current["comparison"])


def test_unsupported_audit_disagreement_does_not_manufacture_infeasibility():
    report = live_report(audit_result="fail", audit_refs=[])
    assert report["readiness"] != "no_feasible_option"
    for option in report["comparison"]:
        result = option["constraint_results"][0]
        assert result["result"] == "unknown" and result["disputed"]
        assert result["basis_refs"] == ["evidence_positive"] and result["counter_basis_refs"] == []
        assert "anthropic" in result["assessor"]


def test_critical_obligations_survive_dissent_bounds_and_later_omission():
    report = live_report(verbose=True, proposal_critical=True)
    assert "Final auditor critical issue" in report["dissent"]
    assert "Unresolved proposal blocker" in report["dissent"]
    assert report["dissent_overflow_count"] > 0
    assert len(report["issues"]) > 20
    assert report["assessment"]["unresolved_critical_defect"]
    assert not report["readiness_dimensions"]["material_objections_resolved"]


@pytest.mark.parametrize("negative", ["contradicted", "mixed", "not_checked"])
def test_same_response_and_prior_claims_cannot_launder_negative_status(negative):
    first = {"alias": "new_claim_1", "id": "claim_previous", "evidence_status": negative, "reference_ids": ["source_one"]}
    second = {"alias": "new_claim_2", "evidence_status": "supported", "reference_ids": ["new_claim_1"]}
    assert "new_claim_2" not in grounded_claim_aliases([first, second], support_reference_ids={"source_one"})
    second["reference_ids"] = ["claim_previous"]
    assert "new_claim_2" not in grounded_claim_aliases([second], support_reference_ids={"source_one"}, prior_claims=[first])


def test_support_bases_require_every_premise_but_accept_an_independent_alternative():
    value = {"alias": "new_claim_1", "evidence_status": "supported", "reference_ids": [],
        "support_bases": [{"reference_ids": ["source_one", "source_missing"]}]}
    assert not grounded_claim_aliases([value], support_reference_ids={"source_one"})
    value["support_bases"].append({"reference_ids": ["source_other"]})
    assert grounded_claim_aliases([value], support_reference_ids={"source_one", "source_other"}) == {"new_claim_1"}
    value["refuting_reference_ids"] = ["source_refutation"]
    assert not grounded_claim_aliases([value], support_reference_ids={"source_one", "source_other", "source_refutation"})


def test_late_exclusion_is_selected_and_exact_offsets_and_omitted_coverage_are_preserved():
    text = "Navigation and generic introduction. " * 220 + "Installation is not included. A separate installation charge applies."
    source = RetrievedSource("https://example.com/terms", text, hashlib.sha256(text.encode()).hexdigest(), datetime.now(timezone.utc).isoformat(), "origin_test")
    passages = exact_passages(source, maximum=2, terms=["installation total cost"])
    assert any("not included" in item["text"] for item in passages)
    for item in passages:
        assert item["text"] == text[item["selected_start"]:item["selected_end"]]
        assert hashlib.sha256(item["text"].encode()).hexdigest() == item["sha256"]
        assert item["source_content_hash"] == source.content_hash
        assert item["uninspected_characters"] > 0


def test_query_page_and_packet_budgets_preserve_later_objectives(research_run):
    class Search:
        count = 0
        def search(self, query, **kwargs):
            self.count += 1
            return [SearchHit("Synthetic terms", f"https://example.com/query{self.count}/source{i}") for i in range(8)]
    class Pages:
        def get(self, target, **kwargs):
            return FetchResponse(200, {"content-type": "text/plain"}, ("Exact source " + target.url).encode())
    with research_run.begin() as session:
        schedule_research(session, session.get(Run, "run_local"))
    search = Search()
    assert execute_research("run_local", session_factory=research_run, search=search, transport=Pages(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        records = list(session.scalars(select(EvidenceRecord)))
        summary = session.get(Run, "run_local").configuration["research_summary"]
    assert search.count == 3 and summary["pages"] == 8
    assert all(any(f"/query{index}/" in row.content["url"] for row in records) for index in (1, 2, 3))
    assert len(records) <= 24 and all(item["sources_retrieved"] > 0 for item in summary["objectives"])


def test_full_evidence_packet_prevents_unusable_search_spending(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        for index in range(24):
            session.add(EvidenceRecord(id=f"evidence_{index}", run_id=run.id, meeting_id=run.meeting_id, owner_id=run.owner_id, sha256="a" * 64, content={}))
        schedule_research(session, run)
    class Search:
        def search(self, *args, **kwargs):
            raise AssertionError("No search can yield inspectable packet content")
    assert execute_research("run_local", session_factory=research_run, search=Search())
    with research_run() as session:
        summary = session.get(Run, "run_local").configuration["research_summary"]
        assert summary["queries"] == 0 and summary["reason"] == "passage_packet_limit"


def test_research_reserves_encoded_downstream_context_before_search(research_run, monkeypatch):
    from round_robin.providers import base
    monkeypatch.setattr(base, "MAX_INITIAL_PACKET_BYTES", 1)
    with research_run.begin() as session:
        schedule_research(session, session.get(Run, "run_local"))
    class Search:
        def search(self, *args, **kwargs):
            raise AssertionError("Search cannot add usable encoded context")
    assert execute_research("run_local", session_factory=research_run, search=Search())
    with research_run() as session:
        summary = session.get(Run, "run_local").configuration["research_summary"]
        assert summary["queries"] == 0 and summary["reason"] == "downstream_context_reserve"


def test_objection_resolution_requires_an_explicit_distinct_review_of_an_answer():
    run = SimpleNamespace(id="run_test", configuration={}, input_snapshot={"options": [], "hard_requirements": []})
    def stage(identity, provider, role):
        return SimpleNamespace(id=identity, run_id=run.id, provider=provider, stage=role)
    _, ledger, _ = reduce_discussion(run, stage("step_first", "openai", "proposal"), {"critical_defects": ["Required compatibility is unproven"]}, {"source_one"})
    run.configuration = {"discussion_issues": ledger}
    identity = ledger[0]["id"]
    response = {"objection_responses": [{"objection_id": identity, "disposition": "resolved", "response": "The source resolves the compatibility concern", "reference_ids": ["source_one"]}]}
    _, ledger, _ = reduce_discussion(run, stage("step_answer", "google", "synthesis"), response, {"source_one"})
    assert ledger[0]["status"] == "answered_pending_review"
    run.configuration = {"discussion_issues": ledger}
    _, ledger, _ = reduce_discussion(run, stage("step_review", "anthropic", "audit"), response, {"source_one"})
    assert ledger[0]["status"] == "resolved" and len(ledger[0]["events"]) == 3


def test_attestation_is_scoped_replay_safe_and_republishes_without_inference(factory):
    run_id = seed(factory, brief=complete_brief())
    drain(factory)
    app = create_app(str(factory.kw["bind"].url))
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with factory() as session:
        before_calls = sum(session.scalars(select(Step.calls)))
        evidence_before = {row.id: deepcopy(row.content) for row in session.scalars(select(EvidenceRecord))}
    with TestClient(app, raise_server_exceptions=False) as client:
        view_response = client.get(f"/api/runs/{run_id}/assessments")
        assert view_response.status_code == 200, view_response.text
        view = view_response.json()
        cost_targets = [item for item in view["targets"] if item["target_type"] == "option_costs"]
        assert cost_targets, "The assessment page must offer an option-cost target"
        target = cost_targets[0]
        body = {"command_id": str(uuid4()), "expected_run_version": view["run_version"], **{key: target[key] for key in ("target_type", "option_id", "requirement_id", "target_hash", "reference_ids")},
            "result": "supported", "attestation": "I attest these exact supplied cost fields and applicability for this comparison.", "valid_until": None}
        response = client.post(f"/api/runs/{run_id}/assessments", json=body)
        assert response.status_code == 201, response.text
        assert client.post(f"/api/runs/{run_id}/assessments", json=body).json() == response.json()
        assert response.json()["current_items"][0]["method"] == "user_attestation"
        assert response.json()["report_version"] == 2
        forged = {**body, "command_id": str(uuid4()), "expected_run_version": response.json()["run_version"], "target_hash": "0" * 64}
        assert client.post(f"/api/runs/{run_id}/assessments", json=forged).status_code == 409
        report = client.get(f"/api/runs/{run_id}/report").json()
        assert report["readiness"] != "ready_to_choose"  # simulation cannot qualify
        assert report["evidence_assessments"][0]["scope"].endswith("not independent supplier verification.")
    with factory() as session:
        assert sum(session.scalars(select(Step.calls))) == before_calls
        assert {row.id: row.content for row in session.scalars(select(EvidenceRecord))} == evidence_before
        assert len(list(session.scalars(select(ReportVersion)))) == 2
