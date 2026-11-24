from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
from uuid import uuid4
import hashlib,json
from fastapi.testclient import TestClient
from sqlalchemy import select
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.fixtures import sample_brief
from round_robin.database.models import Attempt, Run, ReportVersion
from backend.tests.test_orchestration import drain

def cmd(**kw): return {'command_id':str(uuid4()),**kw}
with TemporaryDirectory(prefix='rr-evidence-audit-') as td:
    p=Path(td)
    settings=replace(Settings(),development=True,execution_mode='simulated',paid_access_enabled=False,provider_config_file=None,artifact_directory=p/'artifacts',durability_control_directory=p/'control')
    app=create_app('sqlite:///'+str(p/'probe.db'),settings=settings,initialize=True)
    with TestClient(app) as client:
        brief=sample_brief().model_dump(mode='json')
        meeting=client.post('/api/meetings',json=cmd(brief=brief))
        assert meeting.status_code==201,meeting.text
        mid=meeting.json()['id']
        content=('Ordinary supplier quote and specifications.\n'*1200).encode()
        upload=client.post(f'/api/meetings/{mid}/uploads',content=content,headers={'Content-Type':'text/plain','X-Command-Id':str(uuid4()),'X-Expected-Revision':'1','X-Filename':'supplier-quote.txt','X-Content-SHA256':hashlib.sha256(content).hexdigest()})
        assert upload.status_code==201,upload.text
        brief['selected_attachment_ids']=[upload.json()['id']]
        saved=client.patch(f'/api/meetings/{mid}/draft',json=cmd(expected_revision=1,brief=brief))
        assert saved.status_code==200,saved.text
        revision=saved.json()['revision']
        quote=client.post(f'/api/meetings/{mid}/preflight',json=cmd(expected_draft_revision=revision))
        assert quote.status_code==200,quote.text
        started=client.post(f'/api/meetings/{mid}/runs',json=cmd(preflight_id=quote.json()['id'],expected_draft_revision=revision))
        assert started.status_code==202,started.text
        rid=started.json()['id']
        drain(app.state.session_factory)
        with app.state.session_factory() as session:
            row=session.get(Run,rid)
            attempts=list(session.scalars(select(Attempt).where(Attempt.run_id==rid)))
            report=session.scalar(select(ReportVersion).where(ReportVersion.run_id==rid))
            result={'upload_bytes':len(content),'upload_status':upload.status_code,'upload_state':upload.json()['status'],'preflight_status':quote.status_code,'preflight_blockers':quote.json()['blockers'],'preflight_warnings':quote.json()['warnings'],'start_status':started.status_code,'terminal_status':row.status,'stop_reason':row.stop_reason,'attempt_stages':[a.request_json.get('stage') for a in attempts],'comparison':report.content['comparison'] if report else None,'provider_requests_external':0}
        print(json.dumps(result,indent=2))
        Path('/tmp/round_robin_attachment_probe.json').write_text(json.dumps(result,indent=2))
    app.state.engine.dispose()
