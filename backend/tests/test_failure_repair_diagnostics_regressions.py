from datetime import timedelta

import pytest

from round_robin.database.models import Run, Step, utcnow
from round_robin.providers.base import ProviderFailure
from round_robin.worker import tasks
from backend.tests.test_orchestration import factory as factory, seed  # noqa: F401


@pytest.mark.parametrize("mode", ["empty", "populated"])
def test_retry_saves_bounded_repair_diagnostics(factory, mode):
    run_id = seed(factory)
    now = utcnow()
    diagnostics = tuple({"code": f"issue_{i}", "path": f"$.items[{i}]"} for i in range(13)) if mode == "populated" else ()
    with factory.begin() as session:
        run = session.get(Run, run_id)
        step = Step(id="failed_step", run_id=run_id, stage="intake", round=1,
                    role_slot_id="moderator", provider="openai", status="authorized",
                    calls=1, infrastructure_claims=1, lease_owner="previous",
                    lease_expires_at=now - timedelta(seconds=1))
        session.add(step)
        session.flush()
        tasks._settle_failure(session, run, step, ProviderFailure("schema_invalid", retryable=True, diagnostics=diagnostics))
    with factory() as session:
        run = session.get(Run, run_id)
        saved = run.configuration["step_dispatch"]["failed_step"]["repair_diagnostics"]
        assert saved == (list(diagnostics[:12]) if diagnostics else [{"code": "schema_invalid", "path": "$"}])
