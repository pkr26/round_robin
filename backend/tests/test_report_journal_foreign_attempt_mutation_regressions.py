"""A saved report cannot authenticate a step with a foreign-run attempt."""
from copy import deepcopy

from round_robin.database.models import Attempt, Event, ReportVersion, Step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import (
    _read_current_report,
    _saved_with_journal,
)


def test_saved_report_rejects_attempt_from_foreign_run(factory, tmp_path):
    run_id = _saved_with_journal(factory)
    other_id = _saved_with_journal(factory, suffix="foreign")
    with factory.begin() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        committed = session.scalars(select(Event).where(
            Event.run_id == run_id,
            Event.event_type == "content.committed",
            Event.sequence <= report.event_cutoff,
        )).all()
        steps = [session.get(Step, event.payload["step_id"]) for event in committed]
        step = next(step for step in steps if step is not None and step.result_json and step.result_json.get("attempt_id"))
        original = session.get(Attempt, step.result_json["attempt_id"])
        assert original is not None and original.run_id == run_id
        # Coordinated corruption: the foreign attempt references the local step
        # and mirrors its successful response, while its own run remains foreign.
        result = deepcopy(step.result_json)
        result["attempt_id"] = "foreign_saved_attempt_probe"
        step.result_json = result
        session.add(Attempt(
            id=result["attempt_id"],
            run_id=other_id,
            step_id=step.id,
            provider=step.provider,
            status="succeeded",
            request_json=deepcopy(original.request_json),
            response_json=deepcopy(result),
        ))
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 503, response.text
