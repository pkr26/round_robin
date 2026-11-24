"""The full approved public scope can be rendered without private input."""

import pytest
from round_robin.research.queries import PublicScope, QueryRejected, render_query


def test_query_renders_all_eight_approved_terms_in_requested_order():
    terms = tuple((f"term-{index}", f"public-{index}") for index in range(8))
    scope = PublicScope(terms=terms, region="Arizona")
    selected = [f"term-{index}" for index in reversed(range(8))]

    result = render_query(scope, term_ids=selected, template_id="comparison_v1")

    assert result == "public-7 public-6 public-5 public-4 public-3 public-2 public-1 public-0 comparison Arizona"


@pytest.mark.parametrize("selected", [[], ["approved", "approved"], ["private"]])
def test_query_rejects_empty_duplicate_and_unapproved_term_selection(selected):
    scope = PublicScope(terms=(("approved", "public term"),), region="Arizona")

    with pytest.raises(QueryRejected):
        render_query(scope, term_ids=selected, template_id="comparison_v1")


def test_public_term_length_accepts_120_characters_and_rejects_121():
    term = "x" * 120
    scope = PublicScope(terms=(("approved", term),))
    assert render_query(scope, term_ids=["approved"], template_id="comparison_v1").startswith(term)

    with pytest.raises(QueryRejected):
        PublicScope(terms=(("approved", term + "x"),))
