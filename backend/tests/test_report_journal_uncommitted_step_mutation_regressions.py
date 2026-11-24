"""Report reads ignore steps outside the saved contribution cutoff."""

from round_robin.database.models import Step

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import (
    _read_current_report,
    _saved_with_journal,
)


def test_saved_report_ignores_uncommitted_later_step(factory, tmp_path):
    run_id = _saved_with_journal(factory)
    before = _read_current_report(factory, tmp_path, run_id)
    assert before.status_code == 200, before.text
    with factory.begin() as session:
        # Model a later, uncommitted step without claiming the normal writer
        # currently schedules this step after a completed report.
        session.add(Step(
            id="later_uncommitted_step",
            run_id=run_id,
            stage="proposal",
            round=999,
            stage_iteration=0,
            role_slot_id="later_probe",
            status="pending",
        ))
    after = _read_current_report(factory, tmp_path, run_id)
    assert after.status_code == 200, after.text
    assert after.json() == before.json()
