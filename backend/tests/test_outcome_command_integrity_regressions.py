"""Outcome commands bind immutable reports to the current owner and meeting."""
from copy import deepcopy
from uuid import uuid4

import pytest
from round_robin.api.auth import require_owner
from round_robin.database.models import (
    Command,
    Meeting,
    Outcome,
    Owner,
    PreflightRecord,
    ReportVersion,
    Run,
    Step,
)
from sqlalchemy import func, select

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


@pytest.fixture
def scoped(artifacts):
    app, client, meeting_id, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        current = session.get(Run, run_id)
        preflight = session.get(PreflightRecord, current.preflight_id)
        session.add(Owner(id="owner_foreign"))
        session.flush()
        session.add_all([Meeting(id="meeting_other", owner_id=current.owner_id, title="Synthetic other"),
                         Meeting(id="meeting_foreign", owner_id="owner_foreign", title="Synthetic foreign")])
        session.flush()
        for suffix, mid, owner in [("other", "meeting_other", current.owner_id),
                                   ("foreign", "meeting_foreign", "owner_foreign"),
                                   ("mismatched_owner", meeting_id, "owner_foreign"),
                                   ("same", meeting_id, current.owner_id)]:
            pid = f"preflight_synthetic_{suffix}"
            pfields = {column.name: deepcopy(getattr(preflight, column.name)) for column in PreflightRecord.__table__.columns
                       if column.name not in {"id", "meeting_id", "owner_id", "consumed_by_run_id"}}
            session.add(PreflightRecord(id=pid, meeting_id=mid, owner_id=owner, **pfields))
            session.flush()
            fields = {column.name: deepcopy(getattr(current, column.name)) for column in Run.__table__.columns
                      if column.name not in {"id", "meeting_id", "owner_id", "preflight_id"}}
            session.add(Run(id=f"run_synthetic_{suffix}", meeting_id=mid, owner_id=owner, preflight_id=pid, **fields))
            session.flush()
            session.add(ReportVersion(id=f"report_synthetic_{suffix}", run_id=f"run_synthetic_{suffix}",
                                      version=2 if suffix == "same" else 1, event_cutoff=0, content=deepcopy(report)))
        session.flush()
    return app, client, meeting_id, run_id, report


def snapshot(app):
    with app.state.session_factory() as session:
        return (session.scalar(select(func.count()).select_from(Outcome)),
                session.scalar(select(func.count()).select_from(Command)),
                [(row.id, row.version, deepcopy(row.content)) for row in session.scalars(select(ReportVersion).order_by(ReportVersion.id))],
                [(row.id, row.calls) for row in session.scalars(select(Step).order_by(Step.id))])


def command(run_id, version=1, **fields):
    return {"command_id": str(uuid4()), "run_id": run_id, "report_version": version, "choice": "accepted", **fields}


@pytest.mark.parametrize("case,expected", [("missing_run", 404), ("other_meeting", 404), ("foreign_owner", 404), ("mismatched_owner", 404),
                                             ("missing_version", 409), ("other_run_version", 409)])
def test_scope_rejection_has_no_outcome_or_command_commit(scoped, case, expected):
    app, client, meeting_id, run_id, _ = scoped
    body = command(run_id)
    if case == "missing_run": body["run_id"] = "run_missing"
    if case == "other_meeting": body["run_id"] = "run_synthetic_other"
    if case == "foreign_owner": body["run_id"] = "run_synthetic_foreign"
    if case == "mismatched_owner": body["run_id"] = "run_synthetic_mismatched_owner"
    if case in {"missing_version", "other_run_version"}: body["report_version"] = 2
    if case == "missing_version": body["report_version"] = 3
    before = snapshot(app)
    response = client.post(f"/api/meetings/{meeting_id}/outcomes", json=body)
    assert response.status_code == expected, response.text
    assert snapshot(app) == before
    with app.state.session_factory() as session:
        assert session.get(Command, ("owner_local", body["command_id"])) is None


def test_revision_is_per_meeting_and_replay_does_not_mutate_report(scoped):
    app, client, meeting_id, run_id, _ = scoped
    with app.state.session_factory.begin() as session:
        session.add(Outcome(id="outcome_other", owner_id="owner_local", meeting_id="meeting_other",
                            run_id="run_synthetic_other", revision=8, payload={"synthetic": True}))
    before = snapshot(app)
    first = command(run_id, choice="modified", reason="A synthetic revision", observed_outcome="Pending")
    response = client.post(f"/api/meetings/{meeting_id}/outcomes", json=first)
    assert response.status_code == 201, response.text
    assert response.json()["revision"] == 1
    assert client.post(f"/api/meetings/{meeting_id}/outcomes", json=first).json() == response.json()
    conflict = client.post(f"/api/meetings/{meeting_id}/outcomes", json={**first, "choice": "rejected"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "command_conflict"
    second = client.post(f"/api/meetings/{meeting_id}/outcomes", json=command(run_id, choice="deferred"))
    assert second.status_code == 201 and second.json()["revision"] == 2
    listing = client.get(f"/api/meetings/{meeting_id}/outcomes")
    assert [row["revision"] for row in listing.json()["items"]] == [1, 2]
    after = snapshot(app)
    assert after[0] == before[0] + 2 and after[1] == before[1] + 2
    assert after[2:] == before[2:]


def test_other_owner_cannot_record_or_read_outcome(scoped):
    app, client, meeting_id, run_id, _ = scoped
    before = snapshot(app)
    app.dependency_overrides[require_owner] = lambda: "owner_foreign"
    try:
        assert client.post(f"/api/meetings/{meeting_id}/outcomes", json=command(run_id)).status_code == 404
        assert client.get(f"/api/meetings/{meeting_id}/outcomes").status_code == 404
    finally:
        app.dependency_overrides.pop(require_owner, None)
    assert snapshot(app) == before


def test_commit_failure_rolls_back_outcome_and_original_uuid_can_retry(scoped):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    app, client, meeting_id, run_id, _ = scoped
    before = snapshot(app)
    body = command(run_id, reason="Synthetic commit failure")
    def fail_commit(session):
        if any(isinstance(row, Outcome) for row in session.identity_map.values()):
            raise OperationalError("PRIVATE_SQL_SENTINEL", {}, Exception("private"))
    event.listen(app.state.session_factory, "before_commit", fail_commit)
    try:
        result = client.post(f"/api/meetings/{meeting_id}/outcomes", json=body)
    finally:
        event.remove(app.state.session_factory, "before_commit", fail_commit)
    assert result.status_code == 503 and "PRIVATE_SQL_SENTINEL" not in result.text
    assert snapshot(app) == before
    retry = client.post(f"/api/meetings/{meeting_id}/outcomes", json=body)
    assert retry.status_code == 201 and retry.json()["revision"] == 1
    assert client.post(f"/api/meetings/{meeting_id}/outcomes", json=body).json() == retry.json()
