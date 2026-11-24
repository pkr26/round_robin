"""A saved report requires every committed attempt to match its step."""
from copy import deepcopy

import pytest
from round_robin.database.models import Attempt, Event, ReportVersion, Step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import (
    _read_current_report,
    _saved_with_journal,
)


@pytest.mark.parametrize("tamper", ["step", "provider", "status", "response", "stage", "role"])
def test_report_rejects_mismatched_committed_attempt(factory, tmp_path, tamper):
    run_id = _saved_with_journal(factory)
    before = _read_current_report(factory, tmp_path, run_id)
    assert before.status_code == 200, before.text
    with factory.begin() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        committed = session.scalars(select(Event).where(
            Event.run_id == run_id,
            Event.event_type == "content.committed",
            Event.sequence <= report.event_cutoff,
        )).all()
        steps = [session.get(Step, event.payload["step_id"]) for event in committed]
        step = next(step for step in steps if step is not None and step.result_json and step.result_json.get("attempt_id"))
        attempt = session.get(Attempt, step.result_json["attempt_id"])
        assert attempt is not None and attempt.step_id == step.id
        if tamper == "step":
            other = next(item for item in steps if item is not None and item.id != step.id)
            attempt.step_id = other.id
        elif tamper == "provider":
            attempt.provider = "anthropic" if step.provider != "anthropic" else "openai"
        elif tamper == "status":
            attempt.status = "failed"
        elif tamper == "response":
            altered = deepcopy(attempt.response_json)
            altered["private_tampered_response"] = "PRIVATE_ATTEMPT_SENTINEL"
            attempt.response_json = altered
        else:
            request = deepcopy(attempt.request_json)
            request[tamper] = "PRIVATE_ATTEMPT_SENTINEL"
            attempt.request_json = request
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 503, response.text
    assert "PRIVATE_ATTEMPT_SENTINEL" not in response.text
