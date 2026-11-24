"""Saved requirement judgments retain their provenance in human-readable exports."""

import re
from copy import deepcopy
from html import unescape
from io import BytesIO

import pytest
from pypdf import PdfReader
from round_robin.fixtures import sample_bundle
from round_robin.reports.exports import report_markdown, report_pdf
from round_robin.schemas.contributions import ConstraintResult
from round_robin.schemas.reports import DecisionReport


def compact(value):
    return "".join(str(value).split())


@pytest.mark.parametrize("format", ["markdown", "pdf"])
def test_export_preserves_contested_requirement_assessment_provenance(format):
    report = sample_bundle().scenarios[0].report.model_dump(mode="json")
    constraint = report["comparison"][0]["constraint_results"][1]
    constraint.update(
        result="unknown",
        method="model",
        assessor="Auditor <img src=x> ![tracker](https://example.invalid/x)",
        reason="The two assessments disagree on the installation date.",
        basis_refs=["passage_offer_a"],
        counter_basis_refs=["passage_offer_b"],
        assessment_scope="Attributed review of the supplied statements only.",
        disputed=True,
    )
    report["readiness"] = "conditional"
    ConstraintResult.model_validate(constraint)
    DecisionReport.model_validate(report)
    saved = deepcopy(report)
    if format == "markdown":
        raw = report_markdown(report, title="Saved comparison", sources=[])
        assert "<img" not in raw and "![tracker](" not in raw
        text = unescape(re.sub(r"\\([\\`*_{}\[\]()#+!|\-.])", r"\1", raw))
    else:
        document = PdfReader(BytesIO(report_pdf(report, title="Saved comparison", sources=[])))
        text = "\n".join(page.extract_text() for page in document.pages)
        comparison = text.split("Requirement assessment provenance", 1)[0]
        assert compact(f"{constraint['requirement_id']}: unknown. Disputed: yes. {constraint['reason']}") in compact(comparison)
    text = compact(text)
    expected = (
        f"{constraint['requirement_id']}: unknown. Disputed: yes. {constraint['reason']} "
        f"Basis: passage_offer_a. Counter-evidence: passage_offer_b. "
        f"Method: model. Assessor: {constraint['assessor']}. "
        f"Scope: {constraint['assessment_scope']}"
    )
    assert compact(expected) in text
    assert report == saved


@pytest.mark.parametrize("format", ["markdown", "pdf"])
@pytest.mark.parametrize("sensitive", [True, False])
@pytest.mark.parametrize("status", ["not_checked", "mixed"])
def test_export_preserves_evidence_record_time_and_sensitivity(format, sensitive, status):
    import hashlib

    from round_robin.schemas.views import StoredEvidence

    passage = "The quoted installation period is six weeks."
    source = StoredEvidence.model_validate({
        "id": "evidence_dated", "run_id": "run_sample", "owner_id": "owner_sample",
        "source_title": "Supplier quote", "text": passage,
        "sha256": hashlib.sha256(passage.encode()).hexdigest(),
        "origin": "retrieved", "verification_status": status,
        "verified_at": "2026-04-03T14:30:00Z", "time_sensitive": sensitive,
        "origin_group": "group_quote", "locator": "Quote page 2",
        "scope": "Retrieved passage; inspect the stated verification status and exact scope.",
    }).model_dump(mode="json")
    report = sample_bundle().scenarios[0].report.model_dump(mode="json")
    if format == "markdown":
        raw = report_markdown(report, title="Saved comparison", sources=[source])
        text = unescape(re.sub(r"\\([\\`*_{}\[\]()#+!|\-.])", r"\1", raw))
    else:
        document = PdfReader(BytesIO(report_pdf(report, title="Saved comparison", sources=[source])))
        text = "\n".join(page.extract_text() for page in document.pages)
    text = compact(text)
    for fact in (source["id"], source["text"], source["sha256"], source["origin"], source["verification_status"], source["verified_at"], f"Time-sensitive: {'yes' if sensitive else 'no'}"):
        assert compact(fact) in text


def test_pdf_keeps_long_disputed_provenance_through_page_breaks():
    report = sample_bundle().scenarios[0].report.model_dump(mode="json")
    support = [f"evidence_{index:02d}_" + "a" * 80 for index in range(40)]
    counter = [f"counter_{index:02d}_" + "b" * 80 for index in range(40)]
    constraint = report["comparison"][0]["constraint_results"][1]
    constraint.update(
        result="unknown", disputed=True, method="model", assessor="Auditor",
        reason="The source statements disagree about the installation limit.",
        assessment_scope="Applies only to the dated installation period.",
        basis_refs=support, counter_basis_refs=counter,
    )
    report["readiness"] = "conditional"
    ConstraintResult.model_validate(constraint)
    DecisionReport.model_validate(report)
    document = PdfReader(BytesIO(report_pdf(report, title="Long provenance", sources=[])))
    assert len(document.pages) > 3
    parts = []
    for number, page in enumerate(document.pages, 1):
        text = page.extract_text()
        text = text.replace(f"Round Robin | v{report['version']} / cutoff {report['event_cutoff']}\nPage {number}\n", "")
        text = text.replace("Option\nAll-in total\nRequirement results\n", "")
        parts.append(text)
    text = compact("".join(parts))
    assert text.index(compact("requirement_goal: unknown. Disputed: yes.")) < text.index(compact(support[0]))
    assert all(reference in text for reference in support + counter)
    assert compact(constraint["assessment_scope"]) in text
