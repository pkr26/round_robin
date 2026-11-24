import pytest
from round_robin.research.claims import ClaimRejected, grounded_claim_aliases


@pytest.mark.parametrize("status", ["contradicted", "mixed", "not_checked", "stale", "fictional"])
def test_unqualified_source_never_becomes_positive_claim_support(status):
    claim = {"alias": "new_claim_1", "kind": "fact", "evidence_status": "supported", "reference_ids": ["source_one"]}
    assert grounded_claim_aliases([claim], support_reference_ids={"source_one"}, reference_statuses={"source_one": status}) == set()


@pytest.mark.parametrize("status", ["supported", "user_supplied", None])
def test_current_or_unmarked_calculation_roots_remain_usable(status):
    claim = {"alias": "new_claim_1", "kind": "fact", "evidence_status": "supported", "reference_ids": ["source_one"]}
    statuses = {} if status is None else {"source_one": status}
    assert grounded_claim_aliases([claim], support_reference_ids={"source_one"}, reference_statuses=statuses) == {"new_claim_1"}


def test_refutation_edges_are_traversed_for_hidden_cycles_even_with_a_source():
    first = {"alias": "new_claim_1", "kind": "fact", "evidence_status": "supported", "reference_ids": ["source_one"], "refuting_reference_ids": ["new_claim_2"]}
    second = {"alias": "new_claim_2", "kind": "fact", "evidence_status": "supported", "reference_ids": ["new_claim_1"]}
    with pytest.raises(ClaimRejected, match="circular"):
        grounded_claim_aliases([first, second], support_reference_ids={"source_one"})


def test_calculation_can_transmit_scoped_support_to_a_factual_claim():
    calculation = {"id": "claim_previous", "kind": "calculation", "evidence_status": "supported", "reference_ids": ["source_one"]}
    fact = {"alias": "new_claim_1", "kind": "fact", "evidence_status": "supported", "reference_ids": ["claim_previous"]}
    assert grounded_claim_aliases([fact], support_reference_ids={"source_one"}, prior_claims=[calculation]) == {"new_claim_1"}


def test_sparse_nonfactual_claim_with_no_reference_ids_remains_unrooted():
    sparse = {"alias": "new_claim_1", "kind": "assumption", "evidence_status": "not_checked"}
    assert grounded_claim_aliases([sparse], support_reference_ids=set()) == set()
