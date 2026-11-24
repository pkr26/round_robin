from copy import deepcopy
from uuid import uuid4

from round_robin.database.models import Command
from round_robin.database.visual_models import DesignVersion

from backend.tests.test_visual import visual_app as visual_app  # noqa: PLC0414


def test_legacy_native_command_receipt_hides_both_private_paths_without_rewriting_history(
    visual_app, monkeypatch
):
    app, client, meeting, _ = visual_app
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED", "1")
    design = client.post(
        f"/api/meetings/{meeting['id']}/design-sessions",
        json={
            "command_id": str(uuid4()),
            "mode": "review_only",
            "brief": {
                "purpose": "Review source",
                "audience": "Owner",
                "domain": "structured_diagram",
                "width": 500,
                "height": 200,
            },
            "maximum_usd": "0",
        },
    ).json()
    command_id = str(uuid4())
    body = {
        "command_id": command_id,
        "expected_revision": design["revision"],
        "diagram": {
            "width": 500,
            "height": 200,
            "nodes": [{"id": "node_a", "label": "Saved source", "x": 20, "y": 20}],
            "edges": [],
        },
    }
    response = client.post(
        f"/api/design-sessions/{design['id']}/native-diagram", json=body
    )
    assert response.status_code == 201, response.text
    version_id = response.json()["versions"][0]["id"]
    with app.state.session_factory.begin() as session:
        version = session.get(DesignVersion, version_id)
        receipt = session.get(Command, ("owner_local", command_id))
        saved = deepcopy(receipt.result)
        saved["versions"][0]["provenance"] = deepcopy(version.provenance)
        assert (
            saved["versions"][0]["provenance"]["svg_path"]
            and saved["versions"][0]["provenance"]["source_path"]
        )
        receipt.result = saved
    response = client.get(f"/api/commands/{command_id}")
    assert response.status_code == 200, response.text
    actual = response.json()["result"]["versions"][0]["provenance"]
    assert "svg_path" not in actual and "source_path" not in actual
    assert actual["svg_hash"] == saved["versions"][0]["provenance"]["svg_hash"]
    assert actual["source_hash"] == saved["versions"][0]["provenance"]["source_hash"]
    with app.state.session_factory() as session:
        assert session.get(Command, ("owner_local", command_id)).result == saved
