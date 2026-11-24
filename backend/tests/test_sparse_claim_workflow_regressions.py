from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from round_robin.database.claim_models import ClaimVersion, ReuseRecord
from round_robin.database.models import Run
from round_robin.database.services import canonical_hash
from round_robin.research.claims import ClaimRejected
from round_robin.research.reuse import claim_packet, read_claim
from sqlalchemy import select
from test_exhaustive_evidence_reuse import preflight, start

pytest_plugins = ("test_exhaustive_evidence_reuse",)


def test_saved_sparse_assumption_survives_read_followup_and_worker_packet(parent):
    app, _, run, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        row = session.get(ClaimVersion, selections[1]["id"])
        content = deepcopy(row.content)
        content.update(kind="assumption", evidence_status="not_checked", time_sensitive=False)
        content.pop("reference_ids")
        content.pop("support_bases", None)
        row.content, row.sha256 = content, canonical_hash(content)
        original_hash = row.sha256
        selection = {**selections[1], "sha256": original_hash}
    with TestClient(app) as client:
        visible = client.get(f"/api/runs/{run['id']}/claims")
        assert visible.status_code == 200, visible.text
        assert visible.json()["items"][0] == {**content, "sha256": original_hash}
        quote = preflight(client, parent, [selection])
        assert quote.status_code == 200, quote.text
        response, _ = start(client, parent, quote.json())
        assert response.status_code == 202, response.text
        child_id = response.json()["id"]
    with app.state.session_factory() as session:
        copied = claim_packet(session, session.get(Run, child_id), imported_only=True)[0]
        assert copied["id"] != content["id"]
        assert copied["reference_ids"] == [] and copied["evidence_status"] == "not_checked"
        entry = session.scalar(select(ReuseRecord).where(ReuseRecord.run_id == child_id))
        assert entry.parent_sha256 == original_hash and entry.child_id == copied["id"]
        old = session.get(ClaimVersion, content["id"])
        assert old.content == content and old.sha256 == original_hash
        from types import SimpleNamespace

        from round_robin.worker.tasks import build_request
        request = build_request(session, session.get(Run, child_id), SimpleNamespace(id="step_test", stage="proposal", role_slot_id="proposal_test", calls=0, error_code=None))
        assert request.claims[0]["id"] == copied["id"]


def test_saved_sparse_imported_claim_builds_json_provider_packet(parent):
    import json
    from types import SimpleNamespace

    from round_robin.worker.tasks import build_request

    app, _, run, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        row = session.get(ClaimVersion, selections[1]["id"])
        content = deepcopy(row.content)
        content.update(kind="assumption", evidence_status="not_checked", time_sensitive=False)
        content.pop("reference_ids")
        content.pop("support_bases", None)
        row.content, row.sha256, row.step_id = content, canonical_hash(content), None
        original_hash = row.sha256
    with app.state.session_factory() as session:
        request = build_request(session, session.get(Run, run["id"]), SimpleNamespace(id="step_test", stage="proposal", role_slot_id="proposal_test", calls=0, error_code=None))
        packet = request.packet()
        assert request.claims[0] == {**content, "sha256": original_hash}
        assert content["id"] in json.dumps(packet)


@pytest.mark.parametrize("kind", ["fact", "calculation"])
def test_sparse_supported_factual_claim_without_any_basis_fails_closed(parent, kind):
    app, _, run, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        row = session.get(ClaimVersion, selections[1]["id"])
        content = deepcopy(row.content)
        content.update(kind=kind, evidence_status="supported")
        content.pop("reference_ids")
        content.pop("support_bases", None)
        row.content, row.sha256 = content, canonical_hash(content)
        with pytest.raises(ClaimRejected):
            read_claim(row, run=session.get(Run, run["id"]))
        assert row.content == content and row.sha256 == canonical_hash(content)


@pytest.mark.parametrize("include_source", [False, True])
def test_sparse_supported_alternative_basis_retains_scope_and_freshness(parent, include_source):
    app, _, _, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        row = session.get(ClaimVersion, selections[1]["id"])
        content = deepcopy(row.content)
        content.pop("reference_ids")
        content["support_bases"] = [{"reference_ids": [selections[0]["id"]]}]
        content["time_sensitive"] = False
        row.content, row.sha256 = content, canonical_hash(content)
        original_hash = row.sha256
        selection = {**selections[1], "sha256": original_hash}
    chosen = ([selections[0]] if include_source else []) + [selection]
    with TestClient(app) as client:
        quote = preflight(client, parent, chosen)
        assert quote.status_code == 200, quote.text
        response, _ = start(client, parent, quote.json())
        assert response.status_code == 202, response.text
        child_id = response.json()["id"]
    with app.state.session_factory() as session:
        copied = claim_packet(session, session.get(Run, child_id), imported_only=True)[0]
        assert copied["reference_ids"] == []
        assert copied["evidence_status"] == "not_checked"
        entries = {entry.parent_id: entry.child_id for entry in session.scalars(select(ReuseRecord).where(ReuseRecord.run_id == child_id))}
        expected_bases = [{"reference_ids": [entries[selections[0]["id"]]]}] if include_source else []
        assert copied["support_bases"] == expected_bases
        assert session.get(ClaimVersion, content["id"]).content == content
        assert session.get(ClaimVersion, content["id"]).sha256 == original_hash
