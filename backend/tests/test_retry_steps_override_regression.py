"""A caller's explicit step set controls manual retry eligibility."""

from datetime import timedelta

from backend.tests.test_audit_runtime_fixes import retained_terminal_run
from backend.tests.test_orchestration import factory
from round_robin.database.models import Run, utcnow
from round_robin.orchestration.policies import retryable_steps


def test_retry_uses_explicit_empty_step_set(factory):
    run_id = retained_terminal_run(factory, expires_at=utcnow() + timedelta(hours=1))
    with factory() as session:
        run = session.get(Run, run_id)
        eligible = retryable_steps(session, run)
        assert len(eligible) == 1
        assert retryable_steps(session, run, steps=eligible) == eligible
        assert retryable_steps(session, run, steps=[]) == []
