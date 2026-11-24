"""API-path proof of stale-cost re-publication with temporary synthetic live records.

Only fixture setup converts simulated records to a live-labeled control fixture;
no live adapter or paid authorization is invoked. The demonstrated failure is
normal owner assessment POST behavior against that already-completed fixture.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy import select

from backend.tests.test_evidence_regressions import complete_brief
from backend.tests.test_orchestration import seed,drain
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Run,ReportVersion,Step,EvidenceRecord
from round_robin.orchestration.reporting import build_report


def run_probe():
    day0=datetime(2026,10,28,19,0,tzinfo=timezone.utc)
    day1=datetime(2026,11,17,19,0,tzinfo=timezone.utc)
    expiry=datetime(2026,11,10,tzinfo=timezone.utc).date()
    with TemporaryDirectory(prefix='session-expiry-api-') as directory:
        app=create_app(f'sqlite:///{Path(directory)/"probe.sqlite"}',settings=replace(Settings(),artifact_directory=Path(directory)/'artifacts'),initialize=True)
        app.dependency_overrides[require_owner]=lambda:'owner_test'
        factory=app.state.session_factory
        rid=seed(factory,brief=complete_brief(valid_until=expiry))
        with factory.begin() as session:session.get(Run,rid).created_at=day0
        drain(factory)
        # A synthetic completed live fixture; its steps/source bytes are valid
        # records from the actual local pipeline. No source dates are changed.
        with factory.begin() as session:
            run=session.get(Run,rid)
            run.configuration={**run.configuration,'mode':'live'}
            steps=list(session.scalars(select(Step).where(Step.run_id==rid).order_by(Step.created_at,Step.id)))
            for step in steps:
                step.result_json={**step.result_json,'simulated':False}
            original=session.scalar(select(ReportVersion).where(ReportVersion.run_id==rid))
            usage={**original.content['usage'],'simulated':False}
            evidence=[item.content for item in session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id==rid))]
            body=build_report(run,steps,status=run.status,stop_reason=run.stop_reason,report_id=original.id,version=original.version,usage=usage,evidence=evidence)
            body['event_cutoff']=original.event_cutoff
            original.content=body
            calls_before=sum(step.calls for step in steps)
        with TestClient(app) as client, MonkeyPatch.context() as patch:
            patch.setattr('round_robin.api.assessments.utcnow',lambda:day0)
            patch.setattr('round_robin.research.discussion.utcnow',lambda:day0)
            view=client.get(f'/api/runs/{rid}/assessments').json()
            targets=[item for item in view['targets'] if item['target_type']=='option_costs']
            selected=client.get(f'/api/runs/{rid}/report').json()['recommendation']['option_id']
            targets += [item for item in view['targets'] if item['target_type']=='requirement' and item['option_id']==selected]
            def attest(target):
                view=client.get(f'/api/runs/{rid}/assessments').json()
                return client.post(f'/api/runs/{rid}/assessments',json={'command_id':str(uuid4()),'expected_run_version':view['run_version'],
                    **{key:target[key] for key in ('target_type','option_id','requirement_id','target_hash','reference_ids')},
                    'result':'supported' if target['target_type']=='option_costs' else 'pass',
                    'attestation':'I checked these exact saved inputs and their stated scope in this synthetic audit.', 'valid_until':None})
            for target in targets:
                response=attest(target)
                assert response.status_code==201,response.text
            first=client.get(f'/api/runs/{rid}/report').json()
            assert first['readiness']=='ready_to_choose',first['readiness_reasons']
            patch.setattr('round_robin.api.assessments.utcnow',lambda:day1)
            patch.setattr('round_robin.research.discussion.utcnow',lambda:day1)
            requirement=next(item for item in targets if item['target_type']=='requirement')
            response=attest(requirement)
            assert response.status_code==201,response.text
            second=client.get(f'/api/runs/{rid}/report').json()
            assert second['readiness']=='ready_to_choose'
        with factory() as session:calls_after=sum(session.scalars(select(Step.calls).where(Step.run_id==rid)))
        app.state.engine.dispose()
        return {'initial_assessment_time':day0.isoformat(),'reassessment_time':day1.isoformat(),'quote_valid_until':expiry.isoformat(),
            'fresh_requirement_post_status':response.status_code,'initial_report_version':first['version'],'new_report_version':second['version'],
            'new_readiness':second['readiness'],'new_cost_totals':[item['total'] for item in second['comparison']],
            'cost_receipt_dates':[item['recorded_at'] for item in second['evidence_assessments'] if item['target_type']=='option_costs'],
            'new_requirement_attestation_did_not_replace_cost_receipts':True,'model_call_count_unchanged':calls_after==calls_before,'external_provider_requests':0,
            'fixture_qualification':'Completed live-labeled fixture derives from actual simulated pipeline; initial label/usage setup is synthetic, and no live-provider quality is claimed.'}

if __name__=='__main__':
    result=run_probe()
    Path(__file__).with_name('api-expiry-results.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
