import pytest
from round_robin.orchestration.protocol import next_stage


@pytest.mark.parametrize(
    ("confirmation", "expected"),
    [(False, "proposals"), (True, "awaiting_input")],
)
def test_successful_intake_holds_for_questions(confirmation, expected):
    assert next_stage("intake", ["openai"], confirmation_required=confirmation) == expected


@pytest.mark.parametrize(
    ("providers", "expected"),
    [
        ([], "publication"),
        (["openai"], "publication"),
        (["openai", "openai", "openai"], "publication"),
        (["openai", "anthropic"], "synthesis"),
        (["anthropic", "google", "openai"], "synthesis"),
    ],
)
def test_proposals_need_two_distinct_successful_providers(providers, expected):
    assert next_stage("proposals", providers) == expected


@pytest.mark.parametrize(("providers", "expected"), [([], "publication"), (["openai"], "audit")])
def test_synthesis_requires_success_before_audit(providers, expected):
    assert next_stage("synthesis", providers) == expected


def test_admitted_audit_correction_returns_to_synthesis():
    assert next_stage("audit", correction_admitted=True) == "synthesis"
