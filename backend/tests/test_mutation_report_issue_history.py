"""Saved objection history must survive both human-readable exports."""

import re
from html import unescape
from io import BytesIO

import pytest
from pypdf import PdfReader
from round_robin.fixtures import sample_bundle
from round_robin.reports.exports import describe, report_markdown, report_pdf
from round_robin.schemas.discussion import DiscussionIssue


@pytest.mark.parametrize("format", ["markdown", "pdf"])
def test_report_export_retains_saved_issue_event_history(format):
    report = sample_bundle().scenarios[0].report.model_dump(mode="json")
    issue = DiscussionIssue.model_validate({
        "id": "issue_installation", "concern": "Installation may be excluded.",
        "severity": "critical", "status": "open", "origin": "structured",
        "source_step_id": "step_initial", "source_provider": "provider_a <img src=x>", "source_stage": "proposal",
        "target_reference_id": "passage_offer_a", "option_id": "option_a", "requirement_id": "requirement_budget",
        "reference_ids": ["passage_offer_a", "passage_offer_b"],
        "events": [
            {
                "id": "event_response", "step_id": "step_review", "provider": "provider_b", "stage": "audit",
                "disposition": "answered", "response": "Quote excludes installation. ![pixel](https://example.invalid/x) <img src=x>",
                "reference_ids": ["evidence_quote"], "input_hash": "a" * 64,
            },
            {
                "id": "event_reopened", "step_id": "step_followup", "provider": "provider_c", "stage": "audit",
                "disposition": "reopened", "response": "A dated installation quote is still required.",
                "reference_ids": ["evidence_revision"], "input_hash": "b" * 64,
            },
        ],
    }).model_dump(mode="json")
    report["issues"] = [issue]

    if format == "markdown":
        text = report_markdown(report, title="Saved report", sources=[])
        assert "![pixel](" not in text and "<img" not in text
        text = unescape(re.sub(r"\\([\\`*_{}\[\]()#+!|\-.])", r"\1", text))
    else:
        reader = PdfReader(BytesIO(report_pdf(report, title="Saved report", sources=[])))
        text = "\n".join(page.extract_text() for page in reader.pages)

    compact = lambda value: "".join(value.split())
    text = compact(text)
    for value in (issue["id"], issue["concern"], "event_response", "provider_b", "answered", "evidence_quote", issue["events"][0]["response"], "event_reopened", "provider_c", "reopened", "evidence_revision", issue["events"][1]["response"]):
        assert compact(value) in text
    assert compact("critical: Installation may be excluded.") in text
    assert compact("Status: open. Issue: issue_installation.") in text
    for value in (
        "origin: structured", "source_step_id: step_initial", "source_provider: provider_a <img src=x>",
        "source_stage: proposal", "target_reference_id: passage_offer_a", "option_id: option_a",
        "requirement_id: requirement_budget", "reference_ids: passage_offer_a, passage_offer_b",
    ):
        assert compact(value) in text
    assert text.index(compact("event_response")) < text.index(compact("event_reopened"))


@pytest.mark.parametrize("field", ["responses", "resolutions"])
def test_issue_description_preserves_legacy_history_fields(field):
    assert "Earlier saved response" in describe({"concern": "Prior issue", field: ["Earlier saved response"]})


@pytest.mark.parametrize("format", ["markdown", "pdf"])
@pytest.mark.parametrize("has_events", [False, True])
def test_report_export_retains_mixed_and_legacy_issue_history(format, has_events):
    report = sample_bundle().scenarios[0].report.model_dump(mode="json")
    events = [{"response": "New event response"}] if has_events else []
    report["issues"] = [{
        "concern": "Legacy issue", "events": events,
        "responses": ["Earlier saved response"], "resolutions": ["Earlier saved resolution"],
    }]

    if format == "markdown":
        text = report_markdown(report, title="Saved report", sources=[])
    else:
        reader = PdfReader(BytesIO(report_pdf(report, title="Saved report", sources=[])))
        text = "\n".join(page.extract_text() for page in reader.pages)
    text = "".join(text.split())
    assert "Earliersavedresponse" in text
    assert "Earliersavedresolution" in text
    if has_events:
        assert "Neweventresponse" in text
