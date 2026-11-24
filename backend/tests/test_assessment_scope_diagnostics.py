"""Assessment rejections explain the invalid scope to API callers."""

from types import SimpleNamespace

import pytest

from round_robin.research.claims import ClaimRejected
from round_robin.research.discussion import assessment_target


@pytest.mark.parametrize(
    ("option_id", "requirement_id", "expected_terms"),
    [
        ("missing", None, ("option", "frozen brief")),
        ("option_one", "requirement_one", ("cost support", "requirement verdict")),
    ],
)
def test_invalid_assessment_scope_explains_its_reason(option_id, requirement_id, expected_terms):
    run = SimpleNamespace(
        configuration={},
        input_snapshot={"options": [{"id": "option_one"}], "currency": "USD"},
    )
    with pytest.raises(ClaimRejected) as caught:
        assessment_target(run, "option_costs", option_id, requirement_id)

    message = str(caught.value).lower()
    assert all(term in message for term in expected_terms)
