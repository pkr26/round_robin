"""Offline boundary checks for the user-approved public search scope."""

import pytest

from round_robin.research.queries import PublicScope, QueryRejected, render_query


def test_public_scope_accepts_maximum_approved_fields_and_renders_them():
    terms = tuple((f"term_{index}", "public text " + "x" * 108) for index in range(8))
    scope = PublicScope(terms=terms, region="r" * 120, template_ids=("comparison_v1",))

    query = render_query(scope, term_ids=[key for key, _ in terms], template_id="comparison_v1")
    assert query == " ".join(value for _, value in terms) + " comparison " + scope.region


@pytest.mark.parametrize(
    "scope_fields",
    [
        {"terms": tuple((str(i), "approved") for i in range(9))},
        {"region": "r" * 121},
        {"terms": (("same", "one"), ("same", "two"))},
        {"terms": (("", "approved"),)},
        {"terms": (("term", "   "),)},
        {"terms": (("term", "x" * 121),)},
        {"terms": (("term", "valid\x1ftext"),)},
        {"template_ids": ("comparison_v1", "private-prose")},
    ],
)
def test_public_scope_rejects_invalid_fields_independently(scope_fields):
    with pytest.raises(QueryRejected):
        PublicScope(**scope_fields)
