"""Manual retry counts authorized model calls that are absent from step counters."""
from datetime import timedelta

from backend.tests.test_audit_runtime_fixes import retained_terminal_run
from backend.tests.test_orchestration import factory
from round_robin.database.models import BudgetOperation, Run, utcnow
from round_robin.orchestration.policies import retryable_steps


def test_retry_uses_ledger_count_when_it_exceeds_step_count(factory):
    run_id = retained_terminal_run(factory, expires_at=utcnow() + timedelta(hours=1))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "limits": {**run.configuration["limits"], "max_requests": 3}}
        assert len(retryable_steps(session, run)) == 1
        session.add(BudgetOperation(id="another_unknown_model_call", run_id=run.id, owner_id=run.owner_id,
                                    kind="model", status="unknown", reserved_usd="0.02",
                                    metadata_json={"model_call": True}))
        session.flush()
        assert retryable_steps(session, run) == []
