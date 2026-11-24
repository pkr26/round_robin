"""A saved report export retains decision facts and uncertainty."""

import re
from copy import deepcopy
from html import unescape

from round_robin.fixtures import sample_bundle
from round_robin.reports.exports import describe, report_markdown
from round_robin.schemas.runtime import RuntimeUsage


def literal_markdown(text):
    return unescape(re.sub(r"\\([\\`*_{}\[\]()#+!|\-.])", r"\1", text))


def test_legacy_usage_without_exposure_fields_keeps_its_existing_description():
    usage = {"status": "reported", "request_count": 1, "actual_cost": "0.10", "currency": "USD"}
    assert describe(usage) == "Usage: reported. Provider requests: 1. Confirmed cost: 0.10 USD."


def test_markdown_preserves_recommendation_identity_cost_uncertainty_and_source():
    bundle = sample_bundle()
    report = bundle.scenarios[0].report.model_dump(mode="json")
    source = bundle.evidence[0].model_dump(mode="json")
    report["comparison"][0].update(total=None, known_subtotal="123.45", missing_components=["installation"])
    report["comparison"][1].update(total="0.00", known_subtotal="0.00", missing_components=[])
    report["next_evidence"][0].update(responsible_person="Procurement lead", revisit_date="2026-12-05")
    before = deepcopy((report, source))

    text = literal_markdown(report_markdown(report, title="Saved decision", sources=[source]))

    assert f"Saved report {report['id']} | version {report['version']} | event cutoff {report['event_cutoff']}" in text
    assert "SIMULATED: no provider calls" in text
    assert report["recommendation"]["summary"] in text
    assert report["recommendation"]["conditions"][0] in text
    assert "All-in total: Unknown USD over 12 months." in text
    assert "Known subtotal: 123.45 USD. Missing: installation." in text
    assert "All-in total: 0.00 USD over 12 months." in text
    assert source["source_title"] in text and source["locator"] in text
    assert source["sha256"] in text and source["text"] in text and source["scope"] in text
    calculation = report["calculations"][0]
    assert f"{calculation['formula']} = {calculation['output']} {calculation['unit']}" in text
    assert calculation["rounding"] in text
    assert calculation["inputs"][0]["source_ref"] in text
    assert report["coverage"][0]["provider"] in text and report["coverage"][0]["notes"] in text
    assert "Provider requests: 0" in text and "Confirmed cost: 0 USD" in text
    assert report["stop_reason"] in text
    for key in ("tradeoff", "largest_uncertainty", "what_would_change_answer", "next_action"):
        assert report[key] in text
    followup = report["next_evidence"][0]
    for key in ("question", "affected_conclusion", "verification_method", "request_text", "responsible_person", "revisit_date"):
        assert followup[key] in text
    assert report["dissent"][0] in text and report["readiness_reasons"][0] in text
    assert (report, source) == before


def test_markdown_does_not_invent_recommendation_for_partial_report():
    report = sample_bundle().scenarios[1].partial_report.model_dump(mode="json")
    report.update(fictional=False, simulated=False)
    report["calculations"][0]["output"] = None
    report["essential_dependencies"][1].update(status="unverified", accepted_scoped_support=False, reference_ids=[])
    report["usage"] = {
        "simulated": False, "status": "unknown", "actual_cost": None,
        "conservative_exposure_usd": "0.40", "unknown_operation_ids": ["attempt_unresolved", "attempt_second"],
        "request_count": 2, "currency": "USD",
    }
    RuntimeUsage.model_validate(report["usage"])

    text = literal_markdown(report_markdown(report, title="Partial decision", sources=[]))

    assert "Decision readiness: unavailable" in text
    assert report["readiness_unavailable_reason"] in text
    assert "No valid synthesis exists" in text
    assert f"{report['calculations'][0]['formula']} = Unknown USD" in text
    assert "Assessment: supported. Basis: passage_offer_a, passage_offer_b." in text
    assert "Assessment: unverified. Basis: no supporting record supplied." in text
    assert "Usage: unknown. Provider requests: 2. Confirmed cost: unknown USD." in text
    assert "Conservative exposure: 0.40 USD." in text
    assert "Unresolved operations: attempt_unresolved, attempt_second." in text
