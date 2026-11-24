from copy import deepcopy
from datetime import date
from io import BytesIO

import pytest
from pypdf import PdfReader
from round_robin.fixtures import sample_brief, sample_bundle
from round_robin.orchestration.reporting import comparison_data
from round_robin.reports.exports import report_markdown, report_pdf
from round_robin.research.calculator import lifecycle_total


def option_for(kind):
    option = sample_brief().options[0]
    first = option.cost_components[0]
    if kind == "empty":
        components = []
    elif kind == "unknown":
        components = [first.model_copy(update={"amount": None, "unknown_reason": "Awaiting supplier quote"})]
    elif kind in {"excluded", "not_applicable"}:
        components = [first.model_copy(update={"status": kind})]
    elif kind == "zero":
        components = [first.model_copy(update={"amount": "0"})]
    else:
        components = [first, option.cost_components[1].model_copy(update={"amount": None, "unknown_reason": "Awaiting monthly price"})]
    return option.model_copy(update={"cost_components": components})


@pytest.mark.parametrize("kind", ["empty", "unknown", "excluded", "not_applicable"])
def test_no_known_amounts_are_not_described_as_explicit_zero(kind):
    result = lifecycle_total(option_for(kind), 12, "USD", calculation_id="calc_test", as_of=date(2026, 1, 1))
    assert result.output is None and result.known_subtotal == "0.00"
    assert result.formula == "No included cost amounts"
    assert result.missing_components


def test_explicit_zero_and_partial_known_arithmetic_keep_actual_formula():
    zero = lifecycle_total(option_for("zero"), 12, "USD", calculation_id="calc_test", as_of=date(2026, 1, 1))
    partial = lifecycle_total(option_for("partial"), 12, "USD", calculation_id="calc_test", as_of=date(2026, 1, 1))
    assert zero.output == "0.00" and zero.formula == "0 × 1"
    assert partial.output is None and partial.known_subtotal == "1000.00"
    assert partial.formula == "1000 × 1" and partial.missing_components


@pytest.mark.parametrize("kind", ["empty", "unknown", "excluded", "not_applicable"])
def test_calculated_unknown_flows_into_saved_export_without_zero_label(kind):
    brief = sample_brief()
    brief = brief.model_copy(update={"options": [option_for(kind), *brief.options[1:]]})
    comparison, calculations = comparison_data(brief, as_of=date(2026, 1, 1))
    assert comparison[0]["total"] is None
    calculation = calculations[0]
    assert calculation["output"] is None and calculation["formula"] == "No included cost amounts"
    report = sample_bundle().scenarios[0].report.model_dump(mode="json")
    report["calculations"][0] = calculation
    before = deepcopy(report)
    md = report_markdown(report, title="Synthetic", sources=[])
    pdf = PdfReader(BytesIO(report_pdf(report, title="Synthetic", sources=[])))
    text = "".join(page.extract_text() for page in pdf.pages)
    for result in (md, text):
        assert "No included cost amounts = Unknown USD" in result
        assert "Explicit zero cost = Unknown USD" not in result
    assert report == before
