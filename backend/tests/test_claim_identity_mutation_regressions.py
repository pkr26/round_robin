from copy import deepcopy

import pytest
from round_robin.research.claims import ClaimRejected, assign_claim_ids


def test_nested_source_and_response_local_references_are_scoped_and_rewritten():
    original = {
        "claims": [
            {"alias": "new_claim_1", "reference_ids": ["source_a"]},
            {"alias": "new_claim_2", "reference_ids": ["new_claim_1"],
             "support_bases": [{"reference_ids": ["source_a"]}], "refuting_reference_ids": ["source_b"]},
        ],
        "constraints": [{"basis_refs": ["new_claim_1", "source_b"], "counter_basis_refs": ["new_claim_2"]}],
        "objections": [{"target_reference_id": "new_claim_2", "reference_ids": ["source_a"]}],
        "objection_responses": [{"reference_ids": ["new_claim_1"]}],
    }
    before = deepcopy(original)
    ids = iter(["claim_server_1", "claim_server_2"])
    output, mapping = assign_claim_ids(original, allowed_reference_ids={"source_a", "source_b"}, id_factory=lambda: next(ids))
    assert original == before
    assert mapping == {"new_claim_1": "claim_server_1", "new_claim_2": "claim_server_2"}
    assert [item["id"] for item in output["claims"]] == ["claim_server_1", "claim_server_2"]
    assert output["claims"][1]["reference_ids"] == ["claim_server_1"]
    assert output["claims"][1]["support_bases"] == [{"reference_ids": ["source_a"]}]
    assert output["claims"][1]["refuting_reference_ids"] == ["source_b"]
    assert output["constraints"][0] == {"basis_refs": ["claim_server_1", "source_b"], "counter_basis_refs": ["claim_server_2"]}
    assert output["objections"][0] == {"target_reference_id": "claim_server_2", "reference_ids": ["source_a"]}
    assert output["objection_responses"][0]["reference_ids"] == ["claim_server_1"]


@pytest.mark.parametrize("key", ["reference_ids", "basis_refs", "counter_basis_refs", "refuting_reference_ids"])
def test_nested_foreign_references_are_rejected(key):
    with pytest.raises(ClaimRejected):
        assign_claim_ids({"nested": [{key: ["source_foreign"]}]}, allowed_reference_ids={"source_local"})


def test_target_can_point_to_scoped_existing_id_or_be_null_but_not_foreign():
    value = {"objections": [{"target_reference_id": "source_local"}, {"target_reference_id": None}]}
    output, _ = assign_claim_ids(value, allowed_reference_ids={"source_local"})
    assert output == value
    with pytest.raises(ClaimRejected):
        assign_claim_ids({"objections": [{"target_reference_id": "source_foreign"}]}, allowed_reference_ids={"source_local"})


def test_null_objection_target_does_not_skip_later_reference_rewrite():
    contribution = {
        "claims": [{"alias": "new_claim_1"}],
        "objections": [{"target_reference_id": None, "reference_ids": ["new_claim_1"]}],
    }
    result, _ = assign_claim_ids(contribution, allowed_reference_ids=set(), id_factory=lambda: "claim_server_1")
    assert result["objections"][0]["reference_ids"] == ["claim_server_1"]


def test_null_objection_target_does_not_skip_later_reference_validation():
    contribution = {
        "claims": [{"alias": "new_claim_1"}],
        "objections": [{"target_reference_id": None, "reference_ids": ["source_foreign"]}],
    }
    with pytest.raises(ClaimRejected):
        assign_claim_ids(contribution, allowed_reference_ids=set(), id_factory=lambda: "claim_server_1")


@pytest.mark.parametrize("malformed", [[{}], [None], "source_a", None])
def test_malformed_claim_reference_arrays_fail_with_controlled_rejection(malformed):
    with pytest.raises(ClaimRejected):
        assign_claim_ids({"claims": [{"alias": "new_claim_1", "reference_ids": malformed}]}, allowed_reference_ids={"source_a"})


def test_twenty_aliases_are_allowed_twenty_one_are_rejected():
    claims = [{"alias": f"new_claim_{index}"} for index in range(1, 22)]
    generated = iter(f"claim_server_{index}" for index in range(1, 21))
    result, mapping = assign_claim_ids({"claims": claims[:20]}, allowed_reference_ids=set(), id_factory=lambda: next(generated))
    assert len(result["claims"]) == len(mapping) == 20
    with pytest.raises(ClaimRejected):
        assign_claim_ids({"claims": claims}, allowed_reference_ids=set())


def test_duplicate_alias_and_generated_existing_identity_are_rejected():
    with pytest.raises(ClaimRejected):
        assign_claim_ids({"claims": [{"alias": "new_claim_1"}, {"alias": "new_claim_1"}]}, allowed_reference_ids=set())
    with pytest.raises(ClaimRejected):
        assign_claim_ids({"claims": [{"alias": "new_claim_1"}]}, allowed_reference_ids={"source_local"}, id_factory=lambda: "source_local")


def test_reference_budget_accepts_forty_distinct_in_scope_and_rejects_forty_one():
    refs = [f"source_{number}" for number in range(41)]
    output, _ = assign_claim_ids({"constraints": [{"basis_refs": refs[:40]}]}, allowed_reference_ids=set(refs))
    assert output["constraints"][0]["basis_refs"] == refs[:40]
    with pytest.raises(ClaimRejected):
        assign_claim_ids({"constraints": [{"basis_refs": refs}]}, allowed_reference_ids=set(refs))
