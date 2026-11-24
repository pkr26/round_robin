"""Offline audit probes; creates only fresh temporary SQLite/artifact state.

Run from the repository:
PYTHONPATH=backend/src:backend/tests:. .venv/bin/python /tmp/rr-audit2-data/probe_exports_and_readiness.py
"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4
import hashlib
import json

from fastapi.testclient import TestClient
from sqlalchemy import select
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import EvidenceRecord, ReportVersion, Run, Step
from round_robin.fixtures import sample_brief
from round_robin.orchestration.controller import prepare_evidence, validated_evidence
from round_robin.orchestration.reporting import build_report
from round_robin.reports.exports import render_saved_report
from test_orchestration import drain, seed

OUT = Path('/tmp/rr-audit2-data')

def command(**kw): return {'command_id': str(uuid4()), **kw}

def checked(response, status):
    assert response.status_code == status, (response.status_code, response.text)
    return response.json()

def markdown_probe():
    with TemporaryDirectory(prefix='rr-audit2-data-export-') as root:
        root = Path(root)
        app = create_app(f'sqlite:///{root / "db.sqlite"}', settings=replace(Settings(), artifact_directory=root / 'private'), initialize=True)
        brief = sample_brief().model_dump(mode='json')
        brief['title'] = 'Audit export: <img src="https://tracking.invalid/title-pixel">'
        brief['options'][0]['label'] = 'Supplier ![preview](https://tracking.invalid/option-pixel)'
        supplier_text = ('Supplier quote: $100.\n'
            '![supplier tracking pixel](https://tracking.invalid/supplier-pixel)\n'
            '<img src="https://tracking.invalid/raw-html-pixel">\n')
        with TestClient(app) as client:
            meeting = checked(client.post('/api/meetings', json=command(brief=brief)), 201)
            meeting_id = meeting['id']
            raw = supplier_text.encode()
            uploaded = checked(client.post(f'/api/meetings/{meeting_id}/uploads', content=raw,
                headers={'Content-Type': 'text/plain', 'X-Command-Id': str(uuid4()),
                    'X-Expected-Revision': '1', 'X-Filename': 'supplier.txt',
                    'X-Content-SHA256': hashlib.sha256(raw).hexdigest()}), 201)
            brief['selected_attachment_ids'] = [uploaded['id']]
            checked(client.patch(f'/api/meetings/{meeting_id}/draft', json=command(expected_revision=1, brief=brief)), 200)
            preflight = checked(client.post(f'/api/meetings/{meeting_id}/preflight', json=command(expected_draft_revision=2, mode='simulated')), 200)
            started = checked(client.post(f'/api/meetings/{meeting_id}/runs', json=command(expected_draft_revision=2, preflight_id=preflight['id'])), 202)
            drain(app.state.session_factory)
            with app.state.session_factory() as session:
                run = session.get(Run, started['id'])
                report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run.id))
                saved = deepcopy(report.content)
                status = run.status
            exported = checked(client.post(f'/api/runs/{started["id"]}/exports', json=command(report_id=saved['id'], format='markdown')), 200)
            assert exported['status'] == 'ready', exported
            download = client.get(exported['download_url'])
            assert download.status_code == 200
            markdown = download.text
            assert '![supplier tracking pixel](https://tracking.invalid/supplier-pixel)' in markdown
            assert '<img src="https://tracking.invalid/raw-html-pixel">' in markdown
            assert '![preview](https://tracking.invalid/option-pixel)' in markdown
            assert '<img src="https://tracking.invalid/title-pixel">' in markdown
            (OUT / 'export-unsafe-markup.md').write_text(markdown)
            return {'http_create': 201, 'upload': uploaded['status'], 'preflight_blockers': preflight['blockers'],
                'http_start': 202, 'run_status': status, 'export_status': exported['status'],
                'raw_remote_image_markdown_preserved': True, 'raw_html_image_preserved': True,
                'network_requests_made': False, 'artifact': str(OUT / 'export-unsafe-markup.md')}

def complete_brief():
    body = sample_brief().model_dump(mode='json')
    body.update(budget='1000', deadline=None, hard_requirements=[], horizon_months=12)
    body['options'] = []
    for index in range(2):
        body['options'].append({'id': f'option_{index}', 'label': f'Fully itemized supplier {index}',
            'role': 'candidate', 'provenance': 'user_supplied', 'cost_components': [
                {'id': f'cost_{index}_{kind}', 'label': kind, 'kind': kind, 'amount': '100' if kind == 'upfront' else '0',
                 'currency': 'USD', 'source_ref': f'quote_{index}', 'frequency': 'once',
                 'status': 'included' if kind == 'upfront' else 'not_applicable'}
                for kind in ['upfront', 'installation', 'recurring', 'maintenance', 'tax_delivery']]})
    from round_robin.schemas.briefs import DraftBrief
    return DraftBrief.model_validate(body)

def readiness_probe(*, amount='100', included_status='included'):
    # Uses real evidence preparation and report construction, with deliberately
    # perfect synthetic live review records. No upstream adapter is constructed.
    with TemporaryDirectory(prefix='rr-audit2-data-ready-') as root:
        root = Path(root)
        app = create_app(f'sqlite:///{root / "db.sqlite"}', settings=replace(Settings(), artifact_directory=root / 'private'), initialize=True)
        brief = complete_brief().model_dump(mode='json')
        for option in brief['options']:
            option['cost_components'][0].update(amount=amount, status=included_status)
        from round_robin.schemas.briefs import DraftBrief
        run_id = seed(app.state.session_factory, brief=DraftBrief.model_validate(brief))
        with app.state.session_factory.begin() as session:
            run = session.get(Run, run_id)
            run.configuration = {**run.configuration, 'mode': 'live'}
            prepare_evidence(session, run)
            session.flush()
            sources = validated_evidence(session, run)
            # A normal supported claim can be accepted against the structured
            # source, but there is no corresponding promotion of that source.
            from round_robin.research.reuse import persist_claims
            claim_step = Step(id='step_claim_probe', run_id=run.id, stage='proposal', role_slot_id='openai', provider='openai', status='succeeded')
            session.add(claim_step)
            session.flush()
            committed = persist_claims(session, run, claim_step, {'claims':[{'alias':'new_claim_1',
                'proposition':'All supplier cost components have been supplied and are current.', 'kind':'fact',
                'evidence_status':'supported', 'reference_ids':[sources[0]['id']]}]}, {sources[0]['id']})
            output = {'summary': 'Choose the fully itemized supplier.', 'recommendation_option_id': 'option_0',
                'constraints': [], 'dissent': [], 'audit_passed': True, 'critical_defects': []}
            steps = [SimpleNamespace(id=f'step_{i}', provider=p, stage=s, status='succeeded', result_json={'output': output}, error_code=None)
                for i, (p, s) in enumerate([('openai','proposal'),('anthropic','proposal'),('google','proposal'),('openai','synthesis'),('google','audit')])]
            report = build_report(run, steps, status='completed', stop_reason='all_stages_completed', report_id='report_ready_probe', version=1,
                usage={'simulated':False,'status':'reported','actual_cost':'0','conservative_exposure_usd':'0','unknown_operation_ids':[],'request_count':5}, evidence=sources, claims=committed['claims'])
            result = {'evidence_statuses': sorted({s['verification_status'] for s in sources}),
                'all_hard_requirements_pass': all(r['result']=='pass' for c in report['comparison'] for r in c['constraint_results']),
                'cost_totals': [c['total'] for c in report['comparison']],
                'assessment': report['assessment'], 'readiness': report['readiness'],
                'accepted_claim_evidence_status': committed['claims'][0]['evidence_status'],
                'reasons': report['readiness_reasons'], 'dependencies': report['essential_dependencies'],
                'source_records_created_by': 'real prepare_evidence', 'review_records': 'synthetic ideal positive distinct-provider reviews; no paid execution'}
            assert report['readiness'] == 'conditional'
            if included_status == 'included':
                assert result['all_hard_requirements_pass'] and result['cost_totals'] == [f'{amount}.00', f'{amount}.00']
                assert len(report['readiness_reasons']) == 1
            (OUT / f'readiness-{included_status}-{amount}-report.json').write_text(json.dumps(report,indent=2))
            return result

if __name__ == '__main__':
    result = {'markdown': markdown_probe(), 'readiness': {
        'positive_included': readiness_probe(), 'zero_included': readiness_probe(amount='0'),
        'excluded': readiness_probe(included_status='excluded'),
        'not_applicable': readiness_probe(amount='0', included_status='not_applicable')}}
    (OUT / 'probe-results.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
