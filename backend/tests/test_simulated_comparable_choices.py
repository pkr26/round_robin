"""A simulated recommendation requires a unique, comparable lowest-cost option."""
from copy import deepcopy
from io import BytesIO

from pypdf import PdfReader
import pytest
from sqlalchemy import select

from backend.tests.test_evidence_regressions import complete_brief
from backend.tests.test_orchestration import drain, factory, seed
from round_robin.database.models import ReportVersion, Step
from round_robin.reports.exports import render_saved_report
from round_robin.reports.insights import comparable_cost_leaders
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.schemas.briefs import DraftBrief


def quoted_brief(green_id, reverse, costs, manual):
    body = complete_brief().model_dump(mode="json")
    body["deadline"] = None
    if not manual:
        body["hard_requirements"] = [item for item in body["hard_requirements"] if item["metric"] == "total_cost"]
    for option, identity, label, cost in zip(body["options"], [green_id, "option_blue", "option_keep"],
                                              ["Green printer", "Blue printer", "Keep existing printer"], costs, strict=True):
        option.update(id=identity, label=label)
        option["cost_components"] = [
            {"id": kind, "label": kind, "kind": kind, "amount": cost if kind == "upfront" else "0",
             "currency": "USD", "frequency": "once", "source_ref": f"quote_{identity}_{kind}",
             "unknown_reason": "A current price is missing." if kind == "upfront" and cost is None else None}
            for kind in ["upfront", "installation", "recurring", "maintenance", "tax_delivery"]]
    if reverse:
        body["options"].reverse()
    return DraftBrief.model_validate(body)


@pytest.mark.parametrize("green_id,reverse,costs,manual,expected,tied", [
    ("option_a", False, ("100", "100", "200"), False, None, ("Green printer", "Blue printer")),
    ("option_z", True, ("100", "100", "200"), False, None, ("Green printer", "Blue printer")),
    ("option_z", True, ("100", "101", "200"), False, "Green printer", ()),
    ("option_z", False, ("100", "101", "0"), False, "Keep existing printer", ()),
    ("option_a", False, ("100", "100", None), False, None, ()),
    ("option_a", False, ("100", "101", "200"), True, None, ()),
    ("option_a", False, ("5000", "5001", "5002"), False, None, ()),
])
def test_simulation_report_and_text_exports_preserve_comparable_choices(factory, green_id, reverse, costs, manual, expected, tied):
    run_id = seed(factory, brief=quoted_brief(green_id, reverse, costs, manual))
    drain(factory)
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        report = read_runtime_report(session, row)
        synthesis = session.scalar(select(Step).where(Step.run_id == run_id, Step.stage == "synthesis"))
        proposed = synthesis.result_json["output"]["recommendation_option_id"]
    labels = {item["option_id"]: item["label"] for item in report["comparison"]}
    if expected:
        assert labels[proposed] == report["recommendation"]["option_label"] == expected
        assert report["recommendation"]["option_id"] == proposed
    else:
        assert proposed is None and report["recommendation"] is None
    tie_ids = report["decision_insights"]["tied_lowest_cost_option_ids"]
    assert sorted(labels[identity] for identity in tie_ids) == sorted(tied)
    for format in ("markdown", "pdf"):
        content, _ = render_saved_report(report, format, title="Printer comparison", sources=[])
        text = content.decode().replace("\\", "") if format == "markdown" else "\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)
        headline = text.split("Recommendation", 1)[1].strip().splitlines()[0]
        if expected:
            assert expected in headline and proposed in headline
            if format == "markdown":
                assert f"### {expected} (preferred option)" in text
            else:
                table = text.split("Comparable costs and hard requirements", 1)[1].split("Requirement assessment provenance", 1)[0]
                assert "Preferred option" in table and expected in table
        elif tied:
            assert "Tied" in headline and all(label in headline for label in tied)
        else:
            insights = report["decision_insights"]
            if insights["cost_comparison_blocker"] == "all_options_failed":
                assert set(insights["failed_option_ids"]) == set(labels) and not insights["unresolved_option_ids"]
                assert "documented hard-requirement failure" in headline
            else:
                assert insights["cost_comparison_blocker"] == "unresolved_options" and "unresolved" in headline
            section = text.split("Recommendation", 1)[1].split("Comparable costs and hard requirements", 1)[0].split("## Reasons", 1)[0]
            assert all("".join(labels[identity].split()) in "".join(section.split())
                       for identity in insights["unresolved_option_ids"] + insights["failed_option_ids"])


def test_only_proven_failure_excludes_a_competing_option_and_applicability_must_match():
    def option(identity, cost, result, refs):
        return {"option_id": identity, "total": cost, "missing_components": [],
                "constraint_results": [{"requirement_id": "requirement", "result": result, "basis_refs": refs}]}
    first = option("option_a", "100.00", "pass", ["source_a"])
    failed = option("option_b", None, "fail", ["source_b"])
    assert comparable_cost_leaders({"requirement"}, [first, failed]) == ["option_a"]
    unresolved = deepcopy(failed)
    unresolved["constraint_results"][0]["basis_refs"] = []
    assert comparable_cost_leaders({"requirement"}, [first, unresolved]) == []
    different = option("option_c", "90", "not_applicable", ["source_c"])
    assert comparable_cost_leaders({"requirement"}, [first, different]) == []
