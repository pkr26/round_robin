"""API-created live runs, fake providers, deterministic worker loss/config expiry.
Run: PYTHONPATH=backend/src:. .venv/bin/python /tmp/rr-audit2-concurrency/probe_retained_hold.py
Only a random disposable PostgreSQL schema is created and dropped. No upstream I/O.
"""
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from backend.tests.test_api import command, create, start
from backend.tests.test_provider_configuration import synthetic_configuration
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Base, BudgetOperation, Run, Step, utcnow
from round_robin.database.services import lock_policies
from round_robin.dispatcher.service import reconcile_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import BudgetDenied, operations_exposure
from round_robin.orchestration.configuration import verify_frozen_configuration
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step

class FakeProvider(SimulatedProvider):
    def conservative_bound(self, request):
        return Decimal('0.02')
    def generate(self, request):
        if request.stage == 'synthesis':
            raise SystemExit('synthetic process death after durable authorization')
        return replace(super().generate(request), cost_usd=Decimal('0.01'))

base_url = 'postgresql+psycopg://round_robin:round_robin_local@127.0.0.1:55432/round_robin_test'
admin = create_engine(base_url)
schema = 'audit2_hold_' + uuid4().hex
with admin.begin() as conn:
    conn.execute(text(f'CREATE SCHEMA "{schema}"'))
app = None
output = {'schema': schema, 'no_external_calls': True}
try:
    with TemporaryDirectory(prefix='rr-audit2-hold-') as temp, ExitStack() as stack:
        root = Path(temp)
        now = [utcnow()]
        config = synthetic_configuration()
        # Valid funding: first run fits ($.40 hold plus $.04 actual calls).
        for field in ('owner_daily_usd', 'owner_monthly_usd'):
            config['aggregate_funding'][field] = '0.60'
        path = root / 'provider.json'
        path.write_text(json.dumps(config))
        settings = replace(Settings(), provider_config_file=path, paid_access_enabled=True,
                           artifact_directory=root/'artifacts', durability_control_directory=root/'control')
        stack.enter_context(patch('round_robin.orchestration.configuration.settings', settings))
        modules = ['api.preflight', 'api.events', 'orchestration.admission', 'orchestration.budget',
                   'orchestration.configuration', 'orchestration.controller', 'orchestration.circuits',
                   'worker.tasks', 'dispatcher.service', 'database.services']
        for name in modules:
            module = importlib.import_module('round_robin.' + name)
            if hasattr(module, 'utcnow'):
                stack.enter_context(patch.object(module, 'utcnow', lambda: now[0]))
        url = make_url(base_url).update_query_dict({'options': f'-csearch_path={schema}'})
        app = create_app(url.render_as_string(hide_password=False), settings=settings)
        Base.metadata.create_all(app.state.engine)
        factory = app.state.session_factory
        registry = {p: FakeProvider(p) for p in ['openai', 'anthropic', 'google']}
        with TestClient(app) as client:
            meeting = create(client)
            with factory.begin() as session:
                for policy in lock_policies(session, 'owner_local'):
                    policy.spending_locked = False
                    policy.limit_usd = '10'
                    policy.metadata_json = {'daily_usd': '10', 'monthly_usd': '50'}
            quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1, mode='live'))
            assert quote.status_code == 200 and not quote.json()['blockers'], quote.text
            started = start(client, meeting, quote.json())
            assert started.status_code == 202, started.text
            rid = started.json()['id']
            # Execute valid intake + three proposals, then crash in synthesis.
            crashed = False
            for _ in range(20):
                with factory.begin() as session:
                    advance_run(session, lock_work_run(session, rid))
                with factory() as session:
                    pending = list(session.scalars(select(Step.id).where(Step.run_id == rid, Step.status == 'pending')))
                for sid in pending:
                    try:
                        execute_step(sid, session_factory=factory, adapter_registry=registry)
                    except SystemExit:
                        crashed = True
                        break
                if crashed:
                    break
            assert crashed
            now[0] += timedelta(seconds=151)
            reconcile_once(session_factory=factory)
            snapshot = client.get(f'/api/runs/{rid}/snapshot').json()
            with factory() as session:
                old = session.get(Run, rid)
                hold = session.get(BudgetOperation, 'finish_' + rid)
                output['after_worker_loss'] = {'status': old.status, 'hold_status': hold.status,
                    'hold_usd': hold.reserved_usd, 'retry_stages': snapshot['retryable_stages'],
                    'active_started_at': str(old.active_started_at)}
                assert old.status == 'completed_with_limitations' and hold.status == 'held'
            # Original provider configuration is now irreversibly expired.
            now[0] += timedelta(days=2)
            for _ in range(3):
                reconcile_once(session_factory=factory)
            with factory() as session:
                old = session.get(Run, rid)
                try:
                    verify_frozen_configuration(old)
                    raise AssertionError('expired configuration unexpectedly eligible')
                except BudgetDenied as exc:
                    output['expired_run_denial'] = str(exc)
                hold = session.get(BudgetOperation, 'finish_' + rid)
                output['after_expiry'] = {'hold_status': hold.status, 'hold_usd': hold.reserved_usd,
                    'daily_exposure': str(operations_exposure(list(session.scalars(select(BudgetOperation))), now=now[0], period='daily'))}
                expected = old.version
            cancelled = client.post(f'/api/runs/{rid}/cancel', json=command(expected_run_version=expected))
            output['cancel_on_terminal_status'] = cancelled.json()['status']
            # Refresh operator config for a fresh, explicitly accepted run.
            config['expires_at'] = (now[0] + timedelta(hours=1)).isoformat()
            path.write_text(json.dumps(config))
            next_meeting = create(client)
            quote = client.post(f"/api/meetings/{next_meeting['id']}/preflight", json=command(expected_draft_revision=1, mode='live'))
            assert quote.status_code == 200 and not quote.json()['blockers'], quote.text
            next_run = start(client, next_meeting, quote.json())
            assert next_run.status_code == 202, next_run.text
            with factory.begin() as session:
                advance_run(session, lock_work_run(session, next_run.json()['id']))
            with factory() as session:
                newer = session.get(Run, next_run.json()['id'])
                output['new_run'] = {'status': newer.status, 'stop_reason': newer.stop_reason}
                assert newer.stop_reason == 'owner_daily_budget_exhausted'
            # No source fixes. Demonstrate removing only the obsolete unused hold
            # would make the exact same funding policy sufficient ($.02 + $.40).
            output['expected_new_run_exposure_if_unused_hold_released'] = '0.42'
            output['owner_daily_ceiling'] = '0.60'
finally:
    if app:
        app.state.engine.dispose()
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()
    output['schema_dropped'] = True
Path('/tmp/rr-audit2-concurrency/retained_hold_result.json').write_text(json.dumps(output, indent=2))
print(json.dumps(output, indent=2))
