from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest
from round_robin.reports.read_adapters import UnreadableReport, read_stored_evidence


@pytest.fixture
def evidence():
    text = "A synthetic quoted passage."
    content = {
        "id": "evidence_a", "run_id": "run_a", "owner_id": "owner_a", "source_title": "Synthetic quotation",
        "text": text, "sha256": sha256(text.encode()).hexdigest(), "origin": "user_supplied",
        "verification_status": "user_supplied", "verified_at": "2026-01-02T12:00:00Z", "time_sensitive": False,
        "origin_group": "input_a", "locator": "page 1", "scope": "quoted price",
    }
    return SimpleNamespace(id=content["id"], run_id=content["run_id"], owner_id=content["owner_id"], meeting_id="meeting_a", sha256=content["sha256"], content=content)


def read(row):
    return read_stored_evidence(row, run_id="run_a", owner_id="owner_a", meeting_id="meeting_a")


def test_valid_evidence_retains_json_timestamp_and_original_optional_shape(evidence):
    original = deepcopy(evidence.content)
    result = read(evidence)
    assert result == original
    assert isinstance(result["verified_at"], str)
    assert "url" not in result and "selected_start" not in result
    result["text"] = "Changed copy"
    assert evidence.content == original


@pytest.mark.parametrize("field", ["run_id", "owner_id", "meeting_id", "id", "sha256"])
def test_foreign_row_scope_or_identity_is_rejected_without_editing_saved_content(evidence, field):
    original = deepcopy(evidence.content)
    setattr(evidence, field, "other_" + field)
    with pytest.raises(UnreadableReport) as error:
        read(evidence)
    assert original["text"] not in str(error.value)
    assert evidence.content == original


@pytest.mark.parametrize("field", ["id", "run_id", "owner_id", "sha256", "text"])
def test_changed_content_scope_identity_or_passage_is_rejected(evidence, field):
    evidence.content[field] = "b" * 64 if field == "sha256" else "private_tampered_value"
    corrupted = deepcopy(evidence.content)
    with pytest.raises(UnreadableReport) as error:
        read(evidence)
    assert "private_tampered_value" not in str(error.value)
    assert evidence.content == corrupted
