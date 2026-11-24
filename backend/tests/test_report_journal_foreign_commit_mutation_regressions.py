"""A committed contribution must belong to the saved report's run."""

from round_robin.database.models import Event, ReportVersion, Step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import (
    _read_current_report,
    _saved_with_journal,
)


def test_saved_report_rejects_foreign_committed_step(factory, tmp_path):
    run_id = _saved_with_journal(factory)
    other_id = _saved_with_journal(factory, suffix="foreign")
    with factory.begin() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        # Model a damaged saved ledger: the public writer never commits a
        # foreign pending step or replaces the report publication event.
        foreign = Step(
            id="foreign_pending_step",
            run_id=other_id,
            stage="proposal",
            round=999,
            stage_iteration=0,
            role_slot_id="foreign_probe",
            status="pending",
        )
        session.add(foreign)
        event_at_cutoff = session.scalar(select(Event).where(
            Event.run_id == run_id, Event.sequence == report.event_cutoff
        ))
        assert event_at_cutoff is not None and event_at_cutoff.event_type == "report.published"
        session.delete(event_at_cutoff)
        session.flush()
        session.add(Event(
            id="foreign_committed_probe",
            run_id=run_id,
            sequence=report.event_cutoff,
            event_type="content.committed",
            payload={"step_id": foreign.id},
        ))
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 503, response.text
