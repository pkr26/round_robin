"""PDF exports preserve the decision and its supporting records."""

from copy import deepcopy
from io import BytesIO

from pypdf import PdfReader
from round_robin.fixtures import sample_bundle
from round_robin.reports.exports import report_pdf
from round_robin.schemas.runtime import RuntimeUsage


def compact(value):
    return "".join(str(value).split())


def test_pdf_retains_recommendation_unknown_cost_and_saved_provenance():
    bundle = sample_bundle()
    report = bundle.scenarios[0].report.model_dump(mode="json")
    source = bundle.evidence[0].model_dump(mode="json")
    report["comparison"][0].update(total=None, known_subtotal="123.45", missing_components=["installation"])
    report["comparison"][1].update(total="0.00", known_subtotal="0.00", missing_components=[])
    report["next_evidence"][0].update(responsible_person="Procurement lead", revisit_date="2026-12-05")
    before = deepcopy((report, source))

    document = PdfReader(BytesIO(report_pdf(report, title="Saved decision", sources=[source])))
    text = compact("\n".join(page.extract_text() for page in document.pages))

    assert compact(f"Report {report['id']} | version {report['version']} | event cutoff {report['event_cutoff']}") in text
    assert compact("SIMULATED OUTPUT: no provider calls") in text
    assert compact(f"Execution: {report['execution_status']} | Readiness at assessment: {report['readiness']}") in text
    assert compact(report["recommendation"]["summary"]) in text
    assert compact(report["recommendation"]["conditions"][0]) in text
    assert compact("Unknown USD 12 months Known subtotal 123.45 USD Missing: 1 component; see details below") in text
    assert compact("0.00 USD 12 months Known subtotal 0.00 USD Missing: none recorded") in text
    assert compact("installation") in text
    for key in ("tradeoff", "largest_uncertainty", "what_would_change_answer", "next_action"):
        assert compact(report[key]) in text
    assert compact(report["dissent"][0]) in text
    assert compact(report["readiness_reasons"][0]) in text
    followup = report["next_evidence"][0]
    for key in ("question", "affected_conclusion", "verification_method", "request_text", "responsible_person", "revisit_date"):
        assert compact(followup[key]) in text
    calculation = report["calculations"][0]
    assert compact(f"{calculation['formula']} = {calculation['output']} {calculation['unit']}") in text
    assert compact(calculation["rounding"]) in text
    assert compact(calculation["inputs"][0]["source_ref"]) in text
    for key in ("source_title", "locator", "sha256", "text", "scope"):
        assert compact(source[key]) in text
    assert compact(report["coverage"][0]["provider"]) in text
    assert compact("Provider requests: 0") in text
    assert compact(report["stop_reason"]) in text
    assert (report, source) == before


def test_pdf_partial_report_preserves_unavailable_reason_and_unknown_calculation():
    report = sample_bundle().scenarios[1].partial_report.model_dump(mode="json")
    report.update(fictional=False, simulated=False)
    report["readiness_unavailable_reason"] = "Synthesis unavailable until the missing quote arrives."
    report["calculations"][0].update(formula="Pending quote plus installation", output=None, unit="USD")
    report["essential_dependencies"][1].update(status="unverified", accepted_scoped_support=False, reference_ids=[])
    report["usage"] = {
        "simulated": False, "status": "unknown", "actual_cost": None,
        "conservative_exposure_usd": "0.40", "unknown_operation_ids": ["attempt_unresolved", "attempt_second"],
        "request_count": 2, "currency": "USD",
    }
    RuntimeUsage.model_validate(report["usage"])

    document = PdfReader(BytesIO(report_pdf(report, title="Partial decision", sources=[])))
    text = compact("\n".join(page.extract_text() for page in document.pages))

    assert compact("Readiness at assessment: unavailable") in text
    assert compact(report["readiness_unavailable_reason"]) in text
    assert compact("Pending quote plus installation = Unknown USD") in text
    assert compact("Assessment: supported. Basis: passage_offer_a, passage_offer_b.") in text
    assert compact("Assessment: unverified. Basis: no supporting record supplied.") in text
    assert compact("Usage: unknown. Provider requests: 2. Confirmed cost: unknown USD.") in text
    assert compact("Conservative exposure: 0.40 USD.") in text
    assert compact("Unresolved operations: attempt_unresolved, attempt_second.") in text
