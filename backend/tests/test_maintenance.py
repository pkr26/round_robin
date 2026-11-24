from datetime import timedelta
import logging

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from round_robin.database.models import Base, Command, ExportRecord, Meeting, Owner, PreflightRecord, Run, Upload, utcnow
from round_robin.operations.maintenance import maintenance
from round_robin.reports.storage import ArtifactStore
from round_robin.security.logging import RedactAccessQueries


def test_retention_cleans_only_expired_temporary_inputs_and_regenerable_exports(tmp_path):
    now = utcnow(); engine = create_engine(f'sqlite:///{tmp_path / "maintenance.sqlite"}')
    Base.metadata.create_all(engine); factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = ArtifactStore(tmp_path / 'artifacts')
    old_key, old_hash = store.put(b'expired report'); new_key, new_hash = store.put(b'current report')
    with factory.begin() as session:
        session.add(Owner(id='owner_test')); session.flush()
        session.add(Meeting(id='meeting_test',owner_id='owner_test',title='Saved')); session.flush()
        session.add(PreflightRecord(id='preflight_test',meeting_id='meeting_test',owner_id='owner_test',draft_revision=1,draft_hash='a'*64,configuration_hash='b'*64,payload={},expires_at=now)); session.flush()
        session.add(Run(id='run_test',meeting_id='meeting_test',owner_id='owner_test',preflight_id='preflight_test',input_snapshot={},configuration={},initial_admission_expires_at=now)); session.flush()
        for identity, state, age in [('upload_old','temporary',2),('upload_ready','ready',10),('upload_new','temporary',0)]:
            session.add(Upload(id=identity,meeting_id='meeting_test',owner_id='owner_test',status=state,filename='private.txt',media_type='text/plain',canonical_text='private',byte_size=7,created_at=now-timedelta(hours=age)))
        session.add(Command(owner_id='owner_test',command_id='00000000-0000-0000-0000-000000000001',command_type='upload_text',body_hash='a'*64,resource_id='upload_old',meeting_id='meeting_test',result={'filename':'private.txt'}))
        for identity, key, digest, days, state in [('export_old',old_key,old_hash,8,'ready'),('export_new',new_key,new_hash,1,'ready'),('export_interrupted',None,None,1,'pending')]:
            session.add(ExportRecord(id=identity,run_id='run_test',owner_id='owner_test',report_version=1,format='json',status=state,path=key,sha256=digest,created_at=now-timedelta(days=days)))
    result = maintenance(factory, store.directory, now=now)
    assert result['expired_uploads']==1 and result['expired_exports']==1 and result['interrupted_exports']==1
    assert not (store.directory/old_key).exists() and store.read(new_key,new_hash)==b'current report'
    with factory() as session:
        assert session.get(Upload,'upload_old').canonical_text is None
        assert session.get(Upload,'upload_ready').canonical_text=='private'
        assert session.get(Upload,'upload_new').status=='temporary'
        receipt = session.scalar(select(Command))
        assert receipt.gone and receipt.result=={}
        assert session.get(Run,'run_test').input_snapshot=={}


def test_access_logging_does_not_disclose_callback_codes_or_query_tokens():
    record = logging.LogRecord('uvicorn.access',logging.INFO,'',0,'%s %s %s %s %s',('127.0.0.1','GET','/auth/callback?code=PRIVATE_CODE&state=SECRET','HTTP/1.1',200),None)
    assert RedactAccessQueries().filter(record)
    assert 'PRIVATE_CODE' not in record.getMessage() and '/auth/callback?[redacted]' in record.getMessage()
