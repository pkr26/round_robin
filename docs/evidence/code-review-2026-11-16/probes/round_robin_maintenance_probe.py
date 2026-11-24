from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import event,select
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.fixtures import sample_brief
from round_robin.database.models import Run
from round_robin.operations.maintenance import maintenance
import json

def cmd(**kw):return {'command_id':str(uuid4()),**kw}
with TemporaryDirectory(prefix='rr-maintenance-audit-') as td:
 p=Path(td);settings=replace(Settings(),development=True,execution_mode='simulated',paid_access_enabled=False,provider_config_file=None,artifact_directory=p/'artifacts',durability_control_directory=p/'control')
 app=create_app('sqlite:///'+str(p/'probe.db'),settings=settings,initialize=True)
 with TestClient(app) as c:
  m=c.post('/api/meetings',json=cmd(brief=sample_brief().model_dump(mode='json'))).json()
  q=c.post(f"/api/meetings/{m['id']}/preflight",json=cmd(expected_draft_revision=1)).json()
  r=c.post(f"/api/meetings/{m['id']}/runs",json=cmd(preflight_id=q['id'],expected_draft_revision=1)).json()
  assert c.request('DELETE',f"/api/meetings/{m['id']}",json=cmd()).status_code==200
  sql=[]
  def log(_conn,_cursor,statement,_params,_context,_executemany):sql.append(statement)
  event.listen(app.state.engine,'before_cursor_execute',log)
  out=[]
  for _ in range(2):
   sql.clear();result=maintenance(app.state.session_factory,settings.artifact_directory)
   count=len(sql)
   with app.state.session_factory() as s:fence=s.get(Run,r['id']).execution_fence
   out.append({'cleanup_result':result,'sql_statements':count,'run_fence':fence})
  print(json.dumps(out,indent=2))
 app.state.engine.dispose()
