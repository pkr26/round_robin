"""Reproduce protected critic time consumption with temporary DB/fake providers."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path('/Users/pradeepreddy/Desktop/round_robin')
sys.path[:0] = [str(ROOT), str(ROOT / 'backend/src')]
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from backend.tests.test_image_provider import image_configuration
from backend.tests.test_image_worker import seed_image, FakeImageAdapter
from round_robin.database.models import Base, BudgetOperation
from round_robin.database.visual_models import DesignOperation, DesignSession
from round_robin.worker import image_tasks

with tempfile.TemporaryDirectory(prefix='rr-review-') as folder, pytest.MonkeyPatch.context() as mp:
    directory = Path(folder).resolve()  # Avoid macOS /var symlink in asset paths.
    configuration = image_configuration.__wrapped__(directory, mp)
    engine = create_engine(f'sqlite:///{directory / "db.sqlite"}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    design_id, operation_id = seed_image(factory, configuration)
    first = image_tasks.execute_image_operation(
        operation_id, session_factory=factory,
        provider_registry={'openai': FakeImageAdapter('openai', configuration, factory, fail=True)},
        artifact_directory=directory / 'artifacts')
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        admitted = design.state['admitted_at']
        prior = session.get(DesignOperation, operation_id)
        session.add(DesignOperation(id='generation_retry', session_id=design_id, provider='openai',
            payload={**prior.payload, 'status': 'queued', 'dispatch_status': 'pending',
                     'lease_owner': None, 'lease_expires_at': None}))
    clock = [datetime.fromtimestamp(admitted + 1500, timezone.utc)]
    mp.setattr(image_tasks, 'utcnow', lambda: clock[0])
    allowance = []

    def consume_time(packet, authorization):
        allowance.append(packet['timeout_seconds'])
        clock[0] += timedelta(seconds=274)

    generation = image_tasks.execute_image_operation(
        'generation_retry', session_factory=factory,
        provider_registry={'openai': FakeImageAdapter('openai', configuration, factory, callback=consume_time)},
        artifact_directory=directory / 'artifacts')
    with factory() as session:
        review_ids = [operation.id for operation in session.scalars(select(DesignOperation))
                      if operation.payload.get('kind') == 'vision']
    registry = {provider: FakeImageAdapter(provider, configuration, factory)
                for provider in ['anthropic', 'google']}
    critics = [image_tasks.execute_image_operation(identity, session_factory=factory,
        provider_registry=registry, artifact_directory=directory / 'artifacts') for identity in review_ids]
    with factory() as session:
        held = session.get(BudgetOperation, f'visualfinish_{design_id}').reserved_usd
    result = {'probe': 'generation_consumes_critic_time', 'initial_failure': first,
              'remaining_seconds_before_retry': 300, 'generation_timeout_seconds': allowance[0],
              'simulated_generation_elapsed_seconds': 274, 'generation_result': generation,
              'critic_results': critics, 'critic_calls': {key: value.calls for key, value in registry.items()},
              'retained_review_hold_usd': held, 'real_provider_calls': 0}
    print(json.dumps(result, indent=2))
    assert allowance == [275.0]
    assert generation['status'] == 'persisted'
    assert len(critics) == 2 and all(item['status'] == 'blocked' for item in critics)
    assert not any(adapter.calls for adapter in registry.values())
    engine.dispose()
