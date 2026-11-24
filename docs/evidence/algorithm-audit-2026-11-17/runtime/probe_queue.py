"""Offline synthetic reproduction of resumption queue quota bypass."""
from datetime import timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import uuid4
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from backend.tests.test_orchestration import seed
from round_robin.api.events import clarify
from round_robin.configuration import Settings
from round_robin.database.models import Base, Run, utcnow
from round_robin.fixtures import sample_brief
from round_robin.schemas.application import Clarification

with tempfile.TemporaryDirectory(prefix='round-robin-queue-audit-') as directory:
    engine=create_engine(f'sqlite:///{Path(directory)/"queue.sqlite"}')
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    waiting=seed(factory,suffix='waiting')
    with factory() as session,session.begin():
        run=session.get(Run,waiting)
        run.status='awaiting_input'
        run.first_admitted_at=utcnow()
        run.input_expires_at=utcnow()+timedelta(hours=24)
    for index in range(3):
        seed(factory,suffix=f'queued_{index}')
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=Settings())),state=SimpleNamespace())
    with factory() as session:
        response=clarify(waiting,Clarification(command_id=uuid4(),expected_run_version=1,brief=sample_brief()),request,owner='owner_test',session=session)
    with factory() as session:
        queued=list(session.scalars(select(Run.id).where(Run.owner_id=='owner_test',Run.status=='queued')))
        result={'configured_owner_queued_limit':Settings().max_queued_per_owner,'queued_before':3,'clarification_status':response['status'],'queued_after':len(queued),'resumption_ticket_issued':bool(session.get(Run,waiting).resumption_expires_at)}
    assert result['queued_after']>result['configured_owner_queued_limit']
    print(json.dumps(result,indent=2))
    engine.dispose()
