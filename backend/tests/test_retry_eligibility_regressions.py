"""Manual retry must respect terminal state and the original live deadline."""

from datetime import timedelta

import pytest

from backend.tests.test_audit_runtime_fixes import retained_terminal_run
from backend.tests.test_orchestration import factory
from round_robin.database.models import Run, utcnow
from round_robin.orchestration.policies import retryable_steps


@pytest.mark.parametrize("status", ["failed", "completed_with_limitations"])
def test_terminal_retry_abandonment_blocks_an_otherwise_eligible_step(factory, status):
    run_id = retained_terminal_run(factory, expires_at=utcnow() + timedelta(hours=1))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.status = status
        assert len(retryable_steps(session, run)) == 1
        run.configuration = {**run.configuration, "retry_abandoned": True}
        assert retryable_steps(session, run) == []


def test_live_retry_expires_at_the_exact_frozen_deadline(factory, monkeypatch):
    now = utcnow()
    run_id = retained_terminal_run(factory, expires_at=now + timedelta(hours=1))
    monkeypatch.setattr("round_robin.orchestration.policies.utcnow", lambda: now)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "expires_at": (now + timedelta(microseconds=1)).isoformat()}
        assert len(retryable_steps(session, run)) == 1
        run.configuration = {**run.configuration, "expires_at": now.isoformat()}
        assert retryable_steps(session, run) == []
