"""Manual retry honors the run's configured request limit."""

from datetime import timedelta

from backend.tests.test_audit_runtime_fixes import retained_terminal_run
from backend.tests.test_orchestration import factory
from round_robin.database.models import Run, utcnow
from round_robin.orchestration.policies import retryable_steps


def test_retry_honors_lower_configured_request_limit(factory):
    run_id = retained_terminal_run(factory, expires_at=utcnow() + timedelta(hours=1))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        assert len(retryable_steps(session, run)) == 1
        run.configuration = {**run.configuration, "limits": {**run.configuration["limits"], "max_requests": 1}}
        assert retryable_steps(session, run) == []
