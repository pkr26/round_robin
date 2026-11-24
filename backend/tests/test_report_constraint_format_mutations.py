from round_robin.reports.exports import describe_constraint
from round_robin.schemas.contributions import ConstraintResult


def constraint(**changes):
    return ConstraintResult.model_validate({"requirement_id":"req_1", "result":"unknown", "reason":"pending", "method":"human", "assessor":"Pat", **changes}).model_dump(mode="json")


def test_empty_basis():
    assert "Basis: none recorded." in describe_constraint(constraint(basis_refs=[]))


def test_scope_punctuation():
    assert describe_constraint(constraint(assessment_scope="Only this quote.")).endswith("Scope: Only this quote.")


def test_separator():
    assert "Basis: ref_a / ref_b." in describe_constraint(constraint(basis_refs=["ref_a", "ref_b"]), reference_separator=" / ")
