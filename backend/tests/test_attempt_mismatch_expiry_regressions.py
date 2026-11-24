from datetime import timedelta

import pytest

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Attempt, Step, utcnow
from round_robin.orchestration.admission import capacity_delay_seconds, provider_capacity_available


@pytest.mark.parametrize("mismatch", ["provider", "lease_owner"])
def test_expired_inconsistent_step_lease_cannot_shorten_recent_attempt(factory, mismatch):
    run_id = seed(factory)
    now = utcnow()
    with factory.begin() as session:
        session.add(Step(id="step_expired", run_id=run_id, stage="proposal", role_slot_id="candidate", provider="openai", status="authorized", lease_owner="lease_a", lease_expires_at=now-timedelta(seconds=1)))
        session.flush()
        session.add(Attempt(id="attempt_fresh", run_id=run_id, step_id="step_expired", provider="google" if mismatch == "provider" else "openai", status="authorized", lease_owner="lease_b" if mismatch == "lease_owner" else "lease_a", authorized_at=now, reserved_usd="0.10", configuration={}))
    with factory() as session:
        assert 145 <= capacity_delay_seconds(session) <= 150
        assert not provider_capacity_available(session, "google", mandatory=True)
