from uuid import uuid4

import pytest

from backend.tests.test_visual import visual_app as visual_app  # noqa: PLC0414
from round_robin.database.models import BudgetOperation
from round_robin.database.visual_models import DesignSession


def test_cancel_fences_work_without_releasing_a_foreign_owner_hold(visual_app):
    app, client, _, design = visual_app
    design_id = design["id"]
    with app.state.session_factory.begin() as session:
        row = session.get(DesignSession, design_id)
        revision, fence = row.revision, row.execution_fence
        session.add_all([
            BudgetOperation(id="hold_own", run_id=design_id, owner_id=row.owner_id, kind="visual_finish_hold", status="held", reserved_usd="0.20", metadata_json={}),
            BudgetOperation(id="hold_foreign", run_id=design_id, owner_id="foreign_owner", kind="visual_finish_hold", status="held", reserved_usd="0.40", metadata_json={"private": "unchanged"}),
            BudgetOperation(id="unknown_own", run_id=design_id, owner_id=row.owner_id, kind="visual_edit", status="unknown", reserved_usd="0.60", metadata_json={}),
        ])
    response = client.post(f"/api/design-sessions/{design_id}/cancel", json={"command_id": str(uuid4()), "expected_revision": revision})
    assert response.status_code == 200, response.text
    with app.state.session_factory() as session:
        row = session.get(DesignSession, design_id)
        assert row.status == "cancelled" and row.execution_fence == fence + 1
        own = session.get(BudgetOperation, "hold_own")
        assert own.status == "released" and own.reserved_usd == "0"
        foreign = session.get(BudgetOperation, "hold_foreign")
        assert (foreign.status, foreign.reserved_usd, foreign.metadata_json) == ("held", "0.40", {"private": "unchanged"})
        unknown = session.get(BudgetOperation, "unknown_own")
        assert unknown.status == "unknown" and unknown.reserved_usd == "0.60"


@pytest.mark.parametrize("damaged", [None, [], "private_sentinel"])
def test_cancel_fences_work_and_preserves_malformed_held_metadata(visual_app, damaged):
    app, client, _, design = visual_app
    design_id = design["id"]
    with app.state.session_factory.begin() as session:
        row = session.get(DesignSession, design_id)
        revision, fence = row.revision, row.execution_fence
        session.add(BudgetOperation(id="hold_bad", run_id=design_id, owner_id=row.owner_id, kind="visual_finish_hold", status="held", reserved_usd="0.40", metadata_json=damaged))
    response = client.post(f"/api/design-sessions/{design_id}/cancel", json={"command_id": str(uuid4()), "expected_revision": revision})
    assert response.status_code == 200, response.text
    with app.state.session_factory() as session:
        row = session.get(DesignSession, design_id)
        assert row.status == "cancelled" and row.execution_fence == fence + 1
        hold = session.get(BudgetOperation, "hold_bad")
        assert hold.status == "held" and hold.reserved_usd == "0.40" and hold.metadata_json == damaged


@pytest.mark.parametrize("damage", ["negative", "nan", "infinite", "nonzero_settlement", "booking", "bad_booking", "roles", "tag", "version", "model_call", "request_id", "legacy_extra", "release_reason"])
def test_cancel_fences_and_preserves_ambiguous_financial_hold(visual_app, damage):
    app, client, _, design = visual_app
    design_id = design["id"]
    metadata = {}
    amount = {"negative": "-0.1", "nan": "NaN", "infinite": "Infinity"}.get(damage, "0.40")
    if damage == "booking": metadata["conservative_booking_usd"] = "0.30"
    if damage == "bad_booking": metadata["conservative_booking_usd"] = "private_not_a_number"
    if damage == "roles": metadata["roles_remaining"] = [{}]
    if damage == "tag": metadata["target_tag"] = []
    if damage == "version": metadata["pool_version"] = True
    if damage == "model_call": metadata["model_call"] = True
    if damage == "request_id": metadata["provider_request_id"] = "private_receipt"
    if damage == "legacy_extra": metadata["legacy_extra"] = "unrecognized"
    if damage == "release_reason": metadata["release_reason"] = {"foreign": "unavailable"}
    settled = "0.10" if damage == "nonzero_settlement" else None
    with app.state.session_factory.begin() as session:
        design_row = session.get(DesignSession, design_id)
        revision, fence = design_row.revision, design_row.execution_fence
        session.add(BudgetOperation(id="hold_bad", run_id=design_id, owner_id=design_row.owner_id, kind="visual_finish_hold", status="held", reserved_usd=amount, settled_usd=settled, metadata_json=metadata))
    response = client.post(f"/api/design-sessions/{design_id}/cancel", json={"command_id": str(uuid4()), "expected_revision": revision})
    assert response.status_code == 200 and "private_not_a_number" not in response.text
    with app.state.session_factory() as session:
        design_row = session.get(DesignSession, design_id)
        assert design_row.status == "cancelled" and design_row.execution_fence == fence + 1
        row = session.get(BudgetOperation, "hold_bad")
        assert (row.status, row.reserved_usd, row.settled_usd, row.metadata_json) == ("held", amount, settled, metadata)
