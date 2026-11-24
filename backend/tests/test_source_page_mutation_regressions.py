"""Saved evidence pagination must preserve source scope and stable cursors."""

from copy import deepcopy
from uuid import uuid4

import pytest
from round_robin.database.models import Attempt, BudgetOperation, EvidenceRecord
from round_robin.fixtures import sample_bundle
from round_robin.schemas.views import StoredEvidence
from sqlalchemy import select

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


@pytest.fixture
def sources_with_many_rows(artifacts):
    app, _, meeting_id, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        exemplar = session.get(EvidenceRecord, "passage_offer_a")
        for number in range(26):
            identity = f"source_{number:02d}"
            content = {**deepcopy(exemplar.content), "id": identity}
            StoredEvidence.model_validate(content)
            session.add(EvidenceRecord(id=identity, run_id=run_id, meeting_id=meeting_id, owner_id="owner_local", sha256=content["sha256"], content=content))
    return artifacts


@pytest.mark.parametrize("limit", [24, 25, 100])
def test_maximum_page_size_and_last_returned_cursor(sources_with_many_rows, limit):
    app, client, _, run_id, _ = sources_with_many_rows
    with app.state.session_factory() as session:
        expected = list(session.scalars(select(EvidenceRecord.id).where(EvidenceRecord.run_id == run_id, EvidenceRecord.owner_id == "owner_local").order_by(EvidenceRecord.id)))
    response = client.get(f"/api/runs/{run_id}/sources", params={"limit": limit})
    assert response.status_code == 200, response.text
    page = response.json()
    assert [item["id"] for item in page["items"]] == expected[:24]
    assert page["next_cursor"] == expected[23]
    next_page = client.get(f"/api/runs/{run_id}/sources", params={"limit": limit, "cursor": page["next_cursor"]}).json()
    assert [item["id"] for item in next_page["items"]] == expected[24:]
    assert next_page["next_cursor"] is None


def test_two_item_pages_have_no_missing_duplicate_or_false_terminal_cursor(sources_with_many_rows):
    _, client, _, run_id, _ = sources_with_many_rows
    first = client.get(f"/api/runs/{run_id}/sources", params={"limit": 2}).json()
    second = client.get(f"/api/runs/{run_id}/sources", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert first["next_cursor"] == first["items"][-1]["id"]
    assert second["next_cursor"] == second["items"][-1]["id"]
    assert len({item["id"] for item in first["items"] + second["items"]}) == 4


def test_exactly_full_final_page_has_no_next_cursor(artifacts):
    _, client, _, run_id, _ = artifacts
    page = client.get(f"/api/runs/{run_id}/sources", params={"limit": 3}).json()
    assert len(page["items"]) == 3
    assert page["next_cursor"] is None


def test_exact_source_identity_and_foreign_owner_are_not_confused(sources_with_many_rows):
    app, client, meeting_id, run_id, _ = sources_with_many_rows
    with app.state.session_factory.begin() as session:
        exemplar = session.get(EvidenceRecord, "passage_offer_a")
        content = {**deepcopy(exemplar.content), "id": "foreign_owner_source", "owner_id": "foreign_owner"}
        session.add(EvidenceRecord(id=content["id"], run_id=run_id, meeting_id=meeting_id, owner_id="foreign_owner", sha256=content["sha256"], content=content))
    exact = client.get(f"/api/runs/{run_id}/sources/source_25")
    assert exact.status_code == 200 and exact.json()["id"] == "source_25"
    foreign = client.get(f"/api/runs/{run_id}/sources/foreign_owner_source")
    assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "not_found"
    with app.state.session_factory() as session:
        assert not list(session.scalars(select(Attempt)))
        assert not list(session.scalars(select(BudgetOperation)))


def test_foreign_run_source_remains_not_found(artifacts):
    app, client, _, run_id, _ = artifacts
    meeting = client.post("/api/meetings", json={"command_id": str(uuid4()), "brief": sample_bundle().brief.model_dump(mode="json")}).json()
    preflight = client.post(f"/api/meetings/{meeting['id']}/preflight", json={"command_id": str(uuid4()), "expected_draft_revision": 1, "mode": "simulated"}).json()
    other = client.post(f"/api/meetings/{meeting['id']}/runs", json={"command_id": str(uuid4()), "preflight_id": preflight["id"], "expected_draft_revision": 1}).json()["id"]
    with app.state.session_factory.begin() as session:
        exemplar = session.get(EvidenceRecord, "passage_offer_a")
        content = {**deepcopy(exemplar.content), "id": "foreign_run_source", "run_id": other}
        session.add(EvidenceRecord(id=content["id"], run_id=other, meeting_id=meeting["id"], owner_id="owner_local", sha256=content["sha256"], content=content))
    response = client.get(f"/api/runs/{run_id}/sources/foreign_run_source")
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
