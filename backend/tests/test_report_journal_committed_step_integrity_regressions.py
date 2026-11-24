"""Journal-backed saved reports reject damaged committed step records."""

import pytest
from round_robin.database.models import Event, ReportVersion, Step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import (
    _read_current_report,
    _saved_with_journal,
)


@pytest.mark.parametrize(
    ("status", "result"),
    [
        ("pending", None),
        ("succeeded", None),
        ("succeeded", ["PRIVATE_CORRUPT_STEP_SENTINEL"]),
        ("succeeded", {}),
        ("succeeded", {"output": "PRIVATE_CORRUPT_STEP_SENTINEL"}),
        ("failed", {"output": {}}),
    ],
)
def test_journal_backed_report_rejects_invalid_committed_step(factory, tmp_path, status, result):
    run_id = _saved_with_journal(factory)
    before = _read_current_report(factory, tmp_path, run_id)
    assert before.status_code == 200, before.text
    with factory.begin() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        # Corrupt persisted history; the normal writer commits only a succeeded
        # step with a dictionary result and dictionary output.
        session.add(Step(
            id="bad_committed_step", run_id=run_id, stage="proposal", round=999,
            stage_iteration=0, role_slot_id="bad_committed_step", status=status,
            result_json=result,
        ))
        event = session.scalar(select(Event).where(
            Event.run_id == run_id, Event.sequence == report.event_cutoff
        ))
        assert event is not None and event.event_type == "report.published"
        session.delete(event)
        session.flush()
        session.add(Event(
            id="bad_committed_event", run_id=run_id, sequence=report.event_cutoff,
            event_type="content.committed", payload={"step_id": "bad_committed_step"},
        ))
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 503, response.text
    assert "PRIVATE_CORRUPT_STEP_SENTINEL" not in response.text


def test_pre_journal_report_remains_readable(factory, tmp_path):
    run_id = _saved_with_journal(factory)
    with factory.begin() as session:
        # Model an old saved report without a discussion event journal.
        journal = list(session.scalars(select(Event).where(
            Event.run_id == run_id, Event.event_type == "discussion.obligation_recorded"
        )))
        assert journal
        for event in journal:
            session.delete(event)
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == run_id
