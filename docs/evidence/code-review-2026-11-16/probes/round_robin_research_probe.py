from pathlib import Path
from tempfile import TemporaryDirectory
import json
import pytest
from sqlalchemy import select
from backend.tests.test_research_service import research_run,SearchFixture,PageFixture
from round_robin.database.models import Run,Step
from round_robin.research.service import schedule_research,execute_research,reconcile_public_research
from round_robin.orchestration.budget import BudgetDenied,remaining_active
from round_robin.orchestration.controller import advance_run
from round_robin.schemas.provider_configuration import ProviderConfiguration
from backend.tests.test_provider_configuration import synthetic_configuration
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
with TemporaryDirectory(prefix='rr-research-audit-') as td:
 p=Path(td);mp=pytest.MonkeyPatch();gen=research_run.__wrapped__(p,mp);factory=next(gen)
 try:
  validated=synthetic_configuration();validated['limits']['active_seconds']=200;validated['finish_policy']['reserved_active_seconds']=50;validated['research']={'enabled':True,'pricing_version':'synthetic_price_v1','per_request_usd':'0.01'}
  ProviderConfiguration.model_validate(validated)
  with factory.begin() as s:
   r=s.get(Run,'run_local');r.configuration={**run_configuration(Settings(),'simulated'),**r.configuration,'limits':{**r.configuration['limits'],'active_seconds':200},'finish_policy':{**r.configuration['finish_policy'],'reserved_active_seconds':50}}
   schedule_research(s,r)
  search=SearchFixture();failures=[]
  for _ in range(2):
   try:execute_research('run_local',session_factory=factory,search=search,transport=PageFixture(),resolver=lambda _:['93.184.216.34'])
   except BudgetDenied as e:failures.append(str(e))
   with factory.begin() as s:
    r=s.get(Run,'run_local');reconciled=reconcile_public_research(s,r);advance_run(s,r)
  with factory() as s:
   r=s.get(Run,'run_local')
   result={'provider_config_schema_valid':True,'active_limit':200,'finish_reserved':50,'errors':failures,'run_status':r.status,'stage':r.stage,'research_state':r.configuration['research_state'],'remaining_active_seconds':remaining_active(r),'reconciler_progressed':reconciled,'proposal_steps':len(list(s.scalars(select(Step)))),'search_calls':len(search.queries),'external_provider_requests':0}
   print(json.dumps(result,indent=2));Path('/tmp/round_robin_research_probe.json').write_text(json.dumps(result,indent=2))
 finally:
  try:next(gen)
  except StopIteration:pass
  mp.undo()
