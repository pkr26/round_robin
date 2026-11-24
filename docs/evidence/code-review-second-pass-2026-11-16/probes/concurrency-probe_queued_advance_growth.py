"""Reproduce durable advance-intent multiplication during valid queue waiting.
PYTHONPATH=backend/src:. .venv/bin/python /tmp/rr-audit2-concurrency/probe_queued_advance_growth.py
No provider or Redis calls: fake sender synchronously executes every broker delivery.
"""
from contextlib import ExitStack
from datetime import timedelta
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from backend.tests.test_orchestration import seed
from round_robin.database.models import Base, Outbox, Run, utcnow
from round_robin.database.services import lock_policies
from round_robin.dispatcher.service import dispatch_once, reconcile_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run

base_url='postgresql+psycopg://round_robin:round_robin_local@127.0.0.1:55432/round_robin_test'
admin=create_engine(base_url)
schema='audit2_queue_'+uuid4().hex
with admin.begin() as conn:conn.execute(text(f'CREATE SCHEMA "{schema}"'))
engine=None
output={'no_external_calls':True, 'schema':schema, 'observations':[]}
try:
    url=make_url(base_url).update_query_dict({'options':f'-csearch_path={schema}'})
    engine=create_engine(url)
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    with ExitStack() as stack:
        now=[utcnow()]
        for name in ['orchestration.admission','orchestration.budget','orchestration.controller','orchestration.configuration','dispatcher.service','database.services']:
            module=importlib.import_module('round_robin.'+name)
            if hasattr(module,'utcnow'):stack.enter_context(patch.object(module,'utcnow',lambda:now[0]))
        rid=seed(factory)
        # An ordinary operator drain stops admissions but keeps durable dispatch
        # enabled, as explicitly supported by admission.py and its regression test.
        with factory.begin() as session:
            deployment,_=lock_policies(session,'owner_test')
            deployment.metadata_json={'draining':True}
        delivered=[0]
        def sender(kind, payload):
            assert kind=='advance', kind
            with factory.begin() as session:
                advance_run(session,lock_work_run(session,payload))
            delivered[0]+=1
        for tick in range(91):
            reconcile_once(session_factory=factory)
            dispatch_once(session_factory=factory,sender=sender)
            if tick in [0,5,15,30,45,60,90]:
                with factory() as session:
                    counts=dict(session.execute(select(Outbox.status,func.count()).group_by(Outbox.status)).all())
                    output['observations'].append({'seconds':tick,'outbox':counts,'rows':sum(counts.values()),'delivered':delivered[0],'run_status':session.get(Run,rid).status})
            now[0]+=timedelta(seconds=1)
        assert output['observations'][-1]['rows']>1000
finally:
    if engine:engine.dispose()
    with admin.begin() as conn:conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()
    output['schema_dropped']=True
Path('/tmp/rr-audit2-concurrency/queued_advance_growth_result.json').write_text(json.dumps(output,indent=2))
print(json.dumps(output,indent=2))
