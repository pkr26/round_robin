from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import FakeImageAdapter, image_factory as image_factory, seed_image  # noqa: PLC0414
from backend.tests.test_orchestration import seed
from round_robin.database.models import Attempt, BudgetOperation, Step, utcnow
from round_robin.database.visual_models import DesignOperation, DesignSession
from round_robin.orchestration.admission import capacity_delay_seconds, lock_work_run, provider_capacity_available
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.image_tasks import _capacity, execute_image_operation
from round_robin.worker.tasks import execute_step


@pytest.mark.parametrize('provider', ['unknown_provider', 'google', 'owner_mismatch'])
@pytest.mark.parametrize('expired', [False, True])
def test_unscoped_provider_blocks_text_and_image_only_until_bounded_expiry(image_factory, image_configuration, tmp_path, provider, expired):
    design_id, image_id = seed_image(image_factory, image_configuration)
    run_id = seed(image_factory)
    now = utcnow()
    with image_factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        target_step = session.scalar(select(Step).where(Step.run_id == run_id, Step.status == 'pending')).id
        session.add(Step(id='step_lost', run_id=run_id, stage='proposal', role_slot_id='lost', provider='openai', status='skipped',
                         lease_owner='step_token' if provider == 'owner_mismatch' else None,
                         lease_expires_at=now + timedelta(days=1) if expired else now - timedelta(seconds=60)))
        session.flush()
        session.add(Attempt(id='attempt_lost', run_id=run_id, step_id='step_lost', provider='openai' if provider == 'owner_mismatch' else provider, status='authorized',
                            lease_owner='attempt_token' if provider == 'owner_mismatch' else None,
                            authorized_at=now-timedelta(seconds=151 if expired else 0), reserved_usd='0.10', configuration={}))
    with image_factory() as session:
        assert provider_capacity_available(session, 'google', mandatory=True) is expired
        assert _capacity(session, session.get(DesignSession, design_id), session.get(DesignOperation, image_id)) is expired
        delay = capacity_delay_seconds(session)
        assert delay == 0 if expired else 145 <= delay <= 150
    if not expired:
        text_provider = SimulatedProvider('openai')
        image_provider = FakeImageAdapter('openai', image_configuration, image_factory)
        text_result = execute_step(target_step, session_factory=image_factory, adapter_registry={'openai': text_provider})
        image_result = execute_image_operation(image_id, session_factory=image_factory,
            provider_registry={'openai': image_provider}, artifact_directory=tmp_path)
        assert text_result['status'] == image_result['status'] == 'capacity_wait'
        assert text_provider.calls == image_provider.calls == 0
        with image_factory() as session:
            assert session.get(BudgetOperation, image_id) is None
            assert session.get(Attempt, 'attempt_lost').status == 'authorized'
