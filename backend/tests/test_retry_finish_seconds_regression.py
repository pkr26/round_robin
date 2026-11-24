"""Saved finish reserves determine whether manual retries fit."""

from datetime import timedelta

from backend.tests.test_audit_runtime_fixes import retained_terminal_run
from backend.tests.test_orchestration import factory
from round_robin.database.models import Run, utcnow
from round_robin.orchestration.policies import retryable_steps


def test_synthesis_retry_uses_configured_finish_seconds(factory, monkeypatch):
    run_id = retained_terminal_run(factory, expires_at=utcnow() + timedelta(hours=1))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {
            **run.configuration,
            "finish_policy": {**run.configuration["finish_policy"], "reserved_active_seconds": 100},
        }
        monkeypatch.setattr("round_robin.orchestration.policies.remaining_active", lambda run: 75)
        assert retryable_steps(session, run) == []
        monkeypatch.setattr("round_robin.orchestration.policies.remaining_active", lambda run: 76)
        assert len(retryable_steps(session, run)) == 1
