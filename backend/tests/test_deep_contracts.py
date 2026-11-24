from pydantic import ValidationError
import pytest

from round_robin.fixtures import SampleBundle, sample_bundle


def test_fixture_scope_is_keyed_by_identity_instead_of_collection_order():
    body = sample_bundle().model_dump(mode="json")
    body["scenarios"] = list(reversed(body["scenarios"]))
    restored = SampleBundle.model_validate(body)
    assert {item.id for item in restored.scenarios} == {"base12", "horizon24", "missing_installation"}


def test_missing_base_fixture_report_is_a_controlled_contract_failure():
    body = sample_bundle().model_dump(mode="json")
    body["scenarios"][0]["report"] = None
    with pytest.raises(ValidationError):
        SampleBundle.model_validate(body)
