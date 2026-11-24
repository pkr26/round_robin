"""Deterministic offline audit probes. No product source modification or sockets."""
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from backend.tests.test_orchestration import seed
from round_robin.database.models import Base, Run, Step, Outbox, Attempt, ReportVersion, utcnow
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import aware
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step

class RateLimited(SimulatedProvider):
    def __init__(self, provider, delay):
        super().__init__(provider)
        self.delay = delay
        self.invocations = []
    def generate(self, request):
        self.invocations.append(time.monotonic())
        raise ProviderFailure('rate_limit', retryable=True, retry_after=self.delay, may_have_sent=False, cost_usd=0)

def setup(path):
    engine = create_engine(f'sqlite:///{path}', connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    run_id = seed(factory)
    with factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        run.stage = 'proposals'
        advance_run(session, run)
        steps = {s.provider:s.id for s in session.scalars(select(Step))}
    return engine, factory, run_id, steps

results = {}
with tempfile.TemporaryDirectory(prefix='round-robin-algorithm-audit-') as directory:
    engine, factory, run_id, steps = setup(Path(directory) / 'backoff.sqlite')
    provider = RateLimited('openai', 120)
    first = execute_step(steps['openai'], session_factory=factory, adapter_registry={'openai':provider})
    with factory() as session:
        future_due = max(aware(item.due_at) for item in session.scalars(select(Outbox).where(Outbox.step_id==steps['openai'])))
        before = session.get(Step, steps['openai']).status
    duplicate = execute_step(steps['openai'], session_factory=factory, adapter_registry={'openai':provider})
    with factory() as session:
        results['retry_after_bypassed_by_duplicate'] = {'requested_retry_after_seconds':120, 'step_before_duplicate':before, 'first_result':first, 'duplicate_result':duplicate, 'actual_seconds_between_calls':provider.invocations[1]-provider.invocations[0], 'scheduled_retry_still_future':future_due > utcnow(), 'attempt_count':len(list(session.scalars(select(Attempt)))), 'step_after_duplicate':session.get(Step, steps['openai']).status}
    assert results['retry_after_bypassed_by_duplicate']['actual_seconds_between_calls'] < 5
    assert results['retry_after_bypassed_by_duplicate']['attempt_count'] == 2
    engine.dispose()

    engine, factory, run_id, steps = setup(Path(directory) / 'reserve.sqlite')
    provider = RateLimited('openai', 500)
    registry = {'openai':provider, 'anthropic':SimulatedProvider('anthropic'), 'google':SimulatedProvider('google')}
    for p in ('openai','anthropic','google'):
        execute_step(steps[p],session_factory=factory,adapter_registry=registry)
    with factory() as session, session.begin():
        run=session.get(Run,run_id)
        before={'stage':run.stage,'status':run.status,'reserved_finish_seconds':run.configuration['finish_policy']['reserved_active_seconds'], 'proposal_statuses':{s.provider:s.status for s in session.scalars(select(Step))}}
        # Advance the deterministic active-time state to the accepted retry's due time.
        run.active_seconds=500
        run.active_started_at=None
    retry=execute_step(steps['openai'],session_factory=factory,adapter_registry=registry)
    with factory() as session:
        synthesis=session.scalar(select(Step.id).where(Step.stage=='synthesis'))
    synthesis_result=execute_step(synthesis,session_factory=factory,adapter_registry=registry)
    with factory() as session:
        run=session.get(Run,run_id)
        report=session.scalar(select(ReportVersion)).content
        results['retry_wait_consumes_mandatory_finish_time']={'accepted_wait_seconds':500,'before_wait':before,'proposal_after_wait':retry,'synthesis_result':synthesis_result,'final_status':run.status,'stop_reason':run.stop_reason,'synthesis_model_calls':session.get(Step,synthesis).calls,'report_readiness':report['readiness'],'report_recommendation':report['recommendation']}
    assert results['retry_wait_consumes_mandatory_finish_time']['stop_reason']=='finish_time_reserve'
    assert results['retry_wait_consumes_mandatory_finish_time']['synthesis_model_calls']==0
    engine.dispose()
print(json.dumps(results, indent=2))
