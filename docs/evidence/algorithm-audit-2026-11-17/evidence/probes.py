"""Offline adversarial algorithm probes; no product source edits or live calls.

Run: PYTHONPATH=backend/src:backend/tests uv run python docs/evidence/algorithm-audit-2026-11-17/evidence/probes.py
The research probe deliberately reuses the existing synthetic SQLite ledger
fixture and its injected frozen-configuration check. All transports are fakes.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from pytest import MonkeyPatch
from sqlalchemy import select

from round_robin.database.models import EvidenceRecord, Run
from round_robin.orchestration.reporting import build_report
from round_robin.providers.base import ProviderOutput, ProviderRequest, validate_output
from round_robin.research.fetcher import FetchResponse, RetrievedSource, exact_passages
from round_robin.research.search import SearchHit
from round_robin.research.service import execute_research, schedule_research
from round_robin.schemas.briefs import DraftBrief


def brief():
    return DraftBrief.model_validate({
        "revision": 1, "title": "Algorithm audit fixture", "question": "Choose A or B",
        "goal": "Meet the mandatory requirement", "currency": "USD", "horizon_months": 12,
        "options": [{"id": f"option_{name}", "label": name, "provenance": "user_supplied",
            "cost_components": [{"id": f"cost_{name}_{kind}", "label": kind, "kind": kind,
                "amount": "100" if kind == "upfront" else "0", "currency": "USD",
                "frequency": "once", "source_ref": "evidence_cost"}
                for kind in ("upfront", "installation", "recurring", "maintenance", "tax_delivery")]}
            for name in ("a", "b")],
        "hard_requirements": [{"id": "requirement_manual", "description": "Supports required feature",
            "origin": "user_supplied", "metric": "manual", "operator": "manual"}],
    }).model_dump(mode="json")


def output(**changes):
    return ProviderOutput.model_validate({"summary": "Synthetic review", "recommendation_option_id": "option_a",
        "reference_ids": [], "questions": [], "assumptions": [], "constraints": [], "audit_passed": None,
        "critical_defects": [], "dissent": [], **changes}).model_dump(mode="json")


def constraints(result, refs):
    return [{"option_id": f"option_{name}", "requirement_id": "requirement_manual", "result": result,
        "reason": f"Synthetic {result} assessment", "basis_refs": refs} for name in ("a", "b")]


def step(identity, stage, provider, value):
    return SimpleNamespace(id=identity, stage=stage, provider=provider, status="succeeded", error_code=None,
        result_json={"output": value})


def report(*, synth=None, audit=None, proposals=None, evidence_status="user_supplied"):
    frozen = brief()
    run = SimpleNamespace(id="run_audit", input_snapshot=frozen, created_at=datetime(2026, 11, 17, tzinfo=timezone.utc),
        event_sequence=10, configuration={"mode": "live", "evidence_prepared": True, "resolved_brief": frozen})
    steps = proposals or [step("proposal_a", "proposal", "openai", output()), step("proposal_b", "proposal", "google", output())]
    steps += [step("synthesis_a", "synthesis", "openai", synth or output(constraints=constraints("pass", ["evidence_positive"]))),
        step("audit_a", "audit", "anthropic", audit or output(audit_passed=True, constraints=constraints("pass", ["evidence_positive"])))]
    return build_report(run, steps, status="completed", stop_reason="Synthetic probe complete", report_id="report_probe", version=1,
        usage={"simulated": False, "status": "reported", "actual_cost": "0", "conservative_exposure_usd": "0",
            "unknown_operation_ids": [], "request_count": 4},
        evidence=[{"id": "evidence_cost", "verification_status": evidence_status},
            {"id": "evidence_positive", "verification_status": "not_checked"}])


def audit_disagreement():
    value = output(audit_passed=False, constraints=constraints("fail", []))
    request = ProviderRequest(run_id="run_audit", step_id="audit_a", stage="audit", role_slot="audit",
        brief=brief(), evidence=[{"id": "evidence_positive"}], peer_outputs=[],
        allowed_reference_ids=frozenset({"evidence_positive"}), max_output_tokens=2048, timeout_seconds=30)
    validated = validate_output(ProviderOutput.model_validate(value), request)
    result = report(audit=validated.model_dump(mode="json"))
    assert result["readiness"] == "no_feasible_option"
    assert all(item["constraint_results"][0]["basis_refs"] == ["evidence_positive"] for item in result["comparison"])
    return {"audit_output_accepted": True, "audit_failure_references": [], "readiness": result["readiness"],
        "published_constraints": [item["constraint_results"] for item in result["comparison"]]}


def missing_verification_transition():
    ordinary = report()
    synthetic_promoted = report(evidence_status="supported")
    assert ordinary["readiness"] == "conditional"
    assert synthetic_promoted["readiness"] == "ready_to_choose"
    return {"ordinary_runtime_evidence_status": "user_supplied", "ordinary_readiness": ordinary["readiness"],
        "ordinary_reasons": ordinary["readiness_reasons"],
        "after_direct_synthetic_promotion_readiness": synthetic_promoted["readiness"],
        "scope": "Static call-site audit separately establishes that the application has no promotion transition."}


def dissent_loss():
    proposals = [step("proposal_a", "proposal", "openai", output(dissent=[f"First proposal concern {n}" for n in range(12)])),
        step("proposal_b", "proposal", "google", output(dissent=[f"Second proposal concern {n}" for n in range(12)]))]
    critical = "AUDITOR_CRITICAL: supplier lacks the mandatory certification."
    result = report(proposals=proposals, audit=output(audit_passed=False, critical_defects=[critical],
        dissent=["AUDITOR_DISSENT: do not accept the supplier claim."]))
    assert len(result["dissent"]) == 20 and not any("AUDITOR_" in item for item in result["dissent"])
    unresolved = report(proposals=[step("proposal_a", "proposal", "openai", output(critical_defects=["PROPOSAL_CRITICAL: unsupported safety claim"])),
        step("proposal_b", "proposal", "google", output())])
    assert not unresolved["dissent"] and not unresolved["assessment"]["unresolved_critical_defect"]
    return {"dissent_count": len(result["dissent"]), "auditor_specific_concerns_survive": False,
        "generic_unresolved_defect_flag_survives": result["assessment"]["unresolved_critical_defect"],
        "proposal_critical_defect_in_final_dissent": unresolved["dissent"],
        "proposal_critical_defect_flag": unresolved["assessment"]["unresolved_critical_defect"]}


def support_laundering():
    from round_robin.research.claims import grounded_claim_aliases
    claim = {"alias": "new_claim_1", "proposition": "The supplier is certified.", "kind": "fact", "evidence_status": "supported", "reference_ids": ["claim_rejected"]}
    prior = {"id": "claim_rejected", "proposition": "The supplier is certified.", "kind": "fact", "evidence_status": "contradicted", "reference_ids": ["evidence_denial"]}
    request = ProviderRequest(run_id="run_audit", step_id="proposal_a", stage="proposal", role_slot="proposal",
        brief=brief(), evidence=[{"id": "evidence_denial", "text": "The supplier is NOT certified.", "verification_status": "contradicted"}],
        peer_outputs=[], allowed_reference_ids=frozenset({"claim_rejected", "evidence_denial"}), max_output_tokens=3072,
        timeout_seconds=30, claims=(prior,))
    result = validate_output(ProviderOutput.model_validate(output(claims=[claim])), request)
    assert result.claims[0].evidence_status == "supported"
    return {"prior_claim_status": prior["evidence_status"], "source_text": request.evidence[0]["text"],
        "source_status": "contradicted", "new_claim_status": result.claims[0].evidence_status,
        "rooted_aliases": sorted(grounded_claim_aliases([claim], support_reference_ids={"evidence_denial"}, prior_claims=[prior]))}


def prefix_passages():
    source = RetrievedSource("https://example.com/terms", "Header " * 600 + "CRITICAL EXCLUSION: Installation is not included.", "a" * 64,
        "2026-11-17T00:00:00Z", "origin_example")
    passages = exact_passages(source, maximum=2)
    assert not any("CRITICAL EXCLUSION" in item["text"] for item in passages)
    return {"source_characters": len(source.text), "selected_characters": sum(len(item["text"]) for item in passages),
        "critical_exclusion_selected": False, "scope_notice": passages[0]["scope"]}


def counterevidence_starvation():
    from test_research_service import research_run
    class Search:
        def __init__(self): self.queries = []
        def search(self, query, **kwargs):
            self.queries.append(query)
            turn = len(self.queries)
            return [SearchHit(f"Query {turn} source {i}", f"https://example.com/query{turn}/source{i}") for i in range(8)]
    class Pages:
        def __init__(self): self.urls = []
        def get(self, target, **kwargs):
            self.urls.append(target.url)
            return FetchResponse(200, {"content-type": "text/plain"}, ("Facts from " + target.url).encode())
    with TemporaryDirectory() as directory, MonkeyPatch.context() as monkeypatch:
        fixture = research_run.__wrapped__(Path(directory), monkeypatch)
        factory = next(fixture)
        try:
            with factory.begin() as session:
                schedule_research(session, session.get(Run, "run_local"))
            search, pages = Search(), Pages()
            assert execute_research("run_local", session_factory=factory, search=search, transport=pages, resolver=lambda _: ["93.184.216.34"])
            with factory() as session:
                summary = deepcopy(session.get(Run, "run_local").configuration["research_summary"])
                saved_urls = [row.content["url"] for row in session.scalars(select(EvidenceRecord))]
            assert len(search.queries) == 3 and len(pages.urls) == 8
            assert all("/query1/" in url for url in saved_urls)
            assert summary["limitations"] == []
            return {"queries": search.queries, "fetched_urls": pages.urls, "saved_urls": saved_urls,
                "later_query_sources_fetched": 0, "research_summary": summary}
        finally:
            fixture.close()


if __name__ == "__main__":
    results = {name: probe() for name, probe in [
        ("audit_disagreement", audit_disagreement), ("missing_verification_transition", missing_verification_transition),
        ("dissent_loss", dissent_loss), ("support_laundering", support_laundering),
        ("prefix_passages", prefix_passages), ("counterevidence_starvation", counterevidence_starvation)]}
    target = Path(__file__).with_name("probe-results.json")
    target.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({"probes": len(results), "all_assertions_passed": True, "results": str(target)}, indent=2))
