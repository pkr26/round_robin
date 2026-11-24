"""Invalid requirement scopes give the attesting caller a useful reason."""

from types import SimpleNamespace

import pytest

from round_robin.research.claims import ClaimRejected
from round_robin.research.discussion import assessment_target


@pytest.mark.parametrize("requirement_id", ["missing", "cost"])
def test_requirement_scope_rejection_explains_cost_and_existence(requirement_id):
    run = SimpleNamespace(configuration={}, input_snapshot={
        "options": [{"id": "option_one"}],
        "hard_requirements": [{"id": "cost", "metric": "total_cost"}],
    })
    with pytest.raises(ClaimRejected) as caught:
        assessment_target(run, "requirement", "option_one", requirement_id)
    message = str(caught.value).lower()
    assert "requirement" in message
    assert "exist" in message
    assert "cost" in message
