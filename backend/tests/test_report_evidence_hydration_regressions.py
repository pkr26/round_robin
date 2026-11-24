from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from round_robin.database.models import Event, EvidenceRecord, ReportVersion, Run
from round_robin.fixtures import sample_brief
from sqlalchemy import text

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


@pytest.mark.parametrize(
    "damage",
    [
        "absent",
        "duplicate",
        "foreign_owner",
        "foreign_meeting",
        "foreign_run",
        "after_cutoff",
        "invalid_type",
        "oversized",
        "blank",
    ],
)
def test_bad_selected_scope_is_unavailable_and_sanitized(artifacts, damage):
    app, client, _, run_id, report = artifacts
    other_run_id = None
    if damage == "foreign_run":
        meeting = client.post(
            "/api/meetings",
            json={
                "command_id": str(uuid4()),
                "brief": sample_brief().model_dump(mode="json"),
            },
        ).json()
        preflight = client.post(
            f"/api/meetings/{meeting['id']}/preflight",
            json={
                "command_id": str(uuid4()),
                "expected_draft_revision": 1,
                "mode": "simulated",
            },
        ).json()
        other_run_id = client.post(
            f"/api/meetings/{meeting['id']}/runs",
            json={
                "command_id": str(uuid4()),
                "preflight_id": preflight["id"],
                "expected_draft_revision": 1,
            },
        ).json()["id"]
    with app.state.session_factory.begin() as session:
        saved = session.get(ReportVersion, report["id"])
        source = session.get(EvidenceRecord, report["evidence_ids"][0])
        value = deepcopy(saved.content)
        if damage == "absent":
            value["evidence_ids"].append("missing_id")
        elif damage == "duplicate":
            value["evidence_ids"].append(value["evidence_ids"][0])
        elif damage == "foreign_owner":
            source.owner_id = "foreign_owner"
        elif damage == "foreign_meeting":
            source.meeting_id = "foreign_meeting"
        elif damage == "foreign_run":
            source.run_id = other_run_id
        elif damage == "after_cutoff":
            source.created_at = saved.created_at + timedelta(seconds=1)
        elif damage == "invalid_type":
            value["evidence_ids"] = ["PRIVATE_SENTINEL", {"no": "id"}]
        elif damage == "oversized":
            value["evidence_ids"] = ["e" * 97]
        elif damage == "blank":
            value["evidence_ids"] = [""]
        saved.content = value
    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 503, (damage, response.status_code, response.text)
    assert response.json()["error"]["code"] == "saved_result_unavailable"
    assert "PRIVATE_SENTINEL" not in response.text


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("depth", ["invalid", "deep"])
def test_unreferenced_bad_content_only_affects_legacy_implicit_scope(
    artifacts, legacy, depth
):
    app, client, meeting_id, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        row = session.get(ReportVersion, report["id"])
        if legacy:
            body = deepcopy(row.content)
            del body["evidence_ids"]
            row.content = body
        session.execute(
            text(
                "INSERT INTO evidence (id,run_id,meeting_id,owner_id,content,sha256,created_at) VALUES (:id,:run,:meeting,:owner,:raw,:sha,:created)"
            ),
            {
                "id": "evidence_unreferenced",
                "run": run_id,
                "meeting": meeting_id,
                "owner": "owner_local",
                "raw": '{"PRIVATE_SENTINEL":'
                if depth == "invalid"
                else "[" * 10000 + "0" + "]" * 10000,
                "sha": "a" * 64,
                "created": row.created_at - timedelta(seconds=1),
            },
        )
    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == (503 if legacy else 200), response.text
    if not legacy:
        assert response.json() == report
    assert "PRIVATE_SENTINEL" not in response.text


@pytest.mark.parametrize("total", [24, 25])
def test_legacy_scope_bound_and_sorted_identity_order(artifacts, total):
    app, client, meeting_id, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        saved = session.get(ReportVersion, report["id"])
        body = deepcopy(saved.content)
        del body["evidence_ids"]
        saved.content = body
        source = session.get(EvidenceRecord, report["evidence_ids"][0])
        ids = list(report["evidence_ids"])
        for i in range(total - len(ids)):
            identity = f"evidence_{total - i:03}"
            content = {**deepcopy(source.content), "id": identity}
            session.add(
                EvidenceRecord(
                    id=identity,
                    run_id=run_id,
                    meeting_id=meeting_id,
                    owner_id="owner_local",
                    sha256=source.sha256,
                    content=content,
                    created_at=saved.created_at - timedelta(seconds=1),
                )
            )
            ids.append(identity)
    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == (200 if total == 24 else 503), response.text
    if total == 24:
        assert response.json()["evidence_ids"] == sorted(ids)


@pytest.mark.parametrize("past_cutoff", [False, True])
def test_committed_event_validation_still_respects_cutoff(artifacts, past_cutoff):
    app, client, _, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        row = session.get(ReportVersion, report["id"])
        sequence = row.event_cutoff - 1 if past_cutoff else row.event_cutoff + 1
        session.add(
            Event(
                id="event_" + uuid4().hex,
                run_id=run_id,
                sequence=sequence,
                event_type="content.committed",
                payload={"step_id": ["PRIVATE_SENTINEL"]},
            )
        )
        session.get(Run, run_id).event_sequence = max(0, sequence)
    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == (503 if past_cutoff else 200), response.text
    assert "PRIVATE_SENTINEL" not in response.text
